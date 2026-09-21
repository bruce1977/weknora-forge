"""Publish orchestration (fixed order, matching the requirement):

    2.1 resolve/create the tag and create the article as a draft (a draft never triggers parsing)
    2.2 write custom_metadata
    2.3 flip the draft to publish (triggers parsing + vectorisation)
    2.4 optionally wait for post-processing (poll parse_status / enable_status)

Wait behaviour, timeouts and rollback come from config.json, so callers only send the
article itself. On failure the response uses the standard error envelope and - when
configured - the half-built draft is removed again.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..config import Config
from ..errors import ForgeError, bad_request, upstream_error
from ..logging import get_logger
from ..schemas import PublishRequest
from ..upstream import WeKnoraClient

logger = get_logger(__name__)


class PublishResult:
    """Minimal successful outcome of a publish run."""

    def __init__(
        self,
        knowledge_id: Optional[str] = None,
        tag_id: Optional[str] = None,
        tag_name: Optional[str] = None,
        parse_status: Optional[str] = None,
        enable_status: Optional[str] = None,
    ) -> None:
        self.knowledge_id = knowledge_id
        self.tag_id = tag_id
        self.tag_name = tag_name
        self.parse_status = parse_status
        self.enable_status = enable_status

    def as_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"success": True}
        for key in ("knowledge_id", "tag_id", "tag_name", "parse_status", "enable_status"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


class PublishService:
    def __init__(self, client: WeKnoraClient, config: Config) -> None:
        self.client = client
        self.config = config

    # ------------------------------------------------------------------ #
    async def publish(self, req: PublishRequest, api_key: str) -> PublishResult:
        settings = self.config.publish
        knowledge_id: Optional[str] = None
        tag_id: Optional[str] = None
        tag_name: Optional[str] = None

        try:
            # ---------------- 2.1 tag resolution ----------------
            if req.tag:
                if req.tag.id:
                    tag_id = req.tag.id
                    tag_name = req.tag.name
                elif req.tag.name:
                    tag, action = await self.client.ensure_tag(
                        req.kb_id,
                        api_key,
                        req.tag.name,
                        req.tag.color,
                        req.tag.sort_order,
                        req.tag.create_if_missing,
                    )
                    if tag is None:
                        raise bad_request(
                            f"Tag '{req.tag.name}' does not exist and create_if_missing=false",
                            error_id="TAG_NOT_FOUND",
                        )
                    tag_id = tag.get("id")
                    tag_name = tag.get("name")
                    logger.debug("tag %s -> %s", req.tag.name, action)

            # ---------------- 2.1 draft article ----------------
            draft = await self.client.create_manual_knowledge(
                req.kb_id,
                api_key,
                title=req.title,
                content=req.content,
                status="draft",
                tag_id=tag_id,
                channel=req.channel or settings.default_channel,
            )
            knowledge_id = draft.get("id")
            if not knowledge_id:
                raise upstream_error("WeKnora did not return a knowledge id for the draft")

            # ---------------- 2.2 custom metas ----------------
            await self._write_metas(req, api_key, knowledge_id)

            # ---------------- 2.3 publish ----------------
            published = await self.client.update_manual_knowledge(
                knowledge_id, api_key, title=req.title, content=req.content, status="publish"
            )
            latest = published or {}

            # ---------------- 2.4 wait ----------------
            if settings.wait and knowledge_id:
                latest = await self._wait(knowledge_id, api_key)

            return PublishResult(
                knowledge_id=knowledge_id,
                tag_id=tag_id,
                tag_name=tag_name,
                parse_status=latest.get("parse_status"),
                enable_status=latest.get("enable_status"),
            )
        except ForgeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise upstream_error(str(exc)) from exc

    # ------------------------------------------------------------------ #
    async def _write_metas(self, req: PublishRequest, api_key: str, knowledge_id: str) -> Dict[str, Any]:
        settings = self.config.publish
        payload = dict(req.custom_metas or {})
        try:
            if settings.merge_metas and payload:
                current = await self.client.get_knowledge(knowledge_id, api_key)
                existing = current.get("custom_metadata") or {}
                if isinstance(existing, str):
                    try:
                        existing = json.loads(existing)
                    except ValueError:
                        existing = {}
                payload = {**(existing or {}), **payload}
            if payload:
                await self.client.update_knowledge(knowledge_id, api_key, custom_metadata=payload)
            if req.description is not None:
                await self.client.update_knowledge(knowledge_id, api_key, description=req.description)
            return payload
        except ForgeError as exc:
            await self._maybe_rollback(knowledge_id, api_key)
            exc.message = f"{exc.message} (step: update custom_metadata)"
            raise
        except Exception as exc:  # noqa: BLE001
            await self._maybe_rollback(knowledge_id, api_key)
            raise upstream_error(f"Failed to update custom_metadata: {exc}") from exc

    # ------------------------------------------------------------------ #
    async def _wait(self, knowledge_id: str, api_key: str) -> Dict[str, Any]:
        settings = self.config.publish
        try:
            result = await self.client.wait_knowledge(
                knowledge_id,
                api_key,
                until=settings.wait_until,
                timeout=float(settings.timeout_seconds),
                interval=float(settings.poll_interval_seconds),
            )
            if result.get("timed_out"):
                logger.warning(
                    "knowledge %s did not reach %s within %ss (parse=%s enable=%s)",
                    knowledge_id, settings.wait_until, settings.timeout_seconds,
                    result.get("parse_status"), result.get("enable_status"),
                )
            return result.get("knowledge") or {}
        except Exception as exc:  # noqa: BLE001 - waiting must never fail the publish
            logger.warning("post-publish wait failed for %s: %s", knowledge_id, exc)
            return {}

    # ------------------------------------------------------------------ #
    async def _maybe_rollback(self, knowledge_id: str, api_key: str) -> None:
        if not self.config.publish.rollback_on_failure or not knowledge_id:
            return
        try:
            await self.client.delete_knowledge(knowledge_id, api_key)
            logger.info("rolled back draft %s", knowledge_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rollback of draft %s failed: %s", knowledge_id, exc)
