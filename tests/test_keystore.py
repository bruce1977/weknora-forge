"""Keystore tests: env / keys.json pair loading, override, auto-refresh.

Covered behaviours:
  * the test credentials come from WEKNORA_API_KEY / WEKNORA_API_SECRET (env)
  * keys.json (next to config.json) supplies deployment pairs
  * a keys.json entry overrides the environment secret for the same api_key
  * editing keys.json refreshes the cache automatically (rotation without restart)
  * an unregistered api_key is rejected with 401 INVALID_SIGNATURE
  * a malformed keys.json falls back to the environment pair
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.keystore import (
    API_KEY_ENV,
    API_SECRET_ENV,
    keys_file_path,
    keystore,
)
from tests.conftest import API_KEY, API_SECRET, auth_headers, sign

V1_PATH = "/api/v1/knowledge-bases"
V1_URL_RE = r"^http://upstream\.test/api/v1/knowledge-bases/?(\?.*)?$"


def _error(resp) -> tuple:
    body = resp.json()
    return resp.status_code, body.get("error_id")


@pytest.fixture()
def keys_path():
    """Provide the temp keys.json, restoring the base pair afterwards."""
    path = keys_file_path()
    original = path.read_text(encoding="utf-8") if path.exists() else None
    yield path
    if original is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(original, encoding="utf-8")
    keystore.reset()


@pytest.fixture()
def upstream_ok():
    with respx.mock(assert_all_called=False) as router:
        router.route(url__regex=V1_URL_RE).mock(
            return_value=httpx.Response(200, json={"success": True, "data": []})
        )
        yield router


# --------------------------------------------------------------------------- #
# Unit: dictionary contents
# --------------------------------------------------------------------------- #
def test_base_pair_is_loaded_from_env(keys_path):
    """The suite's credentials come from WEKNORA_API_KEY / WEKNORA_API_SECRET."""
    keystore.reset()
    assert keystore.get_secret(API_KEY) == API_SECRET
    assert keystore.get_secret("sk-unregistered") is None
    assert keystore.get_secret("") is None


def test_env_pair_is_loaded(monkeypatch, keys_path):
    monkeypatch.setenv(API_KEY_ENV, "sk-env-key")
    monkeypatch.setenv(API_SECRET_ENV, "env-secret-value")
    keystore.reset()
    assert keystore.get_secret("sk-env-key") == "env-secret-value"
    assert keystore.get_secret("sk-env-key-missing") is None


def test_keys_json_entry_merges_with_env(keys_path):
    keys_path.write_text(
        json.dumps([{"api_key": "sk-file-key", "api_secret": "file-secret"}]),
        encoding="utf-8",
    )
    keystore.reset()
    assert keystore.get_secret("sk-file-key") == "file-secret"
    # the environment pair keeps working alongside the file pair
    assert keystore.get_secret(API_KEY) == API_SECRET


def test_keys_json_overrides_env_secret(monkeypatch, keys_path):
    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    monkeypatch.setenv(API_SECRET_ENV, "env-secret-old")
    keys_path.write_text("[]", encoding="utf-8")  # file has no entry yet
    keystore.reset()
    assert keystore.get_secret(API_KEY) == "env-secret-old"

    keys_path.write_text(
        json.dumps([{"api_key": API_KEY, "api_secret": "file-secret-new"}]),
        encoding="utf-8",
    )
    # no explicit reset: the file fingerprint change must trigger a reload
    assert keystore.get_secret(API_KEY) == "file-secret-new"


def test_env_change_refreshes_cache(monkeypatch, keys_path):
    keystore.reset()
    assert keystore.get_secret("sk-rotating") is None
    monkeypatch.setenv(API_KEY_ENV, "sk-rotating")
    monkeypatch.setenv(API_SECRET_ENV, "rotated-secret")
    assert keystore.get_secret("sk-rotating") == "rotated-secret"


def test_malformed_keys_json_falls_back_to_env(monkeypatch, keys_path):
    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    monkeypatch.setenv(API_SECRET_ENV, API_SECRET)
    keys_path.write_text("{not valid json", encoding="utf-8")
    keystore.reset()
    assert keystore.get_secret(API_KEY) == API_SECRET


