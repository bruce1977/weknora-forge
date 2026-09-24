"""Publish endpoint tests: request/response contract and step ordering."""

from __future__ import annotations

import httpx
import respx

from tests.conftest import UPSTREAM, VALIDATE_URL, auth_headers

PUBLISH_PATH = "/api/v2/publish"


def _mock_weknora(router, *, fail_on_create: bool = False):
    """Wire the upstream endpoints the publish flow touches."""
    router.get(f"{UPSTREAM}/knowledge-bases/kb-1/tags").mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": {"data": [], "total": 0}}
        )
    )
    router.post(f"{UPSTREAM}/knowledge-bases/kb-1/tags").mock(
        return_value=httpx.Response(
            200, json={"success": True, "data": {"id": "tag-1", "name": "技术文档"}}
        )
    )
    create = router.post(f"{UPSTREAM}/knowledge-bases/kb-1/knowledge/manual")
    if fail_on_create:
        create.mock(
            return_value=httpx.Response(500, json={"error": {"message": "db down"}})
        )
    else:
        create.mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"id": "kn-1", "parse_status": "pending"},
                },
            )
        )
    router.get(f"{UPSTREAM}/knowledge/kn-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "id": "kn-1",
                    "custom_metadata": {"origin": "qa"},
                    "parse_status": "pending",
                },
            },
        )
    )
    router.put(f"{UPSTREAM}/knowledge/kn-1").mock(
        return_value=httpx.Response(200, json={"success": True, "data": {}})
    )
    router.put(f"{UPSTREAM}/knowledge/manual/kn-1").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "data": {"id": "kn-1", "enable_status": "enabled"}},
        )
    )


def test_publish_returns_only_success_and_knowledge_id(client):
    body = {
        "kb_id": "kb-1",
        "title": "部署手册",
        "content": "# 部署手册\n\n本文档描述了系统的部署流程，包括环境准备、配置步骤和验证方法。",
        "tag_names": ["技术文档"],
        "custom_metas": {"level": 3},
    }
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        _mock_weknora(router)
        resp = client.post(
            PUBLISH_PATH, json=body, headers=auth_headers("POST", PUBLISH_PATH)
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["knowledge_id"] == "kn-1"
    assert data["tag_ids"] == ["tag-1"]
    assert data["tag_names"] == ["技术文档"]
    # nothing else leaks into the success payload
    assert set(data) <= {
        "success",
        "knowledge_id",
        "tag_ids",
        "tag_names",
        "parse_status",
        "enable_status",
    }


def test_publish_writes_draft_then_metas_then_publish(client):
    body = {"kb_id": "kb-1", "title": "t", "content": "c", "custom_metas": {"level": 3}}
    order = []
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        _mock_weknora(router)

        def _record(request):
            payload = request.content.decode("utf-8")
            if "manual" in str(request.url):
                if request.method == "POST":
                    order.append("draft" if '"draft"' in payload else "create?")
                else:
                    order.append(
                        "publish" if '"publish"' in payload else "manual-update"
                    )
            else:
                order.append("metas")
            return httpx.Response(200, json={"success": True, "data": {"id": "kn-1"}})

        # Re-mock the two decisive endpoints so we can inspect the sequence
        router.post(f"{UPSTREAM}/knowledge-bases/kb-1/knowledge/manual").mock(
            side_effect=_record
        )
        router.put(f"{UPSTREAM}/knowledge/kn-1").mock(side_effect=_record)
        router.put(f"{UPSTREAM}/knowledge/manual/kn-1").mock(side_effect=_record)
        resp = client.post(
            PUBLISH_PATH, json=body, headers=auth_headers("POST", PUBLISH_PATH)
        )

    assert resp.status_code == 200
    assert order == ["draft", "metas", "publish"]


def test_publish_merges_existing_metas_when_configured(client):
    body = {"kb_id": "kb-1", "title": "t", "content": "c", "custom_metas": {"level": 3}}
    captured = {}
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        _mock_weknora(router)

        def _capture(request):
            import json

            captured.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"success": True, "data": {}})

        router.put(f"{UPSTREAM}/knowledge/kn-1").mock(side_effect=_capture)
        resp = client.post(
            PUBLISH_PATH, json=body, headers=auth_headers("POST", PUBLISH_PATH)
        )

    assert resp.status_code == 200
    # existing {"origin": "qa"} from GET /knowledge/kn-1 must be preserved
    assert captured["custom_metadata"] == {"origin": "qa", "level": 3}


