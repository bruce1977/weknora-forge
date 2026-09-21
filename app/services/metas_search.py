"""Custom Metas search backed directly by PostgreSQL.

Why SQL and not a side index
----------------------------
WeKnora keeps ``custom_metadata`` as a JSON column on ``knowledges``. PostgreSQL can
index and filter jsonb natively, so Forge compiles the FMQ expression into a WHERE
clause and lets the database do the heavy lifting - no second copy of the data, no
extra dependency and no staleness window.

Execution model
---------------
1. FMQ is compiled to SQL. The compiled predicate is guaranteed to be a SUPERSET of the
   exact expression (see meta_dsl.compile_sql), so nothing is filtered out too early.
2. Up to ``metas_search.max_rows`` candidate rows are fetched, already ordered the way
   the deployment wants them.
3. Every candidate is re-evaluated in Python with full FMQ semantics (numbers vs
   strings vs dates vs lists).
4. The exact result is paginated in memory, so ``total`` / ``page`` are exact.

Column names are resolved against ``information_schema`` at runtime, which keeps this
working when the upstream WeKnora schema shifts (``file_name`` candidates, optional
knowledge-base / tag joins, an absent relation table, ...).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..config import Config, MetasSearchConfig
from ..errors import bad_request
from ..logging import get_logger
from .db import Executor, quote_ident
from .meta_dsl import BUILTIN_FIELDS, build_fields, compile_sql, describe, evaluate, parse_query, used_fields

logger = get_logger(__name__)

MAIN_ALIAS = "k"
KB_ALIAS = "kb"
TAG_ALIAS = "tag"
REL_ALIAS = "rel"
VEC_ALIAS = "vec_agg"
VEC_INNER_ALIAS = "vec"

# Logical FMQ built-in field -> physical column on the knowledge table.
BUILTIN_COLUMNS: Dict[str, str] = {
    "id": "id",
    "kb_id": "knowledge_base_id",
    "title": "title",
    "description": "description",
    "type": "type",
    "file_type": "file_type",
    "source": "source",
    "parse_status": "parse_status",
    "enable_status": "enable_status",
    "created_at": "created_at",
    "updated_at": "updated_at",
    "deleted_at": "deleted_at",
    "tag_id": "tag_id",
}

# Always projected as hidden columns so that sorting and $ field lookups always work.
BASE_INTERNAL_COLUMNS = ("id", "knowledge_base_id", "title", "updated_at", "created_at")


@dataclass
class SearchRequest:
    query: str
    kb_id: Optional[str] = None
    page: int = 1
    page_size: int = 20
    case_insensitive: bool = False
    vector: Optional[Sequence[float]] = None
    include_deleted: Optional[bool] = None
    title: Optional[str] = None
    tags: Optional[List[str]] = None


@dataclass
class SearchResult:
    rows: List[Dict[str, Any]] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    has_more: bool = False
    scanned: int = 0
    truncated: bool = False
    similarity: bool = False
    query: str = ""
    sql: str = ""
    ast: Any = None
    fields: List[str] = field(default_factory=list)
    order_by: str = ""


class MetasSearchService:
    """Builds and runs the metadata search query."""

    def __init__(self, executor: Executor, config: Config) -> None:
        self.ex = executor
        self.config = config
        self.cfg: MetasSearchConfig = config.metas_search

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    async def search(self, req: SearchRequest) -> SearchResult:
        cfg = self.cfg
        table_columns = await self.ex.columns(cfg.table)
        if not table_columns:
            raise bad_request(
                f"Table '{cfg.table}' was not found in the configured database",
                error_id="TABLE_NOT_FOUND",
            )
        if cfg.metadata_column not in table_columns:
            raise bad_request(
                f"Table '{cfg.table}' has no '{cfg.metadata_column}' column - check the metas_search config",
                error_id="METADATA_COLUMN_MISSING",
            )

        node = parse_query(req.query)
        page = max(req.page, 1)
        page_size = min(max(req.page_size, 1), max(cfg.max_page_size, 1))

        built = await self._build_query(node, table_columns, req, page_size)
        candidates = await self.ex.fetch(built.sql, built.params)

        matched = [row for row in candidates if self._matches(row, built.internal_keys, node, req.case_insensitive)]
        total = len(matched)
        start = (page - 1) * page_size
        page_rows = matched[start : start + page_size]

        return SearchResult(
            rows=[{alias: row.get(alias) for alias in built.output_aliases} for row in page_rows],
            total=total,
            page=page,
            page_size=page_size,
            has_more=start + page_size < total,
            scanned=len(candidates),
            truncated=len(candidates) >= built.limit,
            similarity=req.vector is not None,
            query=req.query,
            sql=built.sql,
            ast=describe(node),
            fields=used_fields(node),
            order_by=built.order_sql,
        )

    # ------------------------------------------------------------------ #
    # Query construction
    # ------------------------------------------------------------------ #
    async def _build_query(self, node, table_columns: Set[str], req: SearchRequest, page_size: int) -> "BuiltQuery":
        cfg = self.cfg
        joins, join_expressions = await self._resolve_joins(table_columns)
        projections: List[str] = []
        projection_order: List[Tuple[str, bool]] = []  # (alias, hidden)

        def add(alias: str, expression: str, hidden: bool = False) -> None:
            if any(existing == alias for existing, _hidden in projection_order):
                return
            projection_order.append((alias, hidden))
            projections.append(f"{expression} AS {quote_ident(alias)}")

        # Hidden: the whole metadata blob, needed for the exact Python evaluation
        add("_metas", f"{MAIN_ALIAS}.{quote_ident(cfg.metadata_column)}", hidden=True)

        internal_keys: Dict[str, str] = {}
        for logical, column in BUILTIN_COLUMNS.items():
            used = any(f == f"${logical}" for f in used_fields(node))
            if column not in table_columns:
                continue
            if not (used or column in BASE_INTERNAL_COLUMNS or logical in BUILTIN_FIELDS):
                continue
            alias = f"_b_{column}"
            internal_keys[logical] = alias
            add(alias, f"{MAIN_ALIAS}.{quote_ident(column)}", hidden=True)

        similarity_expr = self._build_vector_clause(req, joins)
        add("similarity", similarity_expr, hidden="similarity" not in cfg.result_column)

        for name in cfg.result_column:
            if name == "similarity":
                continue  # already projected above
            alias = self._safe_alias(name)
            expression = self._result_expression(name, join_expressions, table_columns)
            if expression is None:
                logger.debug("result column %s could not be resolved, skipping", name)
                continue
            add(alias, expression)

        where_sql, params, order_sql = await self._build_filters(node, table_columns, req, joins, params_vector=None)
        limit = max(cfg.max_rows, page_size)
        sql = (
            f"SELECT {', '.join(projections)} "
            f"FROM {quote_ident(cfg.table)} {MAIN_ALIAS} "
            f"{where_sql} {order_sql} LIMIT {limit}"
        )
        output_aliases = [alias for alias, hidden in projection_order if not hidden]
        return BuiltQuery(
            sql=sql,
            params=params,
            output_aliases=output_aliases,
            internal_keys=internal_keys,
            order_sql=order_sql,
            limit=limit,
        )

    # ------------------------------------------------------------------ #
    def _result_expression(
        self, name: str, join_expressions: Dict[str, str], table_columns: Set[str]
    ) -> Optional[str]:
        cfg = self.cfg
        if name in join_expressions:
            return join_expressions[name]
        if name == "file_name":
            candidate = next((c for c in cfg.file_name_candidate if c in table_columns), None)
            return f"{MAIN_ALIAS}.{quote_ident(candidate)}" if candidate else "NULL::text"
        if name in table_columns:
            return f"{MAIN_ALIAS}.{quote_ident(name)}"
        # Unknown name: expose it straight out of custom_metadata (a.b paths included)
        parts = [p for p in name.split(".") if p]
        if not parts:
            return None
        head = "".join(f" -> '{p.replace(chr(39), chr(39) * 2)}'" for p in parts[:-1])
        tail = parts[-1].replace("'", "''")
        return f"({MAIN_ALIAS}.{quote_ident(cfg.metadata_column)})::jsonb{head} ->> '{tail}'"

    # ------------------------------------------------------------------ #
    def _build_vector_clause(self, req: SearchRequest, joins: List[str]) -> str:
        cfg = self.cfg.vector
        if not req.vector:
            return "NULL::float8"
        literal = "[" + ",".join(f"{float(v):.10g}" for v in req.vector) + "]"
        self._vector_literal = literal
        distance = (
            f"MIN({VEC_INNER_ALIAS}.{quote_ident(cfg.column)} "
            f"{cfg.distance_operator} CAST(:forge_vector AS vector))"
        )
        joins.append(
            f"LEFT JOIN LATERAL ("
            f"SELECT {distance} AS _distance "
            f"FROM {quote_ident(cfg.table)} {VEC_INNER_ALIAS} "
            f"WHERE {VEC_INNER_ALIAS}.{quote_ident(cfg.knowledge_column)} = {MAIN_ALIAS}.{quote_ident('id')}"
            f") {VEC_ALIAS} ON TRUE"
        )
        return cfg.similarity_expression.replace("{distance}", f"{VEC_ALIAS}._distance")

    # ------------------------------------------------------------------ #
    async def _resolve_joins(self, table_columns: Set[str]) -> Tuple[List[str], Dict[str, str]]:
        """Return (join clauses, alias -> expression) for knowledge base / tag names."""
        cfg = self.cfg
        joins: List[str] = []
        expressions: Dict[str, str] = {}

        if cfg.join.knowledge_base_name and "knowledge_base_id" in table_columns:
            kb_columns = await self.ex.columns(cfg.knowledge_base_table)
            if {"id", "name"} <= kb_columns:
                joins.append(
                    f"LEFT JOIN {quote_ident(cfg.knowledge_base_table)} {KB_ALIAS} "
                    f"ON {KB_ALIAS}.{quote_ident('id')} = {MAIN_ALIAS}.{quote_ident('knowledge_base_id')}"
                )
                expressions["kb_name"] = f'{KB_ALIAS}.{quote_ident("name")}'

        if cfg.join.tag_name:
            tag_columns = await self.ex.columns(cfg.tag_table)
            if "name" in tag_columns:
                relation_columns = await self.ex.columns(cfg.tag_relation_table)
                if {"knowledge_id", "tag_id"} <= relation_columns:
                    joins.append(
                        f"LEFT JOIN {quote_ident(cfg.tag_relation_table)} {REL_ALIAS} "
                        f"ON {REL_ALIAS}.{quote_ident('knowledge_id')} = {MAIN_ALIAS}.{quote_ident('id')}"
                    )
                    joins.append(
                        f"LEFT JOIN {quote_ident(cfg.tag_table)} {TAG_ALIAS} "
                        f"ON {TAG_ALIAS}.{quote_ident('id')} = {REL_ALIAS}.{quote_ident('tag_id')}"
                    )
                    expressions["tag_name"] = f'{TAG_ALIAS}.{quote_ident("name")}'
                elif "tag_id" in table_columns:
                    joins.append(
                        f"LEFT JOIN {quote_ident(cfg.tag_table)} {TAG_ALIAS} "
                        f"ON {TAG_ALIAS}.{quote_ident('id')} = {MAIN_ALIAS}.{quote_ident('tag_id')}"
                    )
                    expressions["tag_name"] = f'{TAG_ALIAS}.{quote_ident("name")}'
        return joins, expressions

    # ------------------------------------------------------------------ #
    async def _build_filters(
        self,
        node,
        table_columns: Set[str],
        req: SearchRequest,
        joins: List[str],
        params_vector: Optional[Dict[str, Any]],
    ) -> Tuple[str, Dict[str, Any], str]:
        cfg = self.cfg
        builtins = {
            logical: f"{MAIN_ALIAS}.{quote_ident(column)}"
            for logical, column in BUILTIN_COLUMNS.items()
            if column in table_columns
        }
        where_sql, params = compile_sql(
            node,
            metas_column=f"{MAIN_ALIAS}.{quote_ident(cfg.metadata_column)}",
            builtin_columns=builtins,
            case_insensitive=req.case_insensitive,
        )
        clauses: List[str] = [f"({where_sql})"]

        include_deleted = cfg.include_deleted if req.include_deleted is None else req.include_deleted
        if not include_deleted and "deleted_at" in table_columns:
            clauses.append(f"{MAIN_ALIAS}.{quote_ident('deleted_at')} IS NULL")

        if req.kb_id:
            params["forge_kb_id"] = str(req.kb_id)
            clauses.append(f"CAST({MAIN_ALIAS}.{quote_ident('knowledge_base_id')} AS TEXT) = :forge_kb_id")

        # Title search filter
        if req.title:
            title_op = "ILIKE" if req.case_insensitive else "LIKE"
            params["forge_title"] = f"%{req.title}%"
            clauses.append(f"{MAIN_ALIAS}.{quote_ident('title')} {title_op} :forge_title")

        # Tags filter
        if req.tags:
            # Ensure tag join exists
            tag_columns = await self.ex.columns(cfg.tag_table)
            if "name" in tag_columns:
                relation_columns = await self.ex.columns(cfg.tag_relation_table)
                if {"knowledge_id", "tag_id"} <= relation_columns:
                    # Check if tag join already exists
                    tag_join_exists = any(TAG_ALIAS in j for j in joins)
                    if not tag_join_exists:
                        joins.append(
                            f"LEFT JOIN {quote_ident(cfg.tag_relation_table)} {REL_ALIAS} "
                            f"ON {REL_ALIAS}.{quote_ident('knowledge_id')} = {MAIN_ALIAS}.{quote_ident('id')}"
                        )
                        joins.append(
                            f"LEFT JOIN {quote_ident(cfg.tag_table)} {TAG_ALIAS} "
                            f"ON {TAG_ALIAS}.{quote_ident('id')} = {REL_ALIAS}.{quote_ident('tag_id')}"
                        )
                    tag_placeholders = []
                    for i, tag in enumerate(req.tags):
                        param_name = f"forge_tag_{i}"
                        params[param_name] = tag
                        tag_placeholders.append(f":{param_name}")
                    clauses.append(
                        f"{TAG_ALIAS}.{quote_ident('name')} IN ({', '.join(tag_placeholders)})"
                    )

        if cfg.extra_where.strip():
            clauses.append(f"({cfg.extra_where.strip()})")

        if getattr(self, "_vector_literal", None):
            params["forge_vector"] = self._vector_literal

        from_sql = " ".join(joins).strip()
        where = "WHERE " + " AND ".join(clauses)
        return f"{from_sql} {where}".strip(), params, self._order_sql(table_columns)

    # ------------------------------------------------------------------ #
    def _order_sql(self, table_columns: Set[str]) -> str:
        """Ordering comes from config; unknown columns are skipped, never fatal."""
        parts: List[str] = []
        for spec in self.cfg.default_order:
            name = (spec.column or "").strip()
            if not name:
                continue
            direction = "DESC" if spec.desc else "ASC"
            if name == "similarity":
                parts.append(f'{quote_ident("similarity")} {direction} NULLS LAST')
            elif name in table_columns or name in {"kb_name", "tag_name"}:
                parts.append(f"{quote_ident(name)} {direction} NULLS LAST")
        if not parts and "updated_at" in table_columns:
            parts.append(f'{quote_ident("updated_at")} DESC NULLS LAST')
        return "ORDER BY " + ", ".join(parts) if parts else ""

    # ------------------------------------------------------------------ #
    async def apply_index_hints(self) -> None:
        """SET LOCAL knobs requested by config (applied inside the request transaction)."""
        cfg = self.cfg.vector
        if cfg.default_probes > 0:
            await self.ex.set_local("ivfflat.probes", cfg.default_probes)
        if cfg.default_ef_search > 0:
            await self.ex.set_local("hnsw.ef_search", cfg.default_ef_search)
        if cfg.default_beam_factor > 0:
            await self.ex.set_local("hnsw.iterative_scan", "relaxed_ordered")
            await self.ex.set_local("hnsw.max_scan_tuples", cfg.default_beam_factor * 1000)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _matches(row: Dict[str, Any], internal_keys: Dict[str, str], node, case_insensitive: bool) -> bool:
        raw = row.get("_metas")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        record = {logical: row.get(alias) for logical, alias in internal_keys.items()}
        return evaluate(node, build_fields(record, raw), case_insensitive)

    @staticmethod
    def _safe_alias(name: str) -> str:
        alias = name.replace(".", "_")
        return alias if alias.replace("_", "").isalnum() else "col"


@dataclass
class BuiltQuery:
    sql: str
    params: Dict[str, Any]
    output_aliases: List[str]
    internal_keys: Dict[str, str]
    order_sql: str
    limit: int


def builtin_field_names() -> List[str]:
    return sorted(f"${name}" for name in BUILTIN_FIELDS)
