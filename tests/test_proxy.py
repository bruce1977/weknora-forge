"""Reverse-proxy behaviour: HTTPS terminated in front of an HTTP origin.

Deployment shape under test::

    public client --https--> Cloudflare --http--> forge --http--> WeKnora

Covered behaviours:
  * forwarding headers are believed ONLY when the TCP peer is a configured proxy
  * the scheme is rebuilt from X-Forwarded-Proto, so trailing-slash redirects do not
    downgrade an HTTPS visitor to http://
  * the real client address comes from CF-Connecting-IP (or the rightmost untrusted
    X-Forwarded-For entry), never from a value a public caller supplied
  * the v1 passthrough rebuilds X-Forwarded-* towards WeKnora and never relays CDN
    bookkeeping headers verbatim
  * the HMAC payload keeps the path byte-exact (percent-encoding included) across the
    proxy hop
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any, Dict

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.proxy import is_proxy_header
from tests.conftest import VALIDATE_URL, auth_headers, write_config

# A docker bridge address: inside the default trusted_proxies list
TRUSTED_PEER = ("172.17.0.1", 51234)
# A public address: nothing it claims about the caller may be trusted
UNTRUSTED_PEER = ("203.0.113.7", 51234)

TEST_ENDPOINT = "/api/v2/health"


@pytest.fixture()
def make_client():
    """Client factory that lets a test choose the TCP peer address (where the proxy sits)."""
    with ExitStack() as stack:

        def _make(peer=UNTRUSTED_PEER, follow_redirects: bool = False, **overrides: Any) -> TestClient:
            # Always rewrite the file so no test inherits the previous one's proxy setup
            write_config(overrides or None)
            return stack.enter_context(
                TestClient(create_app(), client=peer, follow_redirects=follow_redirects)
            )

        yield _make


def _health(client: TestClient, extra: Dict[str, str] | None = None) -> Dict[str, Any]:
    headers = auth_headers("GET", TEST_ENDPOINT)
    headers.update(extra or {})
    with respx.mock(assert_all_called=False) as router:
        router.get(VALIDATE_URL).mock(return_value=httpx.Response(200, json={"success": True, "data": []}))
        resp = client.get(TEST_ENDPOINT, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# --------------------------------------------------------------------------- #
# Header trust
# --------------------------------------------------------------------------- #
def test_direct_caller_cannot_spoof_proxy_headers(make_client):
    """A public caller talking to Forge directly is not allowed to describe itself."""
    data = _health(
        make_client(peer=UNTRUSTED_PEER),
        {
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "10.1.2.3",
            "CF-Connecting-IP": "10.1.2.3",
        },
    )
    assert data["trusted_proxy"] is False
    assert data["scheme"] == "http"
    assert data["client_ip"] == UNTRUSTED_PEER[0]
    assert data["peer_ip"] == UNTRUSTED_PEER[0]


def test_trusted_proxy_supplies_scheme_and_client_ip(make_client):
    data = _health(
        make_client(peer=TRUSTED_PEER),
        {"X-Forwarded-Proto": "https", "CF-Connecting-IP": "198.51.100.9"},
    )
    assert data["trusted_proxy"] is True
    assert data["scheme"] == "https"
    assert data["client_ip"] == "198.51.100.9"
    assert data["peer_ip"] == TRUSTED_PEER[0]


def test_cloudflare_header_wins_over_forwarded_for(make_client):
    """CF-Connecting-IP is overwritten by Cloudflare, X-Forwarded-For is not."""
    data = _health(
        make_client(peer=TRUSTED_PEER),
        {"CF-Connecting-IP": "198.51.100.9", "X-Forwarded-For": "10.0.0.66"},
    )
    assert data["client_ip"] == "198.51.100.9"


def test_forwarded_for_chain_skips_trusted_hops(make_client):
    """Walk the chain from the right and stop at the first address we do not trust."""
    data = _health(
        make_client(peer=TRUSTED_PEER),
        {"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.9, 172.17.0.1"},
    )
    assert data["client_ip"] == "198.51.100.9"


def test_proxy_handling_can_be_disabled(make_client):
    data = _health(
        make_client(peer=TRUSTED_PEER, proxy={"enabled": False}),
        {"X-Forwarded-Proto": "https", "CF-Connecting-IP": "198.51.100.9"},
    )
    assert data["trusted_proxy"] is False
    assert data["scheme"] == "http"
    assert data["client_ip"] == TRUSTED_PEER[0]


def test_trusted_proxies_list_is_configurable(make_client):
    """An address outside the configured list is a direct caller again."""
    data = _health(
        make_client(peer=TRUSTED_PEER, proxy={"trusted_proxies": ["10.9.9.9"]}),
        {"X-Forwarded-Proto": "https", "CF-Connecting-IP": "198.51.100.9"},
    )
    assert data["trusted_proxy"] is False
    assert data["scheme"] == "http"


# --------------------------------------------------------------------------- #
# Absolute URLs / redirects
# --------------------------------------------------------------------------- #
def test_trailing_slash_redirect_keeps_https(make_client):
    """Without the scheme fix an HTTPS caller would be bounced to http://..."""
    client = make_client(peer=TRUSTED_PEER)
    resp = client.get(f"{TEST_ENDPOINT}/", headers={"X-Forwarded-Proto": "https"})
    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith("https://")
    assert location.endswith("/api/v2/health")