def test_publish_failure_uses_error_envelope(client):
    body = {"kb_id": "kb-1", "title": "t", "content": "c"}
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        _mock_weknora(router, fail_on_create=True)
        resp = client.post(
            PUBLISH_PATH, json=body, headers=auth_headers("POST", PUBLISH_PATH)
        )

    assert resp.status_code == 502
    data = resp.json()
    assert data["success"] is False
    assert data["error_id"] == "UPSTREAM_ERROR"
    assert isinstance(data["error_message"], str) and data["error_message"]


def test_publish_requires_known_fields(client):
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        resp = client.post(
            PUBLISH_PATH,
            json={"title": "no kb"},
            headers=auth_headers("POST", PUBLISH_PATH),
        )
    assert resp.status_code == 422
    assert resp.json()["error_id"] == "INVALID_REQUEST"


def _post_invalid(client, body):
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        resp = client.post(
            PUBLISH_PATH, json=body, headers=auth_headers("POST", PUBLISH_PATH)
        )
    return resp


def test_publish_title_over_200_chars(client):
    """TC-10.1: title longer than 200 chars -> 422 with actual length."""
    resp = _post_invalid(client, {"kb_id": "kb-1", "title": "标" * 201, "content": "c"})
    assert resp.status_code == 422
    data = resp.json()
    assert data["error_id"] == "INVALID_REQUEST"
    err = next(d for d in data["details"] if "title" in d["loc"])
    assert err["type"] == "string_too_long"
    assert err["ctx"]["max_length"] == 200
    assert err["ctx"]["actual_length"] == 201
    assert "actual: 201" in err["msg"]
    assert "input" not in err


def test_publish_content_over_20000_chars(client):
    """TC-10.2: content longer than 20000 chars -> 422, body not echoed back."""
    content = "汉" * 20001
    resp = _post_invalid(client, {"kb_id": "kb-1", "title": "t", "content": content})
    assert resp.status_code == 422
    data = resp.json()
    assert data["error_id"] == "INVALID_REQUEST"
    err = next(d for d in data["details"] if "content" in d["loc"])
    assert err["type"] == "string_too_long"
    assert err["ctx"]["max_length"] == 20000
    assert err["ctx"]["actual_length"] == 20001
    assert "actual: 20001" in err["msg"]
    # the oversized article must not be echoed in the error envelope
    assert "input" not in err
    assert content not in resp.text


def test_publish_empty_title_or_content(client):
    """TC-10.3: empty title / content -> 422 (min_length=1)."""
    for field in ("title", "content"):
        body = {"kb_id": "kb-1", "title": "t", "content": "c"}
        body[field] = ""
        resp = _post_invalid(client, body)
        assert resp.status_code == 422, field
        data = resp.json()
        assert data["error_id"] == "INVALID_REQUEST"
        err = next(d for d in data["details"] if field in d["loc"])
        assert err["type"] == "string_too_short"
        assert err["ctx"]["min_length"] == 1
        assert "input" not in err


def test_publish_ignores_removed_request_parameters(client):
    """wait / wait_until / timeout / idempotency_key are not part of the API surface.

    Wait behaviour moved to config.json plus the new per-request ``sync`` flag.
    """
    body = {
        "kb_id": "kb-1",
        "title": "t",
        "content": "c",
        "wait": False,
        "wait_until": "completed",
        "timeout": 1,
        "idempotency_key": "k-1",
    }
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        _mock_weknora(router)
        resp = client.post(
            PUBLISH_PATH, json=body, headers=auth_headers("POST", PUBLISH_PATH)
        )
    assert resp.status_code == 200
    assert resp.json()["knowledge_id"] == "kn-1"


