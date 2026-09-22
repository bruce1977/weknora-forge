"""System and health endpoints."""

from __future__ import annotations

from typing import Tuple

from fastapi import APIRouter, Depends, Request

from .. import __version__
from ..config import Config
from ..deps import config_dep, get_client, get_db, verify_v2
from ..logging import get_logger
from ..proxy import proxy_info
from ..security import Principal

logger = get_logger(__name__)

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


@router.get("/probe", summary="Probe WeKnora API and PostgreSQL database connectivity")
async def probe(
    request: Request,
    auth: Tuple[Principal, str] = Depends(verify_v2),
    config: Config = Depends(config_dep),
) -> dict:
    """Live connectivity check: test the WeKnora backend AND PostgreSQL database.

    Returns a combined result so operators can see at a glance whether the full
    stack is operational.
    """
    import time

    _principal, api_key = auth
    client = get_client()

    # --- WeKnora API ---
    weknora_result = await client.probe(api_key)
    weknora_ok = weknora_result.pop("ok", False)

    # --- PostgreSQL database ---
    db = get_db()
    db_ok = False
    db_message = "Database not configured"
    db_latency_ms = 0.0
    if db.configured:
        db_start = time.time()
        try:
            db_ok = await db.ping()
            db_latency_ms = round((time.time() - db_start) * 1000, 1)
            db_message = "PostgreSQL is reachable" if db_ok else "PostgreSQL ping failed"
        except Exception as exc:  # noqa: BLE001
            db_latency_ms = round((time.time() - db_start) * 1000, 1)
            db_message = f"PostgreSQL error: {exc}"
            logger.warning("database probe failed: %s", exc)

    overall_ok = weknora_ok and db_ok

    return {
        "success": overall_ok,
        "data": {
            "weknora": weknora_result,
            "database": {
                "ok": db_ok,
                "message": db_message,
                "latency_ms": db_latency_ms,
            },
            "auth_method": _principal.method,
        },
    }
