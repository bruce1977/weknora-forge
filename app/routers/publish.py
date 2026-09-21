"""v2 publish endpoint."""

from __future__ import annotations

from typing import Tuple

from fastapi import APIRouter, Depends, Request

from ..config import Config
from ..deps import config_dep, get_client, verify_v2
from ..errors import upstream_error
from ..schemas import PublishRequest
from ..security import Principal
from ..services.publish_service import PublishService

router = APIRouter(tags=["v2-publish"])


@router.post(
    "/publish",
    summary="Publish an article (tag -> draft -> custom metas -> publish)",
    responses={
        200: {"description": "success", "content": {"application/json": {"example": {"success": True, "knowledge_id": "6f1c...", "tag_id": "9a2b...", "tag_name": "tech"}}}}
    },
)
async def publish_article(
    payload: PublishRequest,
    request: Request,
    auth: Tuple[Principal, str] = Depends(verify_v2),
    config: Config = Depends(config_dep),
) -> dict:
    _principal, api_key = auth
    service = PublishService(get_client(), config)
    result = await service.publish(payload, api_key)
    if not result.knowledge_id:
        raise upstream_error("Publish finished without a knowledge id")
    return result.as_dict()
