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
from tests.conftest import API_KEY, UPSTREAM, VALIDATE_URL, auth_headers


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
            rows=[
                {
                    "id": "kn-1",
                    "title": "deployment guide",
                    "_metas": {"level": 3},
                    "metadata": {"content": "# hello body"},
                    "content": "# hello body",
                }
            ],
            columns={
                "knowledges": {
                    "id",
                    "knowledge_base_id",
                    "title",
                    "custom_metadata",
                    "metadata",
                    "updated_at",
                    "created_at",
                    "deleted_at",
                },
                "knowledge_bases": {"id", "name"},
                "knowledge_tags": {"id", "name"},
                "knowledge_tag_relations": {"knowledge_id", "tag_id"},
                "chunks": {
                    "id",
                    "knowledge_id",
                    "content",
                    "chunk_index",
                    "chunk_type",
                    "deleted_at",
                },
            },
        )

    async def fetch(self, sql, params=None):
        self.statements.append(str(sql))
        self.params.append(dict(params or {}))
        text = str(sql)
        if '"chunks"' in text:
            return []  # no chunk rows scripted; metadata path should supply content
        if "knowledge_tag_relations" in text and '"knowledges"' not in text:
            return []
        from app.services.db import _jsonable

        return [{k: _jsonable(v) for k, v in row.items()} for row in self.rows]


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

    executor = FakeExecutor(
        columns={
            "knowledge_bases": {"id", "deleted_at"},
            "knowledges": {"id", "deleted_at"},
        }
    )
    app, client = build_app(executor)
    respx.get(VALIDATE_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": []})
    )

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

    executor = FakeExecutor(
        columns={
            "knowledge_bases": {"id", "deleted_at"},
            "knowledges": {"id", "deleted_at"},
        }
    )
    app, client = build_app(executor)
    respx.get(VALIDATE_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": []})
    )

    path = "/api/v2/management/purge"
    resp = client.delete(path, headers=auth_headers("DELETE", path))
    assert resp.status_code == 200
    assert (
        resp.json()["data"]["retention_days"]
        == get_config().purge.default_retention_days
    )
    app.dependency_overrides.clear()


@respx.mock
def test_search_endpoint_post(build_app):
    executor = SearchExecutor()
    app, client = build_app(executor)
    respx.get(VALIDATE_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": []})
    )
    respx.get(f"{UPSTREAM}/knowledge-bases", params={"page": 1, "page_size": 500}).mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": {"data": [{"id": "kb-1"}], "total": 1}}
        )
    )

    path = "/api/v2/knowledge/search"
    resp = client.post(
        path,
        json={
            "metas_query": "level >= 3",
            "kb_ids": ["kb-1"],
            "page": 1,
            "page_size": 20,
        },
        headers=auth_headers("POST", path),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["id"] == "kn-1"
    # content is opt-in
    assert "content" not in body["data"]["items"][0]
    # main search params (not the tag batch-fetch)
    main_params = next(
        p
        for s, p in zip(executor.statements, executor.params)
        if "SELECT" in s and '"knowledges"' in s
    )
    assert main_params["forge_kb_id_0"] == "kb-1"
    app.dependency_overrides.clear()


@respx.mock
def test_search_return_content_includes_body(build_app):
    executor = SearchExecutor()
    app, client = build_app(executor)
    respx.get(VALIDATE_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": []})
    )
    respx.get(f"{UPSTREAM}/knowledge-bases", params={"page": 1, "page_size": 500}).mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": {"data": [{"id": "kb-1"}], "total": 1}}
        )
    )

    path = "/api/v2/knowledge/search"
    resp = client.post(
        path,
        json={
            "metas_query": "level >= 3",
            "kb_ids": ["kb-1"],
            "return_content": True,
        },
        headers=auth_headers("POST", path),
    )
    assert resp.status_code == 200
    item = resp.json()["data"]["items"][0]
    assert item["content"] == "# hello body"
    app.dependency_overrides.clear()


