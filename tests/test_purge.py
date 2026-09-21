"""Purge tests (no PostgreSQL needed - a recording executor stands in for it)."""

from __future__ import annotations

import pytest

from app.services.db import FakeExecutor
from app.services.purge_service import PurgeRequest, PurgeService, resolve_request

KB_COLUMNS = {"id", "name", "deleted_at"}
KN_COLUMNS = {"id", "knowledge_base_id", "deleted_at", "title", "updated_at", "custom_metadata"}
EMBEDDING_COLUMNS = {"id", "knowledge_base_id", "knowledge_id", "embedding"}
CHUNK_COLUMNS = {"id", "knowledge_base_id", "knowledge_id", "content"}
MISSING_TABLE_COLUMNS: set = set()


class RecordingExecutor(FakeExecutor):
    """Answers COUNT/SELECT/DELETE per table so the cascade order can be asserted."""

    def __init__(self, select_rows=None, counts=None, **tables):
        super().__init__(columns=tables)
        self.select_rows = {k: [{"id": v} for v in vs] for k, vs in (select_rows or {}).items()}
        self.counts = counts or {}
        self.executed: list = []
        self.params_log: list = []

    async def fetch(self, sql, params=None):  # noqa: ANN001
        statement = str(sql)
        self.statements.append(statement)
        self.params_log.append(dict(params or {}))
        if "COUNT(*)" in statement:
            return [{"count": self.counts.get(_table_of(statement), 0)}]
        return self.select_rows.get(_table_of(statement), [])

    async def execute(self, sql, params=None):  # noqa: ANN001
        self.executed.append(str(sql))
        self.params_log.append(dict(params or {}))
        return self.counts.get(_table_of(str(sql)), 0)


def _table_of(statement: str) -> str:
    import re

    match = re.search(r'FROM "([^"]+)"', statement)
    return match.group(1) if match else ""


def _columns():
    return {
        "knowledge_bases": KB_COLUMNS,
        "knowledges": KN_COLUMNS,
        "embeddings": EMBEDDING_COLUMNS,
        "chunks": CHUNK_COLUMNS,
        "chunk_revisions": CHUNK_COLUMNS,
        "knowledge_tag_relations": {"id", "knowledge_id", "tag_id"},
        "knowledge_processing_spans": {"id", "knowledge_id"},
        "wiki_page_revisions": MISSING_TABLE_COLUMNS,
        "wiki_page_issues": MISSING_TABLE_COLUMNS,
        "wiki_pages": MISSING_TABLE_COLUMNS,
        "wiki_folders": MISSING_TABLE_COLUMNS,
        "data_sources": MISSING_TABLE_COLUMNS,
        "kb_shares": {"id", "knowledge_base_id"},
        "knowledge_tags": {"id", "knowledge_base_id", "name"},
    }


@pytest.fixture()
def config():
    from app.config import get_config

    return get_config()


@pytest.mark.asyncio
async def test_purge_dry_run_never_deletes(config):
    executor = RecordingExecutor(
        select_rows={"knowledge_bases": ["kb-old"], "knowledges": ["kn-old"]},
        counts={"embeddings": 12, "chunks": 3, "knowledges": 1, "knowledge_bases": 1},
        **_columns(),
    )
    report = await PurgeService(executor, config).run(PurgeRequest(retention_days=30, dry_run=True))
    assert executor.executed == []
    assert report.knowledge_base_count == 1
    assert report.knowledge_count == 1
    assert report.matched["embeddings"] == 12
    assert report.deleted["embeddings"] == 0


@pytest.mark.asyncio
async def test_purge_cascade_order(config):
    executor = RecordingExecutor(
        select_rows={"knowledge_bases": ["kb-old"], "knowledges": ["kn-old"]},
        counts={"embeddings": 12, "chunks": 3, "knowledges": 1, "knowledge_bases": 1},
        **_columns(),
    )
    await PurgeService(executor, config).run(PurgeRequest(retention_days=7, dry_run=False))
    tables = [_table_of(sql) for sql in executor.executed if sql.startswith("DELETE")]
    # children first, knowledge_bases absolutely last
    assert tables.index("embeddings") < tables.index("knowledges")
    assert tables.index("knowledges") < tables.index("knowledge_bases")
    assert tables[-1] == "knowledge_bases"


@pytest.mark.asyncio
async def test_purge_counts_are_reported(config):
    executor = RecordingExecutor(
        select_rows={"knowledge_bases": ["kb-old"], "knowledges": ["kn-old"]},
        counts={"embeddings": 12, "knowledges": 1, "knowledge_bases": 1},
        **_columns(),
    )
    report = await PurgeService(executor, config).run(PurgeRequest(retention_days=7, dry_run=False))
    assert report.knowledge_base_count == 1
    assert report.knowledge_count == 1
    assert report.deleted["knowledge_bases"] == 1
    assert report.deleted["embeddings"] == 12


@pytest.mark.asyncio
async def test_purge_orphan_sweep_only_runs_when_requested(config):
    executor = RecordingExecutor(counts={"embeddings": 5, "chunks": 2}, **_columns())
    report = await PurgeService(executor, config).run(
        PurgeRequest(retention_days=30, include_embed=True, dry_run=False)
    )
    assert any("NOT EXISTS" in sql for sql in executor.executed)
    assert report.orphan_matched["embeddings (orphan)"] == 5
    assert report.orphan_deleted["chunks (orphan)"] == 2

    clean = RecordingExecutor(counts={"embeddings": 5}, **_columns())
    report = await PurgeService(clean, config).run(
        PurgeRequest(retention_days=30, include_embed=False, dry_run=False)
    )
    assert not any("NOT EXISTS" in sql for sql in clean.executed)
    assert report.orphan_matched == {}


@pytest.mark.asyncio
async def test_purge_skips_unknown_tables(config):
    executor = RecordingExecutor(
        select_rows={"knowledge_bases": ["kb-old"], "knowledges": ["kn-old"]}, **_columns()
    )
    report = await PurgeService(executor, config).run(PurgeRequest(retention_days=1, dry_run=True))
    assert "wiki_pages" in report.skipped_tables


def test_resolve_request_falls_back_to_config(config):
    req = resolve_request(retention_days=None, include_embed=None, config=config)
    assert req.retention_days == config.purge.default_retention_days
    assert req.include_embed == config.purge.include_embed
    assert req.dry_run == config.purge.dry_run

    req = resolve_request(retention_days=90, include_embed=True, config=config, dry_run=False)
    assert (req.retention_days, req.include_embed, req.dry_run) == (90, True, False)


def test_resolve_request_rejects_negative_days(config):
    from app.errors import ForgeError

    with pytest.raises(ForgeError):
        resolve_request(retention_days=-1, include_embed=None, config=config)
