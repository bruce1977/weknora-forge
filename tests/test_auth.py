"""Authentication tests: HMAC second factor + WeKnora API key validation.

Covered behaviours:
  * signature = HMAC-SHA256(api_key, METHOD + FULL_PATH)
  * v1 passthrough verifies the second factor but never calls back into WeKnora
  * v2 verifies the second factor FIRST, then validates the key upstream (cached)
  * "key rejected" (401) stays distinguishable from "WeKnora unreachable" (502)
"""

from __future__ import annotations

import httpx
import respx

from tests.conftest import API_KEY, VALIDATE_URL, UPSTREAM, auth_headers, sign, write_config

TEST_ENDPOINT = "/api/v2/probe"
PROBE_URL = f"{UPSTREAM}/knowledge-bases"


def _error(resp) -> tuple:
    body = resp.json()
    return resp.status_code, body.get("error_id"), body.get("error_message")


def test_v2_requires_signature(client):
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={"success": True, "data": []}))
        resp = client.get(TEST_ENDPOINT, headers={"X-API-Key": API_KEY})
        assert _error(resp)[:2] == (401, "INVALID_SIGNATURE")


def test_v2_rejects_wrong_path_signature(client):
    """Signing a different request line must fail - there is no timestamp to hide behind."""
    headers = auth_headers("GET", TEST_ENDPOINT)
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={"success": True, "data": []}))
        resp = client.get(f"{TEST_ENDPOINT}?page=2", headers=headers)
        assert _error(resp)[:2] == (401, "INVALID_SIGNATURE")


def test_v2_rejects_signature_from_another_api_key(client):
    headers = auth_headers("GET", TEST_ENDPOINT, api_key="sk-other")
    headers["X-API-Key"] = API_KEY
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={"success": True, "data": []}))
        resp = client.get(TEST_ENDPOINT, headers=headers)
        assert _error(resp)[:2] == (401, "INVALID_SIGNATURE")


def test_v2_validates_api_key_against_upstream(client):
    with respx.mock(assert_all_called=False) as router:
        route = router.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={"success": True, "data": []}))
        router.get(PROBE_URL).mock(
            return_value=httpx.Response(200, json={"success": True, "data": {"ok": True, "message": "reachable", "upstream": "http://upstream.test", "kb_count": 0}})
        )
        resp = client.get(TEST_ENDPOINT, headers=auth_headers("GET", TEST_ENDPOINT))
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["auth_method"] == "hmac"
        assert route.calls.call_count == 1

        # within the cache TTL upstream must not be asked again
        client.get(TEST_ENDPOINT, headers=auth_headers("GET", TEST_ENDPOINT))
        assert route.calls.call_count == 1


def test_v2_reports_invalid_api_key(client):
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(return_value=httpx.Response(401, json={"error": {"message": "unauthorized"}}))
        resp = client.get(TEST_ENDPOINT, headers=auth_headers("GET", TEST_ENDPOINT))
        status, error_id, message = _error(resp)
        assert (status, error_id) == (401, "INVALID_API_KEY")
        assert "rejected" in message


def test_v2_distinguishes_upstream_outage(client):
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(return_value=httpx.Response(500, json={"error": {"message": "boom"}}))
        resp = client.get(TEST_ENDPOINT, headers=auth_headers("GET", TEST_ENDPOINT))
        assert _error(resp)[:2] == (502, "UPSTREAM_ERROR")


def test_v1_passthrough_does_not_validate_api_key(client):
    """v1 stays a transparent pipe: WeKnora itself decides whether the key is good."""
    with respx.mock(assert_all_called=False) as router:
        validate_route = router.get(VALIDATE_URL).mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        upstream_route = router.route(url__regex=r"^http://upstream\.test/api/v1/knowledge-bases/?(\?.*)?$")
        upstream_route.mock(return_value=httpx.Response(200, json={"success": True, "data": []}))
        resp = client.get("/api/v1/knowledge-bases", headers=auth_headers("GET", "/api/v1/knowledge-bases"))
        assert resp.status_code == 200
        assert validate_route.calls.call_count == 0
        assert upstream_route.calls.call_count >= 1


def test_v1_requires_second_factor(client):
    with respx.mock(assert_all_called=False) as router:
        router.route(url__regex=r"^http://upstream\.test/api/v1/knowledge-bases/?(\?.*)?$").mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        resp = client.get("/api/v1/knowledge-bases", headers={"X-API-Key": API_KEY})
        assert resp.status_code == 401


def test_auth_mode_off_allows_signature_free_calls(client):
    write_config({"auth": {"mode": "off"}})
    try:
        with respx.mock(assert_all_called=False) as router:
            router.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={"success": True, "data": []}))
            router.get(PROBE_URL).mock(
                return_value=httpx.Response(200, json={"success": True, "data": {"ok": True, "message": "reachable"}})
            )
            resp = client.get(TEST_ENDPOINT, headers={"X-API-Key": API_KEY})
            assert resp.status_code == 200
            assert resp.json()["data"]["auth_method"] == "anonymous"
    finally:
        write_config({"auth": {"mode": "hmac"}})


def test_missing_api_key_is_reported(client):
    with respx.mock(assert_all_called=False):
        resp = client.get(
            TEST_ENDPOINT,
            headers={"X-Forge-Signature": sign("GET", TEST_ENDPOINT, api_key="x")},
        )
        assert resp.status_code == 401
