"""Dependency wiring: config / upstream client / database / authentication layers."""

from __future__ import annotations

from typing import Optional, Tuple

from fastapi import Depends, Request

from .config import Config, get_config
from .errors import ForgeError, invalid_api_key, unauthorized, upstream_error
from .logging import get_logger
from .security import Principal, authenticate, mask_secret
from .services.db import Database
from .upstream import WeKnoraClient

logger = get_logger(__name__)

_client: Optional[WeKnoraClient] = None
_db: Optional[Database] = None


def config_dep() -> Config:
    return get_config()


def get_client() -> WeKnoraClient:
    if _client is None:  # pragma: no cover - the lifespan hook guarantees initialization
        raise RuntimeError("WeKnoraClient is not initialized")
    return _client


def get_db() -> Database:
    if _db is None:  # pragma: no cover
        raise RuntimeError("Database is not initialized")
    return _db


async def startup(config: Config) -> None:
    global _client, _db
    _client = WeKnoraClient(config)
    _db = Database(config)


async def shutdown() -> None:
    global _client, _db
    if _db is not None:
        await _db.close()
        _db = None
    if _client is not None:
        await _client.aclose()
        _client = None


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
async def verify_v1(
    request: Request, config: Config = Depends(config_dep)
) -> Tuple[Principal, str]:
    """v1 passthrough: second factor only.

    The upstream never validates the key here on purpose - the passthrough must stay a
    transparent pipe and WeKnora rejects bad keys by itself with its own status code.
    """
    if config.auth.require_on_v1:
        principal, api_key = await authenticate(request, config.auth, config.upstream)
    else:
        from .security import extract_upstream_api_key

        api_key = extract_upstream_api_key(request, config.upstream)
        principal = Principal(
            client_id="anonymous", method="anonymous", api_key_present=bool(api_key), api_key_ref=mask_secret(api_key)
        )
    if not api_key:
        raise unauthorized("Missing WeKnora API key: provide X-API-Key (or Authorization: Bearer)")
    principal.api_key_present = True
    principal.api_key_ref = mask_secret(api_key)
    return principal, api_key


async def verify_v2(
    request: Request, config: Config = Depends(config_dep)
) -> Tuple[Principal, str]:
    """v2 extensions: HMAC first, then validate the key upstream."""
    if config.auth.require_on_v2:
        principal, api_key = await authenticate(request, config.auth, config.upstream)
    else:
        from .security import extract_upstream_api_key

        api_key = extract_upstream_api_key(request, config.upstream)
        principal = Principal(
            client_id="anonymous", method="anonymous", api_key_present=bool(api_key), api_key_ref=mask_secret(api_key)
        )
    if not api_key:
        raise unauthorized("Missing WeKnora API key: provide X-API-Key (or Authorization: Bearer)")

    client = get_client()
    valid, message, status = await client.validate_api_key(api_key)
    principal.api_key_valid = valid
    principal.api_key_message = message
    if not valid:
        raise _api_key_failure(message, status)
    return principal, api_key


def _api_key_failure(message: str, status: int) -> ForgeError:
    """Keep "key rejected" (401) apart from "WeKnora unreachable" (502)."""
    if status in (401, 403, 0):
        return invalid_api_key(f"WeKnora rejected the API key: {message}")
    return upstream_error(f"Unable to validate the WeKnora API key (upstream status {status}): {message}")
