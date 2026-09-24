"""Forge second-factor verification.

Native WeKnora has a single credential: ``X-API-Key``. Forge verifies that key against
WeKnora itself (layer 1) and adds a second factor on top (layer 2, every v2 endpoint).

HMAC signature scheme (simplified)
----------------------------------
There is no timestamp, no nonce and no body digest - the signed payload is exactly:

    payload = HTTP_METHOD + HTTP_FULL_PATH          (e.g. "POST/api/v2/publish?dry_run=1")

    X-Forge-Signature = hex(HMAC_SHA256(api_secret, payload))

``api_secret`` is looked up in the keystore (``app/keystore.py``) by the caller's
WeKnora API key: the secret comes from the ``WEKNORA_API_KEY`` / ``WEKNORA_API_SECRET``
environment pair (local testing) merged with ``keys.json`` (deployments; a file entry
overrides the environment secret for the same api_key). The API key itself is never
the signing key, so possessing it alone does not let anyone forge signatures. An
api_key without a configured api_secret is rejected.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional, Tuple

from fastapi import Request

from .config import AuthConfig, UpstreamConfig
from .errors import invalid_signature, unauthorized
from .keystore import keystore
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
    return hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def reset_caches() -> None:
    """Drop in-process caches (tests / config reload)."""
    keystore.reset()


def extract_upstream_api_key(request: Request) -> str:
    """Read the caller's WeKnora credential.

    X-API-Key wins, Authorization: Bearer <token> is the fallback.
    """
    key = request.headers.get(HEADER_API_KEY, "").strip()
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    return key


def _client_label(request: Request, auth: AuthConfig, api_key: str) -> str:
    """Identify the caller for logging. The label is derived from the (masked) API key."""
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
    if not api_key:
        raise unauthorized("Missing WeKnora API key: provide X-API-Key")

    secret = keystore.get_secret(api_key)
    if not secret:
        raise invalid_signature(
            "API key has no configured api_secret "
            "(see WEKNORA_API_KEY/WEKNORA_API_SECRET or keys.json)"
        )

    expected = compute_signature(
        secret, signature_payload(request.method, full_path(request))
    )
    if not hmac.compare_digest(expected, signature):
        raise invalid_signature("Signature does not match METHOD + FULL_PATH")

    return _client_label(request, auth, api_key)


async def authenticate(
    request: Request, auth: AuthConfig, upstream: UpstreamConfig
) -> Tuple[Principal, str]:
    """Run second-factor verification only (layer 2). Returns (principal, api_key)."""
    api_key = extract_upstream_api_key(request)
    if auth.mode == "off":
        return Principal(
            client_id="anonymous",
            method="anonymous",
            api_key_present=bool(api_key),
            api_key_ref=mask_secret(api_key),
        ), api_key
    label = verify_hmac_signature(request, auth, api_key)
    return Principal(
        client_id=label,
        method="hmac",
        api_key_present=True,
        api_key_ref=mask_secret(api_key),
    ), api_key
