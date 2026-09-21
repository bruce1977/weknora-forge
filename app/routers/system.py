"""System and health endpoints."""

from __future__ import annotations

from typing import Tuple

from fastapi import APIRouter, Depends, Request

from .. import __version__
from ..config import Config
from ..deps import config_dep, get_db, verify_v2
from ..proxy import proxy_info
from ..services.db import Database
from ..security import Principal

router = APIRouter(tags=["v2-system"])
health_router = APIRouter(tags=["system"])


@health_router.get("/healthz", summary="Health check (no auth required)")
async def healthz() -> dict:
    return {"status": "ok", "service": "weknora-forge", "version": __version__}


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


@router.get("/health", summary="Dependency health (WeKnora + PostgreSQL)")
async def health(
    request: Request,
    auth: Tuple[Principal, str] = Depends(verify_v2),
    config: Config = Depends(config_dep),
    db: Database = Depends(get_db),
) -> dict:
    database_ok = False
    database_error: str | None = None
    try:
        database_ok = await db.ping()
    except Exception as exc:  # noqa: BLE001
        database_error = str(exc)

    info = proxy_info(request)
    principal, _api_key = auth
    return {
        "success": True,
        "data": {
            "upstream": config.upstream.base_url,
            "auth_mode": config.auth.mode,
            "database_configured": db.configured,
            "database_ok": database_ok,
            "database_error": database_error,
            "auth_method": principal.method,
            "api_key_valid": principal.api_key_valid,
            "scheme": info.scheme,
            "client_ip": info.client_ip,
            "peer_ip": info.peer_ip,
            "trusted_proxy": info.trusted_proxy,
        },
    }
