"""Test scaffolding.

Forge is configured by a JSON file, so the fixtures write one into a temp directory and
point FORGE_CONFIG at it before any app module is imported.

The api_key/api_secret pair used by every signed request is read from the environment
(``WEKNORA_API_KEY`` / ``WEKNORA_API_SECRET``).  When not exported, the values are
filled from the repo ``.env`` (local testing) and finally from deterministic defaults
so a bare CI run still works.  The resolved pair is written back into the environment
so the keystore registers it exactly like a local runtime would.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

_TMP = tempfile.mkdtemp(prefix="forge-test-")

_DEFAULT_API_KEY = "sk-test-key"
_DEFAULT_API_SECRET = "test-secret-3f9c2b7a1d"


def _fill_from_env_file() -> None:
    """Fill WEKNORA_API_KEY / WEKNORA_API_SECRET from the repo .env when absent."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if (
            name in {"WEKNORA_API_KEY", "WEKNORA_API_SECRET"}
            and not os.environ.get(name, "").strip()
        ):
            os.environ[name] = value.strip().strip("'\"")


_fill_from_env_file()

# Base signing pair: real environment first, deterministic defaults as the last resort.
API_KEY = os.environ.get("WEKNORA_API_KEY", "").strip() or _DEFAULT_API_KEY
API_SECRET = os.environ.get("WEKNORA_API_SECRET", "").strip() or _DEFAULT_API_SECRET
# The keystore reads the environment: make sure the resolved pair is registered.
os.environ["WEKNORA_API_KEY"] = API_KEY
os.environ["WEKNORA_API_SECRET"] = API_SECRET

BASE_CONFIG: Dict[str, Any] = {
    "service": {"log_level": "WARNING"},
    "upstream": {
        "base_url": "http://upstream.test",
        "api_prefix": "/api/v1",
        "timeout_seconds": 10,
        "api_key_validate_path": "/knowledge-bases?page=1&page_size=1",
    },
    "auth": {
        "mode": "hmac",
        "require_on_v1": True,
        "require_on_v2": True,
    },
    "publish": {
        "wait_until": "enabled",
        "timeout_seconds": 5,
        "poll_interval_seconds": 0.01,
        "default_channel": "api",
        "merge_metas": True,
        "rollback_on_failure": True,
    },
    "database": {"dsn": "", "host": "", "name": "WeKnora"},
    "metas_search": {
        "table": "knowledges",
        "result_column": ["id", "title", "file_name", "kb_name", "tag_name"],
        "max_rows": 500,
        "chunk_table": "chunks",
    },
    "purge": {
        "dry_run": True,
        "default_retention_days": 30,
        "include_embed": False,
        "max_rows": 1000,
    },
}

_CONFIG_PATH = os.path.join(_TMP, "config.json")
with open(_CONFIG_PATH, "w", encoding="utf-8") as handle:
    json.dump(BASE_CONFIG, handle)

os.environ["FORGE_CONFIG"] = _CONFIG_PATH

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import reload_config  # noqa: E402
from app.main import create_app  # noqa: E402
from app.security import reset_caches  # noqa: E402

UPSTREAM = "http://upstream.test/api/v1"
VALIDATE_URL = f"{UPSTREAM}/knowledge-bases?page=1&page_size=1"


def write_config(overrides: Optional[Dict[str, Any]] = None) -> None:
    """Rewrite the config file (merge one level deep) and reload it."""
    data = json.loads(json.dumps(BASE_CONFIG))
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    with open(_CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    reload_config(_CONFIG_PATH)
    reset_caches()


def sign(method: str, path: str, api_key: str = API_KEY, api_secret: str = "") -> str:
    """HMAC-SHA256 over METHOD + FULL_PATH, keyed by the paired api_secret.

    Defaults to the registered test pair.  Unknown api_keys fall back to
    self-signing so a signature made for one credential never verifies for another.
    """
    secret = api_secret or (API_SECRET if api_key == API_KEY else api_key)
    payload = f"{method.upper()}{path}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def auth_headers(
    method: str, path: str, api_key: str = API_KEY, api_secret: str = ""
) -> Dict[str, str]:
    return {
        "X-API-Key": api_key,
        "X-Forge-Signature": sign(method, path, api_key, api_secret),
        "Content-Type": "application/json",
    }


@pytest.fixture(autouse=True)
def _reset_process_caches():
    """Signature / API-key caches live in module globals: keep tests independent."""
    yield
    reset_caches()
    try:  # the upstream client is created per app instance, ignore when absent
        from app.deps import get_client

        get_client().reset_cache()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture()
def client() -> Iterator[TestClient]:
    application = create_app()
    with TestClient(application) as c:
        yield c
