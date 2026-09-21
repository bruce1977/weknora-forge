"""v2 Custom Metas search (FMQ) - executed as SQL against PostgreSQL."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Query, Request

from ..config import Config
from ..deps import config_dep, get_db, verify_v2
from ..services.db import Database
from ..schemas import MetaParseRequest, MetaSearchRequest
from ..security import Principal
from ..services.meta_dsl import BUILTIN_FIELDS
from ..services.meta_dsl import __doc__ as FMQ_DOC
from ..services.meta_dsl import describe, parse_query, used_fields
from ..services.metas_search import MetasSearchService, SearchRequest

router = APIRouter(tags=["v2-metadata-search"])


def _resolve(payload: MetaSearchRequest, config: Config) -> SearchRequest:
    cfg = config.metas_search
    return SearchRequest(
        query=payload.metas_query,
        kb_id=payload.kb_id,
        page=payload.page,
        page_size=payload.page_size or cfg.default_page_size,
        case_insensitive=payload.case_insensitive,
        vector=payload.vector,
        include_deleted=payload.include_deleted,
        title=payload.title,
        tags=payload.tags,
    )


@router.post("/knowledge/search", summary="Search knowledge by metadata, title, and tags")
async def search_by_metas(
    payload: MetaSearchRequest,
    request: Request,
    auth: Tuple[Principal, str] = Depends(verify_v2),
    config: Config = Depends(config_dep),
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    return await _run_search(_resolve(payload, config), config, db)


@router.post("/metas/parse", summary="Parse an FMQ expression (debugging aid)")
async def parse_expression(
    payload: MetaParseRequest,
    request: Request,
    auth: Tuple[Principal, str] = Depends(verify_v2),
) -> Dict[str, Any]:
    node = parse_query(payload.query)
    return {
        "success": True,
        "data": {
            "query": payload.query,
            "ast": describe(node),
            "fields": used_fields(node),
        },
    }


@router.get("/metas/grammar", summary="FMQ syntax reference")
async def grammar(
    request: Request,
    auth: Tuple[Principal, str] = Depends(verify_v2),
) -> Dict[str, Any]:
    return {
        "success": True,
        "data": {
            "doc": FMQ_DOC,
            "builtin_fields": sorted(f"${f}" for f in BUILTIN_FIELDS),
        },
    }


async def _run_search(req: SearchRequest, config: Config, db: Database) -> Dict[str, Any]:
    async with db.transaction() as executor:
        service = MetasSearchService(executor, config)
        await service.apply_index_hints()
        result = await service.search(req)
    return {
        "success": True,
        "data": {
            "rows": result.rows,
            "total": result.total,
            "page": result.page,
            "page_size": result.page_size,
            "has_more": result.has_more,
            "scanned": result.scanned,
            "truncated": result.truncated,
            "similarity": result.similarity,
            "query": result.query,
            "fields": result.fields,
            "order_by": result.order_by,
            "ast": result.ast,
        },
    }
