"""v2 Custom Metas search (FMQ) - executed as SQL against PostgreSQL."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from fastapi import APIRouter, Depends, Request

from ..config import Config
from ..deps import config_dep, get_client, get_db, verify_v2
from ..errors import forbidden
from ..schemas import MetaSearchRequest
from ..security import Principal
from ..services.db import Database
from ..services.metas_search import MetasSearchService, SearchRequest

router = APIRouter(tags=["v2-metadata-search"])


def _resolve(payload: MetaSearchRequest, config: Config) -> SearchRequest:
    cfg = config.metas_search
    return SearchRequest(
        query=payload.metas_query,
        kb_ids=payload.kb_ids,
        page=payload.page,
        page_size=payload.page_size or cfg.default_page_size,
        case_insensitive=payload.case_insensitive,
        title=payload.title,
        tags=payload.tags,
        return_content=payload.return_content,
    )


async def _assert_kb_access(api_key: str, kb_ids: list[str]) -> None:
    """Verify the API key can access every requested knowledge base via WeKnora."""
    if not kb_ids:
        return
    client = get_client()
    payload = await client.list_knowledge_bases(api_key, page=1, page_size=500)
    # payload: {"data": [...], "total": N} or [...]
    if isinstance(payload, dict):
        items = payload.get("data") or []
    else:
        items = payload or []
    allowed = {
        str(item.get("id"))
        for item in items
        if isinstance(item, dict) and item.get("id")
    }
    denied = [kid for kid in kb_ids if kid not in allowed]
    if denied:
        raise forbidden(
            f"API key has no access to knowledge base(s): {', '.join(denied)}",
            error_id="KB_ACCESS_DENIED",
        )


@router.post(
    "/knowledge/search", summary="Search knowledge by metadata, title, and tags"
)
async def search_by_metas(
    payload: MetaSearchRequest,
    request: Request,
    auth: Tuple[Principal, str] = Depends(verify_v2),
    config: Config = Depends(config_dep),
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    _principal, api_key = auth
    await _assert_kb_access(api_key, payload.kb_ids)
    return await _run_search(_resolve(payload, config), config, db)


async def _run_search(
    req: SearchRequest, config: Config, db: Database
) -> Dict[str, Any]:
    async with db.transaction() as executor:
        service = MetasSearchService(executor, config)
        result = await service.search(req)
    # Response envelope: id / title / kb_name / metas / tag_names per item.
    # `metas` is the full custom_metadata blob for each hit.
    # `content` is present only when the request set return_content=true.
    items = []
    for row in result.rows:
        item: Dict[str, Any] = {
            "id": row.get("id"),
            "title": row.get("title"),
            "kb_name": row.get("kb_name"),
            "metas": row.get("metas") or {},
            "tag_names": row.get("tag_names") or [],
        }
        if req.return_content:
            item["content"] = row.get("content") or ""
        items.append(item)
    return {
        "success": True,
        "data": {
            "items": items,
            "total": result.total,
            "page": result.page,
            "page_size": result.page_size,
            "has_more": result.has_more,
        },
    }
