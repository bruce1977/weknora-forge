"""System and health endpoints."""

from __future__ import annotations

from typing import Tuple

from fastapi import APIRouter, Depends, Request

from .. import __version__
from ..config import Config
from ..deps import config_dep, get_client, verify_v2
from ..security import Principal

router = APIRouter(tags=["v2-system"])
health_router = APIRouter(tags=["system"])


@health_router.get("/", summary="Service information")
async def root(config: Config = Depends(config_dep)) -> dict:
    return {
        "service": "weknora-forge",
        "version": __version__,
        "upstream": config.upstream.base_url,
        "v1_prefix": "/api/v1",
        "v2_prefix": "/api/v2",
        "auth_mode": config.auth.mode,
    }


@router.get("/health", summary="Lightweight liveness check (no auth, no resource access)")
async def health() -> dict:
    # Intentionally open and dependency-free: a cold liveness probe that must not touch
    # WeKnora, PostgreSQL or any other resource so it stays fast and always answers.
    return {}


@router.get("/probe", summary="Probe the configured WeKnora backend for connectivity")
async def probe(
    request: Request,
    auth: Tuple[Principal, str] = Depends(verify_v2),
    config: Config = Depends(config_dep),
) -> dict:
    """Perform a live test request against the configured WeKnora (list its knowledge
    bases). If the backend answers, the probe succeeds; any connection failure or
    non-2xx response is reported as a failure rather than turning into a 5xx.
    """
    _principal, api_key = auth
    client = get_client()
    result = await client.probe(api_key)
    ok = result.pop("ok")
    result["auth_method"] = _principal.method
    return {"success": ok, "data": result}
