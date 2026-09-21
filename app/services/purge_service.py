"""Physical purge of soft-deleted WeKnora data.

WeKnora only soft deletes (it writes ``deleted_at``) and exposes no purge endpoint, so
leftovers keep occupying PostgreSQL and the vector store. This service talks to the
database directly and removes them for real.

The endpoint is intentionally blunt - it is meant to be called by a scheduled job:

    DELETE /api/v2/management/purge?retention_days=30&include_embed=true

Everything whose ``deleted_at`` is older than ``now - retention_days`` goes away,
together with every child row, and - when requested - any vector/chunk row that is no
longer attached to an existing knowledge item. The WeKnora RAG pipeline has no
database-level foreign keys, so the cascade is executed by hand in child -> parent
order, exactly as in the repository's purge.js.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from ..config import Config, PurgeTable
from ..errors import bad_request
from ..logging import get_logger
from .db import Executor, quote_ident

logger = get_logger(__name__)

KNOWLEDGE_TABLE = "knowledges"
KB_TABLE = "knowledge_bases"


@dataclass
class PurgeRequest:
    retention_days: int = 30
    include_embed: bool = False
    dry_run: bool = True
    max_rows: Optional[int] = None


@dataclass
class PurgeReport:
    dry_run: bool
    retention_days: int
    cutoff: str
    include_embed: bool
    knowledge_base_count: int = 0
    knowledge_count: int = 0
    knowledge_bases_sample: List[str] = field(default_factory=list)
    knowledges_sample: List[str] = field(default_factory=list)
    matched: Dict[str, int] = field(default_factory=dict)
    deleted: Dict[str, int] = field(default_factory=dict)
    orphan_matched: Dict[str, int] = field(default_factory=dict)
    orphan_deleted: Dict[str, int] = field(default_factory=dict)
    skipped_tables: List[str] = field(default_factory=list)
    truncated: bool = False


class PurgeService:
    """Runs inside a single transaction supplied by the caller."""

    def __init__(self, executor: Executor, config: Config) -> None:
        self.ex = executor
        self.config = config

    # ------------------------------------------------------------------ #
    async def run(self, req: PurgeRequest) -> PurgeReport:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(req.retention_days, 0))
        config = self.config.purge
        max_rows = req.max_rows or config.max_rows

        kb_ids = await self._soft_deleted_ids(KB_TABLE, cutoff)
        kn_ids = await self._soft_deleted_knowledges(cutoff, kb_ids)

        total = len(kb_ids) + len(kn_ids)
        truncated = False
        if max_rows and total > max_rows:
            truncated = True
            raise bad_request(
                f"Candidate row count {total} exceeds the configured limit {max_rows}; "
                "lower retention_days window or raise purge.max_rows",
                error_id="PURGE_LIMIT_EXCEEDED",
            )

        report = PurgeReport(
            dry_run=req.dry_run,
            retention_days=req.retention_days,
            cutoff=cutoff.isoformat(),
            include_embed=req.include_embed,
            knowledge_base_count=len(kb_ids),
            knowledge_count=len(kn_ids),
            knowledge_bases_sample=kb_ids[:10],
            knowledges_sample=kn_ids[:10],
        )

        if not kb_ids and not kn_ids:
            logger.info("purge: nothing older than %s", cutoff.isoformat())
            if req.include_embed:
                await self._sweep_orphans(report, req.dry_run)
            return report

        await self._cascade(report, kb_ids, kn_ids, req.dry_run)
        await self._delete_knowledge_bases(report, kb_ids, req.dry_run)

        if req.include_embed:
            await self._sweep_orphans(report, req.dry_run)

        logger.warning(
            "purge finished | dry_run=%s retention_days=%s kb=%d knowledge=%d deleted=%s orphans=%s",
            req.dry_run, req.retention_days, len(kb_ids), len(kn_ids), report.deleted, report.orphan_deleted,
        )
        return report

    # ------------------------------------------------------------------ #
    async def _soft_deleted_ids(self, table: str, cutoff: datetime) -> List[str]:
        columns = await self.ex.columns(table)
        if not columns or "deleted_at" not in columns:
            return []
        rows = await self.ex.fetch(
            f"SELECT CAST(id AS TEXT) AS id FROM {quote_ident(table)} "
            f"WHERE deleted_at IS NOT NULL AND deleted_at < :cutoff",
            {"cutoff": cutoff},
        )
        return [str(r["id"]) for r in rows]

    async def _soft_deleted_knowledges(self, cutoff: datetime, kb_ids: List[str]) -> List[str]:
        columns = await self.ex.columns(KNOWLEDGE_TABLE)
        if not columns:
            return []
        where = "deleted_at IS NOT NULL AND deleted_at < :cutoff"
        params: Dict[str, Any] = {"cutoff": cutoff}
        if kb_ids and "knowledge_base_id" in columns:
            where += " OR CAST(knowledge_base_id AS TEXT) IN :kb_ids"
            params["kb_ids"] = kb_ids
        rows = await self.ex.fetch(
            f"SELECT CAST(id AS TEXT) AS id FROM {quote_ident(KNOWLEDGE_TABLE)} WHERE {where}", params
        )
        return [str(r["id"]) for r in rows]

    # ------------------------------------------------------------------ #
    async def _cascade(self, report: PurgeReport, kb_ids: List[str], kn_ids: List[str], dry_run: bool) -> None:
        for spec in self.config.purge.tables:
            table = spec.table
            columns = await self.ex.columns(table)
            if not columns:
                report.skipped_tables.append(table)
                continue
            where, params = self._target_filter(spec, columns, kb_ids, kn_ids)
            if not where:
                report.skipped_tables.append(table)
                continue
            matched = int(
                await self.ex.scalar(f"SELECT COUNT(*) FROM {quote_ident(table)} WHERE {where}", params) or 0
            )
            report.matched[table] = matched
            if dry_run or not matched:
                report.deleted[table] = 0
                continue
            report.deleted[table] = int(await self.ex.execute(f"DELETE FROM {quote_ident(table)} WHERE {where}", params))

    async def _delete_knowledge_bases(self, report: PurgeReport, kb_ids: List[str], dry_run: bool) -> None:
        if not kb_ids:
            return
        columns = await self.ex.columns(KB_TABLE)
        if not columns:
            report.skipped_tables.append(KB_TABLE)
            return
        where = "CAST(id AS TEXT) IN :kb_ids"
        params = {"kb_ids": kb_ids}
        matched = int(await self.ex.scalar(f"SELECT COUNT(*) FROM {quote_ident(KB_TABLE)} WHERE {where}", params) or 0)
        report.matched[KB_TABLE] = matched
        if dry_run or not matched:
            report.deleted[KB_TABLE] = 0
            return
        report.deleted[KB_TABLE] = int(
            await self.ex.execute(f"DELETE FROM {quote_ident(KB_TABLE)} WHERE {where}", params)
        )

    # ------------------------------------------------------------------ #
    async def _sweep_orphans(self, report: PurgeReport, dry_run: bool) -> None:
        """Remove rows whose knowledge / knowledge base no longer exists."""
        for spec in self.config.purge.orphan_tables:
            table = spec.table
            columns = await self.ex.columns(table)
            if not columns:
                report.skipped_tables.append(f"{table} (orphan)")
                continue
            clauses: List[str] = []
            params: Dict[str, Any] = {}
            if spec.knowledge_column and spec.knowledge_column in columns:
                clauses.append(
                    f"({quote_ident(spec.knowledge_column)} IS NOT NULL AND NOT EXISTS "
                    f"(SELECT 1 FROM {quote_ident(KNOWLEDGE_TABLE)} k "
                    f"WHERE CAST(k.id AS TEXT) = CAST({quote_ident(spec.knowledge_column)} AS TEXT)))"
                )
            if spec.knowledge_base_column and spec.knowledge_base_column in columns:
                clauses.append(
                    f"({quote_ident(spec.knowledge_base_column)} IS NOT NULL AND NOT EXISTS "
                    f"(SELECT 1 FROM {quote_ident(KB_TABLE)} kb "
                    f"WHERE CAST(kb.id AS TEXT) = CAST({quote_ident(spec.knowledge_base_column)} AS TEXT)))"
                )
            if not clauses:
                report.skipped_tables.append(f"{table} (orphan)")
                continue
            where = " OR ".join(clauses)
            key = f"{table} (orphan)"
            matched = int(await self.ex.scalar(f"SELECT COUNT(*) FROM {quote_ident(table)} WHERE {where}", params) or 0)
            report.orphan_matched[key] = matched
            if dry_run or not matched:
                report.orphan_deleted[key] = 0
                continue
            report.orphan_deleted[key] = int(
                await self.ex.execute(f"DELETE FROM {quote_ident(table)} WHERE {where}", params)
            )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _target_filter(
        spec: PurgeTable, columns: Set[str], kb_ids: List[str], kn_ids: List[str]
    ) -> tuple[str, Dict[str, Any]]:
        clauses: List[str] = []
        params: Dict[str, Any] = {}
        if kb_ids and spec.knowledge_base_column and spec.knowledge_base_column in columns:
            clauses.append(f"CAST({quote_ident(spec.knowledge_base_column)} AS TEXT) IN :kb_ids")
            params["kb_ids"] = kb_ids
        if kn_ids and spec.knowledge_column and spec.knowledge_column in columns:
            clauses.append(f"CAST({quote_ident(spec.knowledge_column)} AS TEXT) IN :kn_ids")
            params["kn_ids"] = kn_ids
        if not clauses:
            return "", {}
        # Never run without a target substitution
        if not params:
            return "", {}
        return " OR ".join(clauses), params


def resolve_request(
    *,
    retention_days: Optional[int],
    include_embed: Optional[bool],
    config: Config,
    dry_run: Optional[bool] = None,
) -> PurgeRequest:
    """Merge query parameters with config defaults."""
    purge = config.purge
    days = purge.default_retention_days if retention_days is None else retention_days
    if days is None or days < 0:
        raise bad_request("retention_days must be a non-negative integer", error_id="INVALID_RETENTION_DAYS")
    return PurgeRequest(
        retention_days=days,
        include_embed=purge.include_embed if include_embed is None else include_embed,
        dry_run=purge.dry_run if dry_run is None else dry_run,
        max_rows=purge.max_rows,
    )