def test_publish_default_does_not_wait(client):
    """sync defaults to false: the call must not poll after the publish flip."""
    body = {"kb_id": "kb-1", "title": "t", "content": "c", "custom_metas": {"level": 3}}
    gets = {"count": 0}

    def _get(request):
        gets["count"] += 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "id": "kn-1",
                    "custom_metadata": {},
                    "parse_status": "pending",
                    "enable_status": "pending",
                },
            },
        )

    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        _mock_weknora(router)
        router.get(f"{UPSTREAM}/knowledge/kn-1").mock(side_effect=_get)
        resp = client.post(
            PUBLISH_PATH, json=body, headers=auth_headers("POST", PUBLISH_PATH)
        )

    assert resp.status_code == 200
    # exactly one GET: the merge-metas read; no post-publish polling
    assert gets["count"] == 1
    # statuses come straight from the publish response, not from a wait loop
    assert resp.json()["enable_status"] == "enabled"


def test_publish_sync_rejected_when_disabled(client):
    """sync is reserved: allow_sync=false (default) rejects before any upstream write."""
    body = {"kb_id": "kb-1", "title": "t", "content": "c", "sync": True}
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        _mock_weknora(router)
        resp = client.post(
            PUBLISH_PATH, json=body, headers=auth_headers("POST", PUBLISH_PATH)
        )

    assert resp.status_code == 400
    data = resp.json()
    assert data["error_id"] == "SYNC_DISABLED"
    assert isinstance(data["error_message"], str) and data["error_message"]
    # fail-fast: no draft was created upstream
    manual_posts = [
        c
        for c in router.calls
        if c.request.method == "POST" and "knowledge/manual" in str(c.request.url)
    ]
    assert manual_posts == []


def test_publish_sync_waits_for_processing(client):
    """sync=true (with allow_sync) blocks until processing finishes."""
    from tests.conftest import write_config

    write_config(
        {
            "publish": {
                "allow_sync": True,
                "wait_until": "enabled",
                "timeout_seconds": 2,
                "poll_interval_seconds": 0.01,
            }
        }
    )
    try:
        body = {"kb_id": "kb-1", "title": "t", "content": "c", "sync": True}
        polls = {"count": 0}

        def _poll(request):
            polls["count"] += 1
            enabled = polls["count"] > 1
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "id": "kn-1",
                        "enable_status": "enabled" if enabled else "pending",
                        "parse_status": "completed",
                    },
                },
            )

        with respx.mock(assert_all_called=False) as router:
            router.get(VALIDATE_URL).mock(
                return_value=httpx.Response(200, json={"success": True, "data": []})
            )
            _mock_weknora(router)
            router.get(f"{UPSTREAM}/knowledge/kn-1").mock(side_effect=_poll)
            resp = client.post(
                PUBLISH_PATH, json=body, headers=auth_headers("POST", PUBLISH_PATH)
            )

        assert resp.status_code == 200
        assert resp.json()["enable_status"] == "enabled"
        assert polls["count"] >= 2
    finally:
        write_config()  # restore BASE_CONFIG


def test_publish_reuses_existing_tag_when_create_conflicts(client):
    """Create-first: if WeKnora rejects the create (tag already exists), resolve by query."""
    body = {"kb_id": "kb-1", "title": "t", "content": "c", "tag_names": ["技术文档"]}
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        # create rejected because the tag already exists
        router.post(f"{UPSTREAM}/knowledge-bases/kb-1/tags").mock(
            return_value=httpx.Response(
                409, json={"error": {"message": "tag already exists"}}
            )
        )
        # fallback lookup finds the existing tag and returns its id
        router.get(f"{UPSTREAM}/knowledge-bases/kb-1/tags").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "data": [{"id": "tag-exists", "name": "技术文档"}],
                        "total": 1,
                    },
                },
            )
        )
        router.post(f"{UPSTREAM}/knowledge-bases/kb-1/knowledge/manual").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"id": "kn-1", "parse_status": "pending"},
                },
            )
        )
        router.get(f"{UPSTREAM}/knowledge/kn-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "id": "kn-1",
                        "custom_metadata": {},
                        "parse_status": "pending",
                    },
                },
            )
        )
        router.put(f"{UPSTREAM}/knowledge/kn-1").mock(
            return_value=httpx.Response(200, json={"success": True, "data": {}})
        )
        router.put(f"{UPSTREAM}/knowledge/manual/kn-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"id": "kn-1", "enable_status": "enabled"},
                },
            )
        )
        resp = client.post(
            PUBLISH_PATH, json=body, headers=auth_headers("POST", PUBLISH_PATH)
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["tag_ids"] == ["tag-exists"]
    assert data["tag_names"] == ["技术文档"]
