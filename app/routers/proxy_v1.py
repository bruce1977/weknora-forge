"""v1 passthrough: forward the native WeKnora API verbatim and add Forge second-factor verification."""

from __future__ import annotations

from typing import Dict, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..deps import get_client, verify_v1
from ..logging import get_logger
from ..proxy import forwarded_headers, is_proxy_header
from ..security import Principal
from ..upstream import HOP_BY_HOP

logger = get_logger(__name__)

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

_DROP_REQUEST_HEADERS = HOP_BY_HOP | {"host", "content-length", "x-api-key"}


def _is_stripped(name: str) -> bool:
    """Headers Forge never relays to WeKnora.

    Hop-by-hop headers plus everything the transport already owns (host, length, the
    credential), the Forge second-factor headers (upstream has no business seeing
    them) and any proxy / CDN bookkeeping header - X-Forwarded-* is rebuilt from the
    trusted-peer analysis instead of being passed through, so a public caller cannot
    forge what the origin records.
    """
    lowered = name.lower()
    return (
        lowered in _DROP_REQUEST_HEADERS
        or lowered.startswith("x-forge-")
        or is_proxy_header(lowered)
    )


def forward_headers(request: Request, api_key: str) -> Dict[str, str]:
    """Caller headers minus the stripped set, plus the credential and the client chain."""
    headers = {k: v for k, v in request.headers.items() if not _is_stripped(k)}
    headers["X-API-Key"] = api_key
    headers.update(forwarded_headers(request))
    return headers


def build_proxy_router(prefix: str) -> APIRouter:
    router = APIRouter(prefix=prefix or "", tags=["v1-passthrough"])

    @router.api_route("/{path:path}", methods=METHODS, include_in_schema=False)
    async def proxy(  # noqa: ANN001
        path: str,
        request: Request,
        auth: Tuple[Principal, str] = Depends(verify_v1),
    ):
        _principal, api_key = auth
        client = get_client()

        body = await request.body()
        headers = forward_headers(request, api_key)

        upstream = await client.raw(
            request.method,
            path,
            query=request.url.query,
            headers=headers,
            body=body,
        )

        media_type = upstream.headers.get("content-type")
        resp_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in HOP_BY_HOP
            and k.lower() not in {"content-length", "content-encoding"}
        }

        async def stream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            stream(),
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=media_type,
        )

    return router