def test_trailing_slash_redirect_stays_http_for_direct_callers(make_client):
    client = make_client(peer=UNTRUSTED_PEER)
    resp = client.get(f"{TEST_ENDPOINT}/", headers={"X-Forwarded-Proto": "https"})
    assert resp.status_code == 307
    assert resp.headers["location"].startswith("http://")


# --------------------------------------------------------------------------- #
# v1 passthrough
# --------------------------------------------------------------------------- #
def test_v1_passthrough_rebuilds_client_headers(make_client):
    client = make_client(peer=TRUSTED_PEER)
    seen: Dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(200, json={"success": True, "data": []})

    with respx.mock(assert_all_called=False) as router:
        router.route(url__regex=r"^http://upstream\.test/api/v1/knowledge-bases(\?.*)?$").mock(
            side_effect=capture
        )
        resp = client.get(
            "/api/v1/knowledge-bases?page=1&page_size=20",
            headers={
                **auth_headers("GET", "/api/v1/knowledge-bases?page=1&page_size=20"),
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": "10.0.0.66",
                "CF-Ray": "8f2c1a0b9e",
                "CF-Connecting-IP": "198.51.100.9",
            },
        )

    assert resp.status_code == 200
    headers = seen["headers"]
    assert headers["x-forwarded-proto"] == "https"
    # the spoofed value is dropped and rebuilt from the trusted-peer analysis
    assert headers["x-forwarded-for"] == "198.51.100.9"
    assert "cf-ray" not in headers
    assert "cf-connecting-ip" not in headers
    # Forge internals never leak to the origin
    assert "x-forge-signature" not in headers
    assert "x-forge-key" not in headers


def test_v1_passthrough_can_skip_client_headers(make_client):
    client = make_client(peer=TRUSTED_PEER, proxy={"forward_client_info": False})
    seen: Dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(200, json={"success": True, "data": []})

    with respx.mock(assert_all_called=False) as router:
        router.route(url__regex=r"^http://upstream\.test/api/v1/knowledge-bases(\?.*)?$").mock(
            side_effect=capture
        )
        resp = client.get(
            "/api/v1/knowledge-bases",
            headers={**auth_headers("GET", "/api/v1/knowledge-bases"), "X-Forwarded-For": "10.0.0.66"},
        )

    assert resp.status_code == 200
    assert "x-forwarded-for" not in seen["headers"]
    assert "x-forwarded-proto" not in seen["headers"]


# --------------------------------------------------------------------------- #
# Signature survives the proxy hop
# --------------------------------------------------------------------------- #
def test_encoded_path_is_signed_verbatim(make_client):
    """Forge must not normalise the path: the edge signs the bytes it forwarded."""
    client = make_client()
    with respx.mock(assert_all_called=False) as router:
        router.route(url__regex=r"^http://upstream\.test/api/v1/knowledge/.*$").mock(
            return_value=httpx.Response(200, json={"success": True, "data": {}})
        )
        ok = client.get(
            "/api/v1/knowledge/abc%20def",
            headers=auth_headers("GET", "/api/v1/knowledge/abc%20def"),
        )
        assert ok.status_code == 200

    with respx.mock(assert_all_called=False):
        # the decoded form is a different payload, so it must not verify
        bad = client.get(
            "/api/v1/knowledge/abc%20def",
            headers=auth_headers("GET", "/api/v1/knowledge/abc def"),
        )
        assert bad.status_code == 401


def test_same_signature_verifies_on_https_and_http(make_client):
    """The payload has no scheme/host: signed externally, verified internally."""
    client = make_client(peer=TRUSTED_PEER)
    same = auth_headers("GET", TEST_ENDPOINT)["X-Forge-Signature"]

    external = _health(client, {"X-Forwarded-Proto": "https", "X-Forge-Signature": same})
    assert external["scheme"] == "https"

    internal = _health(client, {"X-Forge-Signature": same})
    assert internal["scheme"] == "http"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_proxy_header_detection():
    assert is_proxy_header("CF-Connecting-IP")
    assert is_proxy_header("CF-Ray")
    assert is_proxy_header("cf-anything-future")
    assert is_proxy_header("X-Forwarded-For")
    assert not is_proxy_header("Content-Type")
    assert not is_proxy_header("X-Client-Version")
