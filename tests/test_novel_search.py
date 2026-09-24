"""Live integration test: novel part/chapter metadata search scenario.

Metadata model (all numeric):
  - part:    integer, e.g. 1, 2 (篇)
  - chapter: integer, e.g. 1..14 (回)
  - novel:   string,  e.g. "风雪江湖"

Run with:  pytest tests/test_novel_search.py -m live
The paired api_secret must be registered for FORGE_API_KEY via
WEKNORA_API_KEY/WEKNORA_API_SECRET or data/keys.json (app/keystore.py).
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
# Skip when env is not configured
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
from app.keystore import keystore  # noqa: E402
from app.main import create_app  # noqa: E402

API_KEY = os.environ["FORGE_API_KEY"]
KB_ID = os.environ["FORGE_KB_ID"]
PUBLISH_PATH = "/api/v2/publish"
SEARCH_PATH = "/api/v2/knowledge/search"

_REAL_CONFIG = str(Path(__file__).resolve().parent.parent / "data" / "config.json")
_TS = int(time.time())


def _sign(method: str, path: str, key: str = API_KEY) -> str:
    secret = keystore.get_secret(key)
    if not secret:
        raise RuntimeError(
            f"no api_secret registered for {key!r}: set WEKNORA_API_KEY/"
            "WEKNORA_API_SECRET or add the pair to data/keys.json"
        )
    return hmac.new(
        secret.encode(), f"{method}{path}".encode(), hashlib.sha256
    ).hexdigest()


def _pub_headers():
    return {
        "X-API-Key": API_KEY,
        "X-Forge-Signature": _sign("POST", PUBLISH_PATH),
        "Content-Type": "application/json",
    }


def _search_headers():
    return {
        "X-API-Key": API_KEY,
        "X-Forge-Signature": _sign("POST", SEARCH_PATH),
        "Content-Type": "application/json",
    }


def publish(app, title, content, *, tag_names=None, custom_metas=None) -> dict:
    body: dict = {"kb_id": KB_ID, "title": title, "content": content}
    if tag_names:
        body["tag_names"] = tag_names
    if custom_metas:
        body["custom_metas"] = custom_metas
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post(PUBLISH_PATH, json=body, headers=_pub_headers())
    return r.json()


def search(app, metas_query, *, title=None, tags=None, page=1, page_size=50) -> dict:
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
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post(SEARCH_PATH, json=body, headers=_search_headers())
    return r.json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
NOVEL_CHAPTERS = [
    # ── 风雪江湖 · 第1篇 (part=1, chapters 1-8) ──
    {
        "title": "第1回 风雪初度",
        "content": "话说天下大势，分久必合。风雪之夜，少年仗剑出山。",
        "part": 1,
        "chapter": 1,
        "novel": "风雪江湖",
    },
    {
        "title": "第2回 古寺惊变",
        "content": "行至深山古寺，忽闻禅唱声声。老僧合掌相迎。",
        "part": 1,
        "chapter": 2,
        "novel": "风雪江湖",
    },
    {
        "title": "第3回 剑气纵横",
        "content": "少年拔剑出鞘，剑光如虹。老僧递过一卷古籍。",
        "part": 1,
        "chapter": 3,
        "novel": "风雪江湖",
    },
    {
        "title": "第4回 夜探敌营",
        "content": "月黑风高，少年施展轻功潜入敌营。探得虚实。",
        "part": 1,
        "chapter": 4,
        "novel": "风雪江湖",
    },
    {
        "title": "第5回 义结金兰",
        "content": "途中遇一豪杰，二人意气相投，结为异姓兄弟。",
        "part": 1,
        "chapter": 5,
        "novel": "风雪江湖",
    },
    {
        "title": "第6回 生死相依",
        "content": "兄弟二人遭敌围困，背靠背血战至天明。",
        "part": 1,
        "chapter": 6,
        "novel": "风雪江湖",
    },
    {
        "title": "第7回 秘境寻宝",
        "content": "深入秘境，石壁上刻满上古符文。合力破解机关。",
        "part": 1,
        "chapter": 7,
        "novel": "风雪江湖",
    },
    {
        "title": "第8回 终局之战",
        "content": "最终决战来临。少年剑指苍穹，与宿敌在风雪中对决。",
        "part": 1,
        "chapter": 8,
        "novel": "风雪江湖",
    },
    # ── 风雪江湖 · 第2篇 (part=2, chapters 9-14) ──
    {
        "title": "第9回  新的征程",
        "content": "风雪过后，少年踏上了新的旅途。江湖路远。",
        "part": 2,
        "chapter": 9,
        "novel": "风雪江湖",
    },
    {
        "title": "第10回 暗流涌动",
        "content": "武林盟主大会将至，各方势力暗中角力。",
        "part": 2,
        "chapter": 10,
        "novel": "风雪江湖",
    },
    {
        "title": "第11回 宴上惊变",
        "content": "盟主宴上觥筹交错，忽然灯火尽灭。一声惨叫。",
        "part": 2,
        "chapter": 11,
        "novel": "风雪江湖",
    },
    {
        "title": "第12回 真相大白",
        "content": "少年查明真相，幕后黑手竟是旧识。人心难测。",
        "part": 2,
        "chapter": 12,
        "novel": "风雪江湖",
    },
    {
        "title": "第13回 决裂",
        "content": "兄弟因理念不同而决裂。曾经的誓言化为飞灰。",
        "part": 2,
        "chapter": 13,
        "novel": "风雪江湖",
    },
    {
        "title": "第14回 归隐",
        "content": "风波平息，少年携剑归隐山林。江湖再见。",
        "part": 2,
        "chapter": 14,
        "novel": "风雪江湖",
    },
    # ── 星辰志 · 卷一 (part=1, chapters 1-4, different novel) ──
    {
        "title": "第一回 天命之子",
        "content": "星辰璀璨之夜，天降异象。天命降世。",
        "part": 1,
        "chapter": 1,
        "novel": "星辰志",
    },
    {
        "title": "第二回 修炼入门",
        "content": "少年拜入仙门，灵气入体，踏上修炼之路。",
        "part": 1,
        "chapter": 2,
        "novel": "星辰志",
    },
    {
        "title": "第三回 初试锋芒",
        "content": "宗门大比，少年以低阶修为击败对手，一战成名。",
        "part": 1,
        "chapter": 3,
        "novel": "星辰志",
    },
    {
        "title": "第四回 秘境探险",
        "content": "进入宗门秘境，发现上古遗迹。星辰之力觉醒。",
        "part": 1,
        "chapter": 4,
        "novel": "星辰志",
    },
]


@pytest.fixture(scope="module")
def app():
    mock_config_path = os.environ.get("FORGE_CONFIG", "")
    os.environ["FORGE_CONFIG"] = _REAL_CONFIG
    load_config.cache_clear()
    get_config()
    keystore.reset()
    if not keystore.get_secret(API_KEY):
        pytest.skip(
            "Skipping live tests: no api_secret registered for FORGE_API_KEY "
            "(set WEKNORA_API_SECRET or data/keys.json)"
        )
    application = create_app()
    yield application
    os.environ["FORGE_CONFIG"] = mock_config_path
    load_config.cache_clear()
    get_config()
    keystore.reset()


@pytest.fixture(scope="module", autouse=True)
def _seed_novel_data(app):
    """Publish all chapters once for the module."""
    for ch in NOVEL_CHAPTERS:
        result = publish(
            app,
            ch["title"],
            ch["content"],
            tag_names=[ch["novel"]],
            custom_metas={
                "part": ch["part"],
                "chapter": ch["chapter"],
                "novel": ch["novel"],
                "batch": _TS,
            },
        )
        assert result.get("success"), f"Publish failed: {ch['title']} -> {result}"
    time.sleep(0.5)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestNovelPartChapterSearch:
    """Search chapters by part and chapter number using FMQ."""

    def test_all_chapters_in_part1(self, app):
        """All chapters in part=1 across both novels."""
        result = search(app, f"batch = {_TS} AND part = 1")
        assert result["success"]
        items = result["data"]["items"]
        # 风雪江湖 part1 has 8, 星辰志 part1 has 4 → total 12
        assert len(items) == 12

    def test_all_chapters_in_part2(self, app):
        """All chapters in part=2 (only 风雪江湖)."""
        result = search(app, f"batch = {_TS} AND part = 2")
        assert result["success"]
        items = result["data"]["items"]
        assert len(items) == 6

    def test_chapter_range_within_part(self, app):
        """Part 1, chapters 3-6 (连续区间)."""
        result = search(
            app, f"batch = {_TS} AND part = 1 AND chapter >= 3 AND chapter <= 6"
        )
        assert result["success"]
        items = result["data"]["items"]
        titles = [r.get("title", "") for r in items]
        # 风雪江湖: 第3,4,5,6回; 星辰志: 第三回, 第四回
        assert len(items) == 6
        assert any("第3回" in t for t in titles)
        assert any("第6回" in t for t in titles)
        assert any("第三回" in t for t in titles)

    def test_exact_chapter_in_part(self, app):
        """Exact chapter: part=1, chapter=5."""
        result = search(app, f"batch = {_TS} AND part = 1 AND chapter = 5")
        assert result["success"]
        items = result["data"]["items"]
        titles = [r.get("title", "") for r in items]
        assert len(items) >= 1
        assert any("第5回" in t for t in titles)

    def test_chapter_gt_threshold_in_part(self, app):
        """Part 2, chapters > 11 (连续尾部)."""
        result = search(app, f"batch = {_TS} AND part = 2 AND chapter > 11")
        assert result["success"]
        items = result["data"]["items"]
        titles = [r.get("title", "") for r in items]
        assert len(items) == 3  # 第12,13,14回
        assert any("第12回" in t for t in titles)
        assert any("第14回" in t for t in titles)

    def test_chapter_in_list(self, app):
        """Part 1, specific chapters via IN."""
        result = search(app, f"batch = {_TS} AND part = 1 AND chapter IN (1, 3, 5, 7)")
        assert result["success"]
        items = result["data"]["items"]
        # 风雪江湖: 第1,3,5,7回; 星辰志: 第一回,第三回
        assert len(items) == 6

    def test_novel_filter_plus_part(self, app):
        """Filter by novel name + part to narrow to one novel."""
        result = search(app, f"batch = {_TS} AND part = 1 AND novel = '风雪江湖'")
        assert result["success"]
        items = result["data"]["items"]
        assert len(items) == 8  # only 风雪江湖 part1
        titles = [r.get("title", "") for r in items]
        # verify consecutive chapters
        for i in range(1, 9):
            assert any(f"第{i}回" in t for t in titles), f"Chapter {i} missing"

    def test_consecutive_chapter_window(self, app):
        """Simulate 'reading window': part=2, chapters 10-12 (连续3回)."""
        result = search(
            app, f"batch = {_TS} AND part = 2 AND chapter >= 10 AND chapter <= 12"
        )
        assert result["success"]
        items = result["data"]["items"]
        titles = sorted([r.get("title", "") for r in items])
        assert len(items) == 3
        assert "第10回 暗流涌动" in titles
        assert "第11回 宴上惊变" in titles
        assert "第12回 真相大白" in titles

    def test_no_chapter_outside_range(self, app):
        """Part=1, chapters 100-200 → empty (beyond range)."""
        result = search(
            app, f"batch = {_TS} AND part = 1 AND chapter >= 100 AND chapter <= 200"
        )
        assert result["success"]
        assert result["data"]["total"] == 0

    def test_cross_novel_same_part(self, app):
        """Both novels share part=1 but have different chapter numbers."""
        result = search(app, f"batch = {_TS} AND part = 1 AND chapter = 1")
        assert result["success"]
        items = result["data"]["items"]
        titles = [r.get("title", "") for r in items]
        # 风雪江湖 第1回 + 星辰志 第一回
        assert len(items) == 2
        assert any("第1回" in t for t in titles)
        assert any("第一回" in t for t in titles)
