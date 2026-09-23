"""PostgreSQL metadata search tests.

The service talks to PostgreSQL through the Executor interface, so the tests run it
against a recording double and assert both the generated SQL and the exact filtering
behaviour of the Python layer.
"""

from __future__ import annotations

import json

import pytest

from app.services.db import FakeExecutor
from app.services.metas_search import MetasSearchService, SearchRequest

KNOWLEDGE_COLUMNS = {
    "id",
    "knowledge_base_id",
    "title",
    "description",
    "source",
    "type",
    "parse_status",
    "enable_status",
    "created_at",
    "updated_at",
    "deleted_at",
    "custom_metadata",
    "metadata",
}
KB_COLUMNS = {"id", "name"}
TAG_COLUMNS = {"id", "name"}
RELATION_COLUMNS = {"knowledge_id", "tag_id"}
CHUNK_COLUMNS = {
    "id",
    "knowledge_id",
    "content",
    "chunk_index",
    "chunk_type",
    "deleted_at",
}


class RecordingExecutor(FakeExecutor):
    def __init__(self, rows, *, chunk_rows=None):
        columns = {
            "knowledges": KNOWLEDGE_COLUMNS,
            "knowledge_bases": KB_COLUMNS,
            "knowledge_tags": TAG_COLUMNS,
            "knowledge_tag_relations": RELATION_COLUMNS,
            "chunks": CHUNK_COLUMNS,
        }
        super().__init__(rows=rows, columns=columns)
        self._chunk_rows = list(chunk_rows or [])

    async def fetch(self, sql, params=None):
        self.statements.append(str(sql))
        self.params.append(dict(params or {}))
        text = str(sql)
        # Side queries: main search is the only statement reading "knowledges".
        if '"chunks"' in text:
            return [
                {"kid": r.get("kid"), "body": r.get("body")} for r in self._chunk_rows
            ]
        if "knowledge_tag_relations" in text and '"knowledges"' not in text:
            return [
                {"kid": r.get("id"), "tag_name": r.get("tag_name")} for r in self.rows
            ]
        from app.services.db import _jsonable

        return [{k: _jsonable(v) for k, v in row.items()} for row in self.rows]

    @property
    def main_sql(self) -> str:
        # Prefer the primary knowledge search (not the tag batch-fetch / information_schema).
        for s in reversed(self.statements):
            if "SELECT" in s and "information_schema" not in s and '"knowledges"' in s:
                return s
        return next(
            (
                s
                for s in reversed(self.statements)
                if "SELECT" in s and "information_schema" not in s
            ),
            "",
        )

    @property
    def main_params(self) -> dict:
        for i in range(len(self.statements) - 1, -1, -1):
            s = self.statements[i]
            if "SELECT" in s and "information_schema" not in s and '"knowledges"' in s:
                return self.params[i]
        return self.params[-1] if self.params else {}


def _row(idx: str, metas: dict, **extra):
    base = {
        "id": idx,
        "title": f"doc-{idx}",
        "kb_name": "产品知识库",
        "tag_name": "技术文档",
        "_metas": metas,
        "metadata": {"content": f"body-of-{idx}"},
    }
    base.update(extra)
    return base


@pytest.fixture()
def config():
    from app.config import get_config

    return get_config()


@pytest.mark.asyncio
async def test_search_generates_postgres_sql(config):
    rows = [_row("1", {"level": 3})]
    executor = RecordingExecutor(rows)
    result = await MetasSearchService(executor, config).search(
        SearchRequest(query="level >= 3")
    )

    sql = executor.main_sql
    assert 'FROM "knowledges" k' in sql
    assert 'k."custom_metadata" AS "_metas"' in sql
    assert 'LEFT JOIN "knowledge_bases" kb' in sql
    assert 'LEFT JOIN "knowledge_tags" tag' in sql
    assert 'k."deleted_at" IS NULL' in sql  # soft-deleted rows are hidden by default
    assert "-> 'level'" in sql  # jsonb pushdown (text + jsonb form)
    assert "ORDER BY" in sql and "LIMIT" in sql
    assert result.total == 1
    assert result.rows[0]["id"] == "1"


@pytest.mark.asyncio
async def test_search_filters_exactly_in_python(config):
    executor = RecordingExecutor([_row("1", {"level": 3}), _row("2", {"level": 1})])
    result = await MetasSearchService(executor, config).search(
        SearchRequest(query="level >= 3")
    )
    assert [row["id"] for row in result.rows] == ["1"]
    assert result.total == 1 and result.scanned == 2