def test_keys_json_entries_must_be_objects(keys_path):
    keys_path.write_text(
        json.dumps(
            [
                "not-an-object",
                {"api_key": "", "api_secret": "x"},
                {"api_key": "sk-ok", "api_secret": "s"},
            ]
        ),
        encoding="utf-8",
    )
    keystore.reset()
    assert keystore.get_secret("sk-ok") == "s"
    assert keystore.get_secret("not-an-object") is None


# --------------------------------------------------------------------------- #
# End-to-end: signatures accepted / rejected over HTTP
# --------------------------------------------------------------------------- #
def test_env_pair_verifies_end_to_end(client, monkeypatch, upstream_ok):
    monkeypatch.setenv(API_KEY_ENV, "sk-env-e2e")
    monkeypatch.setenv(API_SECRET_ENV, "env-e2e-secret")
    keystore.reset()
    resp = client.get(
        V1_PATH,
        headers=auth_headers(
            "GET", V1_PATH, api_key="sk-env-e2e", api_secret="env-e2e-secret"
        ),
    )
    assert resp.status_code == 200


def test_keys_json_pair_verifies_end_to_end(client, keys_path, upstream_ok):
    keys_path.write_text(
        json.dumps([{"api_key": "sk-file-e2e", "api_secret": "file-e2e-secret"}]),
        encoding="utf-8",
    )
    keystore.reset()
    resp = client.get(
        V1_PATH,
        headers=auth_headers(
            "GET", V1_PATH, api_key="sk-file-e2e", api_secret="file-e2e-secret"
        ),
    )
    assert resp.status_code == 200

    wrong = client.get(
        V1_PATH,
        headers=auth_headers(
            "GET", V1_PATH, api_key="sk-file-e2e", api_secret="file-e2e-secret-old"
        ),
    )
    assert _error(wrong) == (401, "INVALID_SIGNATURE")


def test_secret_rotation_applies_without_restart(client, keys_path, upstream_ok):
    keys_path.write_text(
        json.dumps([{"api_key": "sk-rotate", "api_secret": "secret-v1"}]),
        encoding="utf-8",
    )
    keystore.reset()
    old_headers = auth_headers(
        "GET", V1_PATH, api_key="sk-rotate", api_secret="secret-v1"
    )
    assert client.get(V1_PATH, headers=old_headers).status_code == 200

    # rotate: same api_key, new (longer, so size changes too) api_secret
    keys_path.write_text(
        json.dumps([{"api_key": "sk-rotate", "api_secret": "secret-v2-rotated"}]),
        encoding="utf-8",
    )
    stale = client.get(V1_PATH, headers=old_headers)
    assert _error(stale) == (401, "INVALID_SIGNATURE")

    fresh_headers = auth_headers(
        "GET", V1_PATH, api_key="sk-rotate", api_secret="secret-v2-rotated"
    )
    assert client.get(V1_PATH, headers=fresh_headers).status_code == 200


def test_unregistered_api_key_is_rejected(client, upstream_ok):
    resp = client.get(
        V1_PATH,
        headers=auth_headers(
            "GET", V1_PATH, api_key="sk-unregistered", api_secret="whatever"
        ),
    )
    assert _error(resp) == (401, "INVALID_SIGNATURE")


def test_signature_keyed_by_api_key_is_rejected(client, upstream_ok):
    """Even the registered key must sign with its api_secret, not with itself."""
    resp = client.get(V1_PATH, headers=auth_headers("GET", V1_PATH, api_secret=API_KEY))
    assert _error(resp) == (401, "INVALID_SIGNATURE")


def test_correct_api_secret_is_accepted(client, upstream_ok):
    resp = client.get(V1_PATH, headers=auth_headers("GET", V1_PATH))
    assert resp.status_code == 200
    # sanity: the helper really signs with API_SECRET
    assert sign("GET", V1_PATH) == sign("GET", V1_PATH, API_KEY, API_SECRET)
