"""Live integration tests for POST /api/v2/knowledge/search.

These tests hit the real WeKnora upstream and PostgreSQL database.
Run with:  pytest tests/test_search_live.py -v
Requires environment variables:
  WEKNORA_BASE_URL  - e.g. http://weknora.internal
  FORGE_API_KEY     - WeKnora API key
  FORGE_KB_ID       - knowledge base id to seed/search
  DB_HOST / DB_PASSWORD (or FORGE_CONFIG pointing to data/config.json)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Skip entire module when env is not configured
# ---------------------------------------------------------------------------
if not os.environ.get("WEKNORA_BASE_URL"):
    pytest.skip(
        "Skipping live tests: WEKNORA_BASE_URL not set", allow_module_level=True
    )
if not os.environ.get("FORGE_API_KEY") or not os.environ.get("FORGE_KB_ID"):
    pytest.skip(
        "Skipping live tests: FORGE_API_KEY / FORGE_KB_ID not set",
        allow_module_level=True,
    )

from app.config import get_config, load_config  # noqa: E402
from app.main import create_app  # noqa: E402

API_KEY = os.environ["FORGE_API_KEY"]
KB_ID = os.environ["FORGE_KB_ID"]
PUBLISH_PATH = "/api/v2/publish"
SEARCH_PATH = "/api/v2/knowledge/search"

# Paths for config switching
_REAL_CONFIG = str(Path(__file__).resolve().parent.parent / "data" / "config.json")


def _sign(method: str, path: str, key: str = API_KEY) -> str:
    return hmac.new(
        key.encode(), f"{method}{path}".encode(), hashlib.sha256
    ).hexdigest()


def _headers(path: str) -> dict:
    return {
        "X-API-Key": API_KEY,
        "X-Forge-Signature": _sign("POST", path),
        "Content-Type": "application/json",
    }


def publish(app, title, content, tag_names=None, custom_metas=None) -> dict:
    body: dict = {"kb_id": KB_ID, "title": title, "content": content}
    if tag_names:
        body["tag_names"] = tag_names
    if custom_metas:
        body["custom_metas"] = custom_metas
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post(PUBLISH_PATH, json=body, headers=_headers(PUBLISH_PATH))
    return r.json()


def search(
    app,
    metas_query,
    *,
    title=None,
    tags=None,
    page=1,
    page_size=50,
    return_content=False,
) -> dict:
    body: dict = {
        "kb_ids": [KB_ID],
        "metas_query": metas_query,
        "page": page,
        "page_size": page_size,
    }
    if title:
        body["title"] = title
    if tags:
        body["tags"] = tags
    if return_content:
        body["return_content"] = True
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post(SEARCH_PATH, json=body, headers=_headers(SEARCH_PATH))
    return r.json()


# ---------------------------------------------------------------------------
# Module-level seed (runs once)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def app():
    # conftest.py already cached load_config(None) with the mock temp config.
    # Switch to the real config for the live test duration.
    mock_config_path = os.environ.get("FORGE_CONFIG", "")
    os.environ["FORGE_CONFIG"] = _REAL_CONFIG
    load_config.cache_clear()
    get_config()  # caches under None with the real config
    application = create_app()
    yield application
    # Restore: put the mock config back into the cache so unit tests are unaffected
    os.environ["FORGE_CONFIG"] = mock_config_path
    load_config.cache_clear()
    get_config()  # re-caches under None with the mock config


_TS = int(time.time())


@pytest.fixture(scope="module", autouse=True)
def _seed(app):
    """Publish test articles once for the whole module."""
    articles = [
        (
            "AI技术发展报告",
            "本文详细介绍了人工智能技术的最新发展趋势。",
            ["技术文档", "人工智能"],
            {"hashcode": f"live_a_{_TS}", "category": "ai", "level": 5},
        ),
        (
            "MySQL性能优化指南",
            "合理的索引策略可以显著提升查询性能。",
            ["技术文档", "数据库"],
            {"hashcode": f"live_b_{_TS}", "category": "db", "level": 3},
        ),
        (
            "智能客服产品说明书",
            "基于AI技术的智能客服系统，支持多轮对话。",
            ["产品文档", "人工智能"],
            {"hashcode": f"live_c_{_TS}", "category": "product", "level": 2},
        ),
        (
            "数据仓库建设方案",
            "采用分层架构，支持海量数据分析。",
            ["产品文档", "数据库"],
            {"hashcode": f"live_d_{_TS}", "category": "infra", "level": 4},
        ),
        (
            "PostgreSQL调优实战",
            "深入讲解PG性能优化。",
            ["技术文档"],
            {"hashcode": f"pg_{_TS}", "level": 4},
        ),
        ("标签测试A", "内容A", ["标签X"], {"hashcode": f"tag_a_{_TS}"}),
        ("标签测试B", "内容B", ["标签Y"], {"hashcode": f"tag_b_{_TS}"}),
        ("标签测试C", "内容C", ["标签Z"], {"hashcode": f"tag_c_{_TS}"}),
        (
            "去重测试",
            "同一篇文章两个标签",
            ["去重标签1", "去重标签2"],
            {"hashcode": f"dedup_{_TS}", "level": 1},
        ),
    ]
    for title, content, tags, metas in articles:
        result = publish(app, title, content, tag_names=tags, custom_metas=metas)
        assert result.get("success"), f"Publish failed: {title} -> {result}"
    time.sleep(1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestSearchByMetadata:
    def test_eq_string(self, app):
        result = search(app, f"hashcode = 'live_a_{_TS}'")
        assert result["success"]
        items = result["data"]["items"]
        assert len(items) >= 1
        assert any("AI" in (r.get("title") or "") for r in items)

    def test_eq_numeric(self, app):
        result = search(app, "level = 3")
        assert result["success"]
        items = result["data"]["items"]
        assert len(items) >= 1
        assert any("MySQL" in (r.get("title") or "") for r in items)

    def test_gt_numeric(self, app):
        result = search(app, "level > 3")
        assert result["success"]
        items = result["data"]["items"]
        titles = [r.get("title", "") for r in items]
        assert any("AI" in t for t in titles)
        assert any("数据仓库" in t for t in titles)

    def test_and_logic(self, app):
        result = search(app, f"hashcode = 'live_a_{_TS}' AND level >= 5")
        assert result["success"]
        assert len(result["data"]["items"]) >= 1

    def test_or_logic(self, app):
        result = search(app, "level = 2 OR level = 5")
        assert result["success"]
        titles = [r.get("title", "") for r in result["data"]["items"]]
        assert any("智能客服" in t for t in titles)
        assert any("AI" in t for t in titles)

    def test_not_exists(self, app):
        result = search(app, "NOT nonexistent_field EXISTS")
        assert result["success"]
        assert result["data"]["total"] >= 4

    def test_in_operator(self, app):
        result = search(app, "level IN (2, 4)")
        assert result["success"]
        titles = [r.get("title", "") for r in result["data"]["items"]]
        assert any("智能客服" in t for t in titles)
        assert any("数据仓库" in t for t in titles)

    def test_contains_string(self, app):
        result = search(app, "category CONTAINS 'db'")
        assert result["success"]
        assert len(result["data"]["items"]) >= 1


class TestSearchByTitle:
    def test_title_match(self, app):
        result = search(app, "$title EXISTS", title="PostgreSQL")
        assert result["success"]
        items = result["data"]["items"]
        assert any("PostgreSQL" in (r.get("title") or "") for r in items)

    def test_title_no_match(self, app):
        result = search(app, "$title EXISTS", title="不存在的文章标题XYZ")
        assert result["success"]
        assert result["data"]["total"] == 0


class TestSearchByTags:
    def test_single_tag(self, app):
        result = search(app, "$title EXISTS", tags=["标签X"])
        assert result["success"]
        items = result["data"]["items"]
        assert any("标签测试A" in (r.get("title") or "") for r in items)

    def test_multi_tag_or(self, app):
        result = search(app, "$title EXISTS", tags=["标签X", "标签Y"])
        assert result["success"]
        titles = [r.get("title", "") for r in result["data"]["items"]]
        has_a = any("标签测试A" in t for t in titles)
        has_b = any("标签测试B" in t for t in titles)
        assert has_a or has_b


class TestDeduplication:
    def test_no_duplicates(self, app):
        result = search(app, "$title EXISTS", tags=["去重标签1", "去重标签2"])
        assert result["success"]
        items = result["data"]["items"]
        ids = [r.get("id") for r in items]
        assert len(ids) == len(set(ids)), f"Duplicate IDs found: {ids}"


class TestReturnContent:
    def test_return_content_includes_body(self, app):
        result = search(app, f"hashcode = 'live_a_{_TS}'", return_content=True)
        assert result["success"]
        items = result["data"]["items"]
        assert items
        assert all("content" in r for r in items)
        assert any("人工智能" in (r.get("content") or "") for r in items)

    def test_return_content_default_omits_body(self, app):
        result = search(app, f"hashcode = 'live_a_{_TS}'")
        assert result["success"]
        items = result["data"]["items"]
        assert items
        assert all("content" not in r for r in items)
