"""API key / api_secret keystore backing the X-Forge-Signature verification.

The signature secret never travels with the request: the caller sends its WeKnora
API key in ``X-API-Key`` and proves possession of the paired ``api_secret`` via
``X-Forge-Signature = hex(HMAC_SHA256(api_secret, payload))``.

Two sources are merged into one in-process dictionary cache:

1. Environment (local testing)::

       WEKNORA_API_KEY=sk-xxxxx
       WEKNORA_API_SECRET=<secret>

2. ``keys.json`` sitting next to the effective ``config.json`` (deployments, one
   or more pairs) - i.e. ``data/keys.json`` locally, ``/data/keys.json`` in the
   container::

       [
         {"api_key": "sk-aaaa", "api_secret": "..."},
         {"api_key": "sk-bbbb", "api_secret": "..."}
       ]

A ``keys.json`` entry whose ``api_key`` equals ``WEKNORA_API_KEY`` overrides the
environment secret. The file is re-read automatically whenever it changes
(mtime + size fingerprint), so secrets can be rotated without a restart.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

from .config import default_config_path
from .logging import get_logger

logger = get_logger(__name__)

API_KEY_ENV = "WEKNORA_API_KEY"
API_SECRET_ENV = "WEKNORA_API_SECRET"
KEYS_FILENAME = "keys.json"


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}***{value[-4:]}"


def keys_file_path() -> Path:
    """``keys.json`` lives next to the effective ``config.json``."""
    return default_config_path().parent / KEYS_FILENAME


class KeyStore:
    """Dictionary cache of ``api_key -> api_secret`` with automatic refresh."""

    def __init__(self) -> None:
        self._pairs: Dict[str, str] = {}
        # (env key, env secret, keys path, (mtime_ns, size) | None)
        self._fingerprint: Optional[Tuple] = None
        self._lock = threading.Lock()

    # -- internals ---------------------------------------------------------- #
    @staticmethod
    def _current_fingerprint() -> Tuple:
        env_key = os.environ.get(API_KEY_ENV, "").strip()
        env_secret = os.environ.get(API_SECRET_ENV, "").strip()
        path = keys_file_path()
        try:
            stat = path.stat()
            file_fp: Optional[Tuple[int, int]] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            file_fp = None
        return (env_key, env_secret, str(path), file_fp)

    @staticmethod
    def _read_file(path: Path) -> Dict[str, str]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "keystore: cannot read %s (%s); file entries ignored", path, exc
            )
            return {}
        if not isinstance(data, list):
            logger.warning(
                "keystore: %s must be a JSON array; file entries ignored", path
            )
            return {}
        pairs: Dict[str, str] = {}
        for index, entry in enumerate(data):
            if not isinstance(entry, dict):
                logger.warning(
                    "keystore: %s[%d] is not an object; skipped", path, index
                )
                continue
            api_key = str(entry.get("api_key") or "").strip()
            api_secret = str(entry.get("api_secret") or "").strip()
            if not api_key or not api_secret:
                logger.warning(
                    "keystore: %s[%d] missing api_key/api_secret; skipped", path, index
                )
                continue
            pairs[api_key] = api_secret
        return pairs

    def _reload(self) -> None:
        pairs: Dict[str, str] = {}
        env_key = os.environ.get(API_KEY_ENV, "").strip()
        env_secret = os.environ.get(API_SECRET_ENV, "").strip()
        if env_key and env_secret:
            pairs[env_key] = env_secret
        path = keys_file_path()
        if path.is_file():
            # keys.json wins over the environment for the same api_key.
            pairs.update(self._read_file(path))
        self._pairs = pairs
        self._fingerprint = self._current_fingerprint()
        logger.info(
            "keystore refreshed: %d key pair(s) [%s]",
            len(pairs),
            ", ".join(mask_secret(key) for key in pairs) or "-",
        )

    def _ensure_fresh(self) -> None:
        if self._current_fingerprint() == self._fingerprint:
            return
        with self._lock:
            if self._current_fingerprint() == self._fingerprint:
                return
            self._reload()

    # -- public ------------------------------------------------------------- #
    def get_secret(self, api_key: str) -> Optional[str]:
        """Return the api_secret paired with ``api_key``, or None when unregistered."""
        if not api_key:
            return None
        self._ensure_fresh()
        return self._pairs.get(api_key)

    def reset(self) -> None:
        """Drop the cache; the next lookup reloads from env + keys.json."""
        with self._lock:
            self._pairs = {}
            self._fingerprint = None
        logger.debug("keystore reset")


keystore = KeyStore()
