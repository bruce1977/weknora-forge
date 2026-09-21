"""End-to-end wiring tests for the v2 endpoints.

The upstream is mocked with respx and PostgreSQL with a recording executor injected
through ``app.dependency_overrides``, so these tests exercise routing, auth, the
response envelope and query-parameter handling exactly like a real deployment.
"""

from __future__ import annotations

from contextlib import ExitStack, asynccontextmanager

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.deps import get_db
from app.main import create_app
from app.services.db import FakeExecutor
from tests.conftest import VALIDATE_URL, auth_headers


class FakeDatabase:
    """Minimal stand-in for app.services.db.Database."""

    def __init__(self, executor):
        self.executor = executor
        self.configured = True

    @asynccontextmanager
    async def transaction(self):
        yield self.executor

    async def ping(self) -> bool:
        return True


class SearchExecutor(FakeExecutor):
    def __init__(self):
        super().__init__(
            rows=[{"id": "kn-1", "title": "deployment guide", "similarity": None, "_metas": {"level": 3}}],
            columns={
                "knowledges": {
                    "id", "knowledge_base_id", "title", "custom_metadata",
                    "updated_at", "created_at", "deleted_at",
                },
                "knowledge_bases": {"id", "name"},
                "knowledge_tags": {"id", "name"},
                "knowledge_tag_relations": {"knowledge_id", "tag_id"},
            },
        )


@pytest.fixture()
def build_app():
    """Create an app with a running lifespan and a faked database."""
    with ExitStack() as stack:

        def _make(executor):
            app = create_app()
            app.dependency_overrides[get_db] = lambda: FakeDatabase(executor)
            return app, stack.enter_context(TestClient(app))

        yield _make


@respx.mock
def test_purge_endpoint_accepts_query_parameters(build_app):
    from app.config import get_config

    executor = FakeExecutor(columns={"knowledge_bases": {"id", "deleted_at"}, "knowledges": {"id", "deleted_at"}})
    app, client = build_app(executor)
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={"success": True, "data": []}))

    path = "/api/v2/management/purge?retention_days=7&include_embed=true"
    resp = client.delete(path, headers=auth_headers("DELETE", path))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["retention_days"] == 7
    assert data["include_embed"] is True
    assert data["dry_run"] is get_config().purge.dry_run
    assert "cutoff" in data and "counts" in data
    app.dependency_overrides.clear()


@respx.mock
def test_purge_endpoint_without_parameters_uses_config(build_app):
    from app.config import get_config

    executor = FakeExecutor(columns={"knowledge_bases": {"id", "deleted_at"}, "knowledges": {"id", "deleted_at"}})
    app, client = build_app(executor)
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={"success": True, "data": []}))

    path = "/api/v2/management/purge"
    resp = client.delete(path, headers=auth_headers("DELETE", path))
    assert resp.status_code == 200
    assert resp.json()["data"]["retention_days"] == get_config().purge.default_retention_days
    app.dependency_overrides.clear()


@respx.mock
def test_search_endpoint_post(build_app):
    executor = SearchExecutor()
    app, client = build_app(executor)
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={"success": True, "data": []}))

    path = "/api/v2/knowledge/search"
    resp = client.post(
        path,
        json={"metas_query": "level >= 3", "kb_id": "kb-1", "page": 1, "page_size": 20},
        headers=auth_headers("POST", path),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["total"] == 1
    assert body["data"]["rows"][0]["id"] == "kn-1"
    assert executor.params[-1]["forge_kb_id"] == "kb-1"
    app.dependency_overrides.clear()


@respx.mock
def test_search_endpoint_post_with_vector(build_app):
    executor = SearchExecutor()
    app, client = build_app(executor)
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={"success": True, "data": []}))

    path = "/api/v2/knowledge/search"
    resp = client.post(
        path,
        json={"metas_query": "level >= 3", "page": 1, "page_size": 5, "vector": [0.1, 0.2, 0.3]},
        headers=auth_headers("POST", path),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["page_size"] == 5
    assert data["similarity"] is True
    assert "forge_vector" in executor.params[-1]
    app.dependency_overrides.clear()


@respx.mock
def test_search_endpoint_post_with_title_and_tags(build_app):
    executor = SearchExecutor()
    app, client = build_app(executor)
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={"success": True, "data": []}))

    path = "/api/v2/knowledge/search"
    resp = client.post(
        path,
        json={"metas_query": "level >= 3", "title": "Milvus", "tags": ["ai", "db"]},
        headers=auth_headers("POST", path),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    app.dependency_overrides.clear()


@respx.mock
def test_database_outage_is_reported_as_upstream_error(build_app):
    class BrokenExecutor(FakeExecutor):
        def __init__(self):
            super().__init__(columns={"knowledges": {"id", "custom_metadata"}})

        async def fetch(self, sql, params=None):
            from app.errors import database_error

            raise database_error("connection refused")

    app, client = build_app(BrokenExecutor())
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={"success": True, "data": []}))

    path = "/api/v2/knowledge/search"
    resp = client.post(
        path,
        json={"metas_query": "level = 3"},
        headers=auth_headers("POST", path),
    )
    assert resp.status_code == 502
    assert resp.json()["error_id"] == "DATABASE_ERROR"
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/api/v2/management/purge"])
@respx.mock
def test_endpoints_require_second_factor(path, build_app):
    app, client = build_app(SearchExecutor())
    respx.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={"success": True, "data": []}))

    method = "DELETE" if "purge" in path else "POST"
    resp = client.request(method, path, headers={"X-API-Key": "sk-test-key"})
    assert resp.status_code == 401
    app.dependency_overrides.clear()
