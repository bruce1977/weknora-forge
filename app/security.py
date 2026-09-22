"""Forge second-factor verification.

Native WeKnora has a single credential: ``X-API-Key``. Forge verifies that key against
WeKnora itself (layer 1) and adds a second factor on top (layer 2, every v2 endpoint).

HMAC signature scheme (simplified)
----------------------------------
There is no timestamp, no nonce and no body digest - the signed payload is exactly:

    payload = HTTP_METHOD + HTTP_FULL_PATH          (e.g. "POST/api/v2/publish?dry_run=1")

    X-Forge-Signature = hex(HMAC_SHA256(secret, payload))

``secret`` is the caller's own WeKnora API key, so no extra shared secret has to be
distributed: whoever owns the key can sign, and nobody else can.

Because the payload contains no freshness input, the same signature would be valid
forever. Freshness is therefore enforced by making every signature **single use**: once
seen it is rejected while it stays in the cache (``auth.signature_cache_ttl_seconds``).
That cache is the replacement for the old timestamp window. Only state-changing methods
(POST/PUT/PATCH/DELETE) are deduplicated; repeating a GET (refresh, pagination) stays
allowed. Transport MUST be HTTPS - see README for the full threat model.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from fastapi import Request

from .config import AuthConfig, UpstreamConfig
from .errors import invalid_signature, unauthorized
from .logging import get_logger

logger = get_logger(__name__)

HEADER_API_KEY = "x-api-key"


@dataclass
class Principal:
    """Resolved caller identity for one request."""

    client_id: str
    method: str  # hmac | anonymous
    api_key_present: bool = False
    api_key_ref: str = ""
    api_key_valid: Optional[bool] = None
    api_key_message: str = ""


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}***{value[-4:]}"


def full_path(request: Request) -> str:
    """Path + query string, byte-exact as they arrived.

    The ASGI ``raw_path`` is preferred over the decoded ``url.path`` so that a
    percent-encoded segment is signed exactly as the caller sent it. Forge sits behind
    an HTTPS terminating proxy: it must not normalise, re-encode or re-order anything
    or the signature computed at the edge would stop matching here.
    """
    raw_path = request.scope.get("raw_path")
    if raw_path:
        path = raw_path.decode("latin-1")
        root_path = request.scope.get("root_path") or ""
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :]
        if not path.startswith("/"):
            path = f"/{path}"
    else:
        path = request.url.path
    query = request.url.query
    return f"{path}?{query}" if query else path


def signature_payload(method: str, path_with_query: str) -> str:
    """METHOD + FULL_PATH, concatenated without any separator."""
    return f"{method.upper()}{path_with_query}"


def compute_signature(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


class SignatureCache:
    """Short-lived single-use cache: seen signatures are rejected within the TTL."""

    def __init__(self, ttl: int, max_entries: int) -> None:
        self.ttl = max(ttl, 0)
        self.max_entries = max(max_entries, 1)
        self._seen: Dict[str, float] = {}

    def _gc(self, now: float) -> None:
        expired = [k for k, ts in self._seen.items() if now - ts > self.ttl]
        for k in expired:
            self._data_pop(k)
        if len(self._seen) > self.max_entries:
            overflow = sorted(self._seen.items(), key=lambda kv: kv[1])[: len(self._seen) - self.max_entries]
            for k, _ts in overflow:
                self._data_pop(k)

    def _data_pop(self, key: str) -> None:
        self._seen.pop(key, None)

    def check_and_add(self, signature: str) -> bool:
        if self.ttl <= 0:
            return True
        now = time.time()
        self._gc(now)
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
        if digest in self._seen:
            return False
        self._seen[digest] = now
        return True

    def clear(self) -> None:
        self._seen.clear()


_signature_cache: Optional[SignatureCache] = None


def get_signature_cache(auth: AuthConfig) -> SignatureCache:
    global _signature_cache
    if _signature_cache is None:
        _signature_cache = SignatureCache(auth.signature_cache_ttl_seconds, auth.signature_cache_max_entries)
    else:
        _signature_cache.ttl = auth.signature_cache_ttl_seconds
        _signature_cache.max_entries = auth.signature_cache_max_entries
    return _signature_cache


def reset_caches() -> None:
    """Drop both in-process caches (tests / config reload)."""
    global _signature_cache
    if _signature_cache is not None:
        _signature_cache.clear()


def extract_upstream_api_key(request: Request, upstream: UpstreamConfig) -> str:
    """Read the caller's WeKnora credential.

    X-API-Key wins, Authorization: Bearer <token> is the fallback, and finally the
    server-held default key from the config ("server acts on behalf of the caller").
    """
    key = request.headers.get(HEADER_API_KEY, "").strip()
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    if not key and upstream.default_api_key:
        key = upstream.default_api_key.strip()
    return key


def _client_label(request: Request, auth: AuthConfig, api_key: str) -> str:
    """Identify the caller for logging. No X-Forge-Key header exists anymore, so the
    label is derived from the (masked) API key."""
    return f"apikey:{mask_secret(api_key)}"


def verify_hmac_signature(
    request: Request,
    auth: AuthConfig,
    api_key: str,
) -> str:
    """Verify layer 2. Returns the client label, raises otherwise."""
    signature = request.headers.get(auth.hmac_header_signature, "").strip().lower()
    if not signature:
        raise invalid_signature(f"Missing {auth.hmac_header_signature} header")
    # Accept uppercase or lowercase hex, ignore surrounding whitespace only
    if not api_key:
        raise unauthorized("Missing WeKnora API key: provide X-API-Key")

    expected = compute_signature(api_key, signature_payload(request.method, full_path(request)))
    if not hmac.compare_digest(expected, signature):
        raise invalid_signature("Signature does not match METHOD + FULL_PATH")

    if request.method.upper() in auth.signature_methods and not get_signature_cache(auth).check_and_add(signature):
        raise invalid_signature("Signature already used (replayed request)")

    return _client_label(request, auth, api_key)


async def authenticate(request: Request, auth: AuthConfig, upstream: UpstreamConfig) -> Tuple[Principal, str]:
    """Run second-factor verification only (layer 2). Returns (principal, api_key)."""
    api_key = extract_upstream_api_key(request, upstream)
    if auth.mode == "off":
        return Principal(client_id="anonymous", method="anonymous", api_key_present=bool(api_key), api_key_ref=mask_secret(api_key)), api_key
    label = verify_hmac_signature(request, auth, api_key)
    return Principal(client_id=label, method="hmac", api_key_present=True, api_key_ref=mask_secret(api_key)), api_key
