"""WeKnora Forge application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .config import get_config
from .deps import shutdown, startup
from .errors import install_exception_handlers
from .logging import get_logger, setup_logging
from .proxy import ProxyHeadersMiddleware
from .routers import maintenance as maintenance_router
from .routers import metas as metas_router
from .routers import publish as publish_router
from .routers import system as system_router
from .routers.proxy_v1 import build_proxy_router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_config()
    setup_logging(config.service.log_level)
    await startup(config)
    logger.info(
        "weknora-forge %s started | upstream=%s | auth=%s | db=%s | trusted_proxies=%s",
        __version__,
        config.upstream.base_url,
        config.auth.mode,
        "configured" if config.database.configured else "not configured",
        ",".join(config.proxy.trusted_proxies) if config.proxy.enabled else "disabled",
    )
    try:
        yield
    finally:
        await shutdown()


def create_app() -> FastAPI:
    config = get_config()
    setup_logging(config.service.log_level)

    app = FastAPI(
        title="WeKnora Forge",
        version=__version__,
        description=(
            "WeKnora API extension layer: v1 passthrough with an HMAC second factor; "
            "v2 adds publish orchestration, Custom Metas search (FMQ -> PostgreSQL) "
            "and physical purge of soft-deleted data."
        ),
        lifespan=lifespan,
    )
    install_exception_handlers(app)

    # HTTPS is terminated upstream (Cloudflare) while Forge speaks plain HTTP: rebuild
    # the caller's scheme / address from the forwarding headers of a trusted peer.
    app.add_middleware(ProxyHeadersMiddleware, config=config.proxy)

    # ---- v1 passthrough ----
    app.include_router(build_proxy_router("/api/v1", config))
    app.include_router(build_proxy_router("/v1", config))

    # ---- v2 extensions ----
    app.include_router(publish_router.router, prefix="/api/v2")
    app.include_router(metas_router.router, prefix="/api/v2")
    app.include_router(maintenance_router.router, prefix="/api/v2")
    app.include_router(system_router.router, prefix="/api/v2")

    # ---- unauthenticated ----
    app.include_router(system_router.health_router)

    return app


app = create_app()