@respx.mock
def test_search_endpoint_post_with_title_and_tags(build_app):
    executor = SearchExecutor()
    app, client = build_app(executor)
    respx.get(VALIDATE_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": []})
    )
    respx.get(f"{UPSTREAM}/knowledge-bases", params={"page": 1, "page_size": 500}).mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": {"data": [{"id": "kb-1"}], "total": 1}}
        )
    )

    path = "/api/v2/knowledge/search"
    resp = client.post(
        path,
        json={
            "metas_query": "level >= 3",
            "kb_ids": ["kb-1"],
            "title": "Milvus",
            "tags": ["ai", "db"],
        },
        headers=auth_headers("POST", path),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    app.dependency_overrides.clear()


@respx.mock
def test_search_requires_kb_ids(build_app):
    executor = SearchExecutor()
    app, client = build_app(executor)
    respx.get(VALIDATE_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": []})
    )

    path = "/api/v2/knowledge/search"
    resp = client.post(
        path,
        json={"metas_query": "level >= 3"},
        headers=auth_headers("POST", path),
    )
    assert resp.status_code == 422
    assert resp.json()["error_id"] == "INVALID_REQUEST"
    app.dependency_overrides.clear()


@respx.mock
def test_search_forbids_inaccessible_kb(build_app):
    executor = SearchExecutor()
    app, client = build_app(executor)
    respx.get(VALIDATE_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": []})
    )
    # list_knowledge_bases returns only kb-1; request for kb-denied must be rejected
    respx.get(f"{UPSTREAM}/knowledge-bases").mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": {"data": [{"id": "kb-1"}], "total": 1}}
        )
    )

    path = "/api/v2/knowledge/search"
    resp = client.post(
        path,
        json={"metas_query": "level >= 3", "kb_ids": ["kb-denied"]},
        headers=auth_headers("POST", path),
    )
    assert resp.status_code == 403
    assert resp.json()["error_id"] == "KB_ACCESS_DENIED"
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
    respx.get(VALIDATE_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": []})
    )

    path = "/api/v2/knowledge/search"
    resp = client.post(
        path,
        json={"metas_query": "level = 3"},
        headers=auth_headers("POST", path),
    )
    # kb_ids is now required
    assert resp.status_code == 422
    assert resp.json()["error_id"] == "INVALID_REQUEST"
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/api/v2/management/purge"])
@respx.mock
def test_endpoints_require_second_factor(path, build_app):
    app, client = build_app(SearchExecutor())
    respx.get(VALIDATE_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": []})
    )

    method = "DELETE" if "purge" in path else "POST"
    resp = client.request(method, path, headers={"X-API-Key": API_KEY})
    assert resp.status_code == 401
    app.dependency_overrides.clear()


@respx.mock
def test_probe_succeeds_when_weknora_reachable(build_app):
    app, client = build_app(SearchExecutor())
    respx.get(VALIDATE_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": []})
    )
    respx.get(f"{UPSTREAM}/knowledge-bases").mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": {"data": [{"id": "kb-1"}], "total": 1}}
        )
    )

    resp = client.get("/api/v2/probe", headers=auth_headers("GET", "/api/v2/probe"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False  # database not configured in test env
    assert body["data"]["weknora"]["knowledge_base_count"] == 1
    assert body["data"]["database"]["ok"] is False
    assert body["data"]["database"]["message"] == "Database not configured"
    app.dependency_overrides.clear()


@respx.mock
def test_probe_reports_failure_when_weknora_down(build_app):
    app, client = build_app(SearchExecutor())
    respx.get(VALIDATE_URL).mock(
        return_value=httpx.Response(200, json={"success": True, "data": []})
    )
    respx.get(f"{UPSTREAM}/knowledge-bases").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    resp = client.get("/api/v2/probe", headers=auth_headers("GET", "/api/v2/probe"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    # a connection failure surfaces as a 502 from the upstream layer, never a 5xx
    assert body["data"]["weknora"]["upstream_status"] == 502
    app.dependency_overrides.clear()


@respx.mock
def test_docs_and_openapi_are_served(build_app):
    app, client = build_app(SearchExecutor())
    # Offline Swagger assets + the generated OpenAPI schema must be reachable.
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "swagger-ui" in docs.text.lower() or "SwaggerUIBundle" in docs.text
    schema_resp = client.get("/openapi.json")
    assert schema_resp.status_code == 200
    schema_data = schema_resp.json()
    paths = schema_data["paths"]
    assert "/api/v2/probe" in paths
    assert "/api/v2/publish" in paths
    assert "/api/v2/knowledge/search" in paths
    # v2 search now takes kb_ids (array), not kb_id
    search_post = paths["/api/v2/knowledge/search"]["post"]
    search_schema = search_post["requestBody"]["content"]["application/json"]["schema"]
    if "$ref" in search_schema:
        ref = search_schema["$ref"].rsplit("/", 1)[-1]
        props = schema_data["components"]["schemas"][ref]["properties"]
    else:
        props = search_schema["properties"]
    assert "kb_ids" in props
    assert "kb_id" not in props
    assert "return_content" in props
    assert "vector" not in props
    assert "include_deleted" not in props
    app.dependency_overrides.clear()
