"""WeKnora Forge application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

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
        "weknora-forge %s started | upstream=%s | auth=%s | db=%s",
        __version__,
        config.upstream.base_url,
        config.auth.mode,
        "configured" if config.database.configured else "not configured",
    )
    try:
        yield
    finally:
        await shutdown()


def create_app() -> FastAPI:
    config = get_config()
    setup_logging(config.service.log_level)
    _swagger_enabled = config.swagger.enabled

    app = FastAPI(
        title="WeKnora Forge",
        version=__version__,
        description=(
            "WeKnora API extension layer: v1 passthrough with an HMAC second factor; "
            "v2 adds publish orchestration, Custom Metas search (FMQ -> PostgreSQL) "
            "and physical purge of soft-deleted data."
        ),
        lifespan=lifespan,
        docs_url=None,  # we serve our own offline /docs below (FastAPI's built-in route ignores custom swagger_*_url)
        openapi_url="/openapi.json" if _swagger_enabled else None,
        redoc_url=None,
    )
    install_exception_handlers(app)

    # ---- offline Swagger UI (vendored assets, no external CDN dependency) ----
    # Gated by config.swagger.enabled (env SWAGGER_ENABLED); when disabled, /docs and
    # /openapi.json are not exposed at all (openapi_url is None above).
    if _swagger_enabled:
        _swagger_dir = Path(__file__).resolve().parent / "static" / "swagger"
        if _swagger_dir.is_dir():
            app.mount(
                "/static/swagger",
                StaticFiles(directory=str(_swagger_dir)),
                name="swagger-ui-static",
            )

            @app.get("/docs", include_in_schema=False)
            async def custom_swagger_ui_html(request: Request) -> HTMLResponse:
                # FastAPI's built-in /docs route does not forward swagger_js_url/css_url/favicon_url,
                # so we render the UI ourselves against the locally vendored assets.
                root_path = request.scope.get("root_path", "").rstrip("/")
                return get_swagger_ui_html(
                    openapi_url=root_path + "/openapi.json",
                    title="WeKnora Forge - Swagger UI",
                    swagger_js_url="/static/swagger/swagger-ui-bundle.js",
                    swagger_css_url="/static/swagger/swagger-ui.css",
                    swagger_favicon_url="/static/swagger/favicon-32x32.png",
                )

    # HTTPS is terminated upstream (Cloudflare) while Forge speaks plain HTTP: rebuild
    # the caller's scheme / address from the forwarding headers of a trusted peer.
    app.add_middleware(ProxyHeadersMiddleware)

    # ---- v1 passthrough ----
    app.include_router(build_proxy_router("/api/v1"))
    app.include_router(build_proxy_router("/v1"))

    # ---- v2 extensions ----
    app.include_router(publish_router.router, prefix="/api/v2")
    app.include_router(metas_router.router, prefix="/api/v2")
    app.include_router(maintenance_router.router, prefix="/api/v2")
    app.include_router(system_router.router, prefix="/api/v2")

    # ---- unauthenticated ----
    app.include_router(system_router.health_router)

    # ---- OpenAPI: expose the custom HMAC scheme so Swagger's "Authorize" can supply it ----
    # FastAPI's built-in security wiring only covers standard schemes; our second factor is
    # two raw headers (X-API-Key + X-Forge-Signature), so we inject them into the schema and
    # require them on every route except the explicitly open liveness probes below.
    _OPEN_PATHS = {"/", "/api/v2/health"}

    # Expose the two HMAC headers as *editable request parameters* on every secured
    # operation. This makes them directly visible/typeable in Swagger's "Try it out"
    # form (rather than only behind the global Authorize popup) so the API can be
    # debugged from the UI. The open liveness probes are left untouched.
    _HEADER_PARAMS = [
        {
            "name": "X-API-Key",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "example": "YOUR_WEKNORA_API_KEY"},
            "description": "Your WeKnora API key - it is also the HMAC signing key.",
        },
        {
            "name": "X-Forge-Signature",
            "in": "header",
            "required": True,
            "schema": {
                "type": "string",
                "example": "hex(HMAC_SHA256(api_key, METHOD + FULL_PATH))",
            },
            "description": (
                "hex(HMAC_SHA256(api_key, METHOD + FULL_PATH)). Generate it with "
                "scripts/gen_forge_signature.py or scripts/hmac_request.py. "
                "NOTE: POST/PUT/PATCH/DELETE signatures are single-use."
            ),
        },
    ]

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        components["securitySchemes"] = {
            "X-API-Key": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": "Your WeKnora API key - it is also the HMAC signing key.",
            },
            "X-Forge-Signature": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Forge-Signature",
                "description": (
                    "hex(HMAC_SHA256(api_key, METHOD + FULL_PATH)). Generate it with "
                    "scripts/gen_forge_signature.py or scripts/hmac_request.py."
                ),
            },
        }
        schema["security"] = [{"X-API-Key": [], "X-Forge-Signature": []}]
        for path, methods in schema.get("paths", {}).items():
            for op in methods.values():
                if not isinstance(op, dict):
                    continue
                if path in _OPEN_PATHS:
                    # explicitly open: no auth, no header params
                    op["security"] = []
                    continue
                existing = op.get("parameters") or []
                names = {p.get("name") for p in existing if isinstance(p, dict)}
                for hp in _HEADER_PARAMS:
                    if hp["name"] not in names:
                        existing.append(hp)
                op["parameters"] = existing
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi

    return app


app = create_app()