@pytest.mark.asyncio
async def test_search_paginates(config):
    rows = [_row(str(i), {"level": 5}) for i in range(1, 8)]
    executor = RecordingExecutor(rows)
    result = await MetasSearchService(executor, config).search(
        SearchRequest(query="level = 5", page=2, page_size=3)
    )
    assert result.total == 7
    assert result.has_more is True
    assert [row["id"] for row in result.rows] == ["4", "5", "6"]


@pytest.mark.asyncio
async def test_search_kb_filter(config):
    executor = RecordingExecutor([_row("1", {"level": 3})])
    await MetasSearchService(executor, config).search(
        SearchRequest(query="level = 3", kb_ids=["kb-x"])
    )
    last_params = executor.main_params
    assert last_params["forge_kb_id_0"] == "kb-x"
    assert 'k."deleted_at" IS NULL' in executor.main_sql


@pytest.mark.asyncio
async def test_result_column_can_come_from_metas(config):
    from app.config import get_config

    from tests.conftest import write_config

    write_config({"metas_search": {"result_column": ["id", "level", "kb_name"]}})
    try:
        executor = RecordingExecutor([_row("1", {"level": 3})])
        result = await MetasSearchService(executor, get_config()).search(
            SearchRequest(query="level = 3")
        )
        assert 'AS "level"' in executor.main_sql
        # result_column columns + metas / tag_names enrichment
        assert set(result.rows[0]) == {"id", "level", "kb_name", "metas", "tag_names"}
        assert result.rows[0]["metas"] == {"level": 3}
        assert isinstance(result.rows[0]["tag_names"], list)
    finally:
        write_config(
            {
                "metas_search": {
                    "result_column": [
                        "id",
                        "title",
                        "file_name",
                        "kb_name",
                        "tag_name",
                    ]
                }
            }
        )


@pytest.mark.asyncio
async def test_missing_metadata_column_is_reported(config):
    class NoMetasExecutor(FakeExecutor):
        def __init__(self):
            super().__init__(columns={"knowledges": {"id", "title"}})

    from app.errors import ForgeError

    with pytest.raises(ForgeError) as excinfo:
        await MetasSearchService(NoMetasExecutor(), config).search(
            SearchRequest(query="level = 3")
        )
    assert excinfo.value.error_id == "METADATA_COLUMN_MISSING"


@pytest.mark.asyncio
async def test_rows_are_json_serialisable(config):
    executor = RecordingExecutor([_row("1", {"tags": ["a", "b"], "nested": {"x": 1}})])
    result = await MetasSearchService(executor, config).search(
        SearchRequest(query="level = 3")
    )
    json.dumps(result.rows, ensure_ascii=False)


@pytest.mark.asyncio
async def test_metas_response_returns_full_blob(config):
    """Response metas is the full custom_metadata, not projected to query fields."""
    full = {
        "level": 3,
        "category": "ops",
        "hashcode": "abc",
        "author": {"name": "bruce", "email": "b@example.com"},
    }
    executor = RecordingExecutor([_row("1", full)])
    result = await MetasSearchService(executor, config).search(
        SearchRequest(query="level >= 3 AND author.name = 'bruce'")
    )
    assert result.rows[0]["metas"] == full
    assert "category" in result.rows[0]["metas"]
    assert "hashcode" in result.rows[0]["metas"]
    assert result.rows[0]["metas"]["author"]["email"] == "b@example.com"


@pytest.mark.asyncio
async def test_return_content_defaults_off(config):
    executor = RecordingExecutor([_row("1", {"level": 3})])
    result = await MetasSearchService(executor, config).search(
        SearchRequest(query="level = 3")
    )
    assert "content" not in result.rows[0]
    assert "->> 'content'" not in executor.main_sql


@pytest.mark.asyncio
async def test_return_content_projects_metadata_body(config):
    # FakeExecutor does not evaluate SQL: the scripted row carries the projected
    # content column exactly as PostgreSQL would return it.
    executor = RecordingExecutor([_row("1", {"level": 3}, content="body-of-1")])
    result = await MetasSearchService(executor, config).search(
        SearchRequest(query="level = 3", return_content=True)
    )
    assert "->> 'content'" in executor.main_sql
    assert result.rows[0]["content"] == "body-of-1"


@pytest.mark.asyncio
async def test_return_content_falls_back_to_chunks(config):
    row = _row("1", {"level": 3}, content="")  # metadata content empty
    executor = RecordingExecutor(
        [row],
        chunk_rows=[{"kid": "1", "body": "chunk-body"}],
    )
    result = await MetasSearchService(executor, config).search(
        SearchRequest(query="level = 3", return_content=True)
    )
    assert result.rows[0]["content"] == "chunk-body"
