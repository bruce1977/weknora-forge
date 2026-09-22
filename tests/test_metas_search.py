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
}
KB_COLUMNS = {"id", "name"}
TAG_COLUMNS = {"id", "name"}
RELATION_COLUMNS = {"knowledge_id", "tag_id"}
EMBEDDING_COLUMNS = {"id", "knowledge_id", "embedding"}


class RecordingExecutor(FakeExecutor):
    def __init__(self, rows):
        super().__init__(
            rows=rows,
            columns={
                "knowledges": KNOWLEDGE_COLUMNS,
                "knowledge_bases": KB_COLUMNS,
                "knowledge_tags": TAG_COLUMNS,
                "knowledge_tag_relations": RELATION_COLUMNS,
                "embeddings": EMBEDDING_COLUMNS,
            },
        )

    @property
    def main_sql(self) -> str:
        return next((s for s in reversed(self.statements) if "SELECT" in s and "information_schema" not in s), "")


def _row(idx: str, metas: dict, **extra):
    base = {
        "id": idx,
        "title": f"doc-{idx}",
        "similarity": None,
        "kb_name": "产品知识库",
        "tag_name": "技术文档",
        "_metas": metas,
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
    result = await MetasSearchService(executor, config).search(SearchRequest(query="level >= 3"))

    sql = executor.main_sql
    assert 'FROM "knowledges" k' in sql
    assert "k.\"custom_metadata\" AS \"_metas\"" in sql
    assert 'LEFT JOIN "knowledge_bases" kb' in sql
    assert 'LEFT JOIN "knowledge_tags" tag' in sql
    assert 'k."deleted_at" IS NULL' in sql          # soft-deleted rows are hidden by default
    assert "-> 'level'" in sql                      # jsonb pushdown (text + jsonb form)
    assert "ORDER BY" in sql and "LIMIT" in sql
    assert result.total == 1
    assert result.rows[0]["id"] == "1"


@pytest.mark.asyncio
async def test_search_filters_exactly_in_python(config):
    executor = RecordingExecutor([_row("1", {"level": 3}), _row("2", {"level": 1})])
    result = await MetasSearchService(executor, config).search(SearchRequest(query="level >= 3"))
    assert [row["id"] for row in result.rows] == ["1"]
    assert result.total == 1 and result.scanned == 2


@pytest.mark.asyncio
async def test_search_paginates(config):
    rows = [_row(str(i), {"level": 5}) for i in range(1, 8)]
    executor = RecordingExecutor(rows)
    result = await MetasSearchService(executor, config).search(SearchRequest(query="level = 5", page=2, page_size=3))
    assert result.total == 7
    assert result.has_more is True
    assert [row["id"] for row in result.rows] == ["4", "5", "6"]


@pytest.mark.asyncio
async def test_search_kb_filter_and_include_deleted(config):
    executor = RecordingExecutor([_row("1", {"level": 3})])
    await MetasSearchService(executor, config).search(SearchRequest(query="level = 3", kb_ids=["kb-x"]))
    last_params = executor.params[-1]
    assert last_params["forge_kb_ids"] == ["kb-x"]
    assert 'k."deleted_at" IS NULL' in executor.main_sql

    executor = RecordingExecutor([_row("1", {"level": 3})])
    await MetasSearchService(executor, config).search(
        SearchRequest(query="level = 3", include_deleted=True)
    )
    assert 'k."deleted_at" IS NULL' not in executor.main_sql


@pytest.mark.asyncio
async def test_vector_adds_lateral_scoring(config):
    executor = RecordingExecutor([_row("1", {"level": 3}, similarity=0.87)])
    result = await MetasSearchService(executor, config).search(
        SearchRequest(query="level = 3", vector=[0.1, 0.2, 0.3])
    )
    sql = executor.main_sql
    assert "LEFT JOIN LATERAL" in sql
    assert "MIN(vec.\"embedding\" <=> CAST(:forge_vector AS vector))" in sql
    assert "1 - (vec_agg._distance)" in sql
    assert executor.params[-1]["forge_vector"].startswith("[")
    assert result.similarity is True
    assert result.rows[0]["similarity"] == 0.87


@pytest.mark.asyncio
async def test_result_column_can_come_from_metas(config):
    from app.config import get_config

    from tests.conftest import write_config

    write_config({"metas_search": {"result_column": ["id", "level", "kb_name"]}})
    try:
        executor = RecordingExecutor([_row("1", {"level": 3})])
        result = await MetasSearchService(executor, get_config()).search(SearchRequest(query="level = 3"))
        assert 'AS "level"' in executor.main_sql
        assert set(result.rows[0]) == {"id", "level", "kb_name"}
    finally:
        write_config(
            {"metas_search": {"result_column": ["id", "title", "file_name", "similarity", "kb_name", "tag_name"]}}
        )


@pytest.mark.asyncio
async def test_missing_metadata_column_is_reported(config):
    class NoMetasExecutor(FakeExecutor):
        def __init__(self):
            super().__init__(columns={"knowledges": {"id", "title"}})

    from app.errors import ForgeError

    with pytest.raises(ForgeError) as excinfo:
        await MetasSearchService(NoMetasExecutor(), config).search(SearchRequest(query="level = 3"))
    assert excinfo.value.error_id == "METADATA_COLUMN_MISSING"


@pytest.mark.asyncio
async def test_rows_are_json_serialisable(config):
    executor = RecordingExecutor([_row("1", {"tags": ["a", "b"], "nested": {"x": 1}})])
    result = await MetasSearchService(executor, config).search(SearchRequest(query="level = 3"))
    json.dumps(result.rows, ensure_ascii=False)
