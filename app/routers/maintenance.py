"""v2 maintenance endpoints."""

from __future__ import annotations

from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Query, Request

from ..config import Config
from ..deps import config_dep, get_db, verify_v2
from ..services.db import Database
from ..security import Principal
from ..services.purge_service import PurgeService, resolve_request

router = APIRouter(tags=["v2-purge"])


@router.delete("/management/purge", summary="Physically purge soft-deleted data")
async def purge(
    request: Request,
    retention_days: Optional[int] = Query(
        None, ge=0, le=36500, description="Delete everything whose deleted_at is older than now - N days"
    ),
    include_embed: Optional[bool] = Query(
        None, description="Also sweep vector/chunk rows that are no longer attached to a knowledge item"
    ),
    dry_run: Optional[bool] = Query(None, description="Overrides purge.dry_run from config.json"),
    auth: Tuple[Principal, str] = Depends(verify_v2),
    config: Config = Depends(config_dep),
    db: Database = Depends(get_db),
) -> dict:
    req = resolve_request(
        retention_days=retention_days,
        include_embed=include_embed,
        config=config,
        dry_run=dry_run,
    )
    async with db.transaction() as executor:
        report = await PurgeService(executor, config).run(req)

    return {
        "success": True,
        "data": {
            "dry_run": report.dry_run,
            "retention_days": report.retention_days,
            "cutoff": report.cutoff,
            "include_embed": report.include_embed,
            "counts": {
                "knowledge_bases": report.knowledge_base_count,
                "knowledges": report.knowledge_count,
            },
            "matched": report.matched,
            "deleted": report.deleted,
            "orphan_matched": report.orphan_matched,
            "orphan_deleted": report.orphan_deleted,
            "skipped_tables": report.skipped_tables,
            "sample": {
                "knowledge_bases": report.knowledge_bases_sample,
                "knowledges": report.knowledges_sample,
            },
        },
    }
