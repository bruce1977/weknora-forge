"""Publish orchestration (fixed order, matching the requirement):

    2.1 resolve/create the tag and create the article as a draft (a draft never triggers parsing)
    2.2 write custom_metadata
    2.3 flip the draft to publish (triggers parsing + vectorisation)
    2.4 optionally wait for post-processing when the request sets sync=true
        (poll parse_status / enable_status)

Wait behaviour, timeouts and rollback come from config.json, so callers only send the
article itself. Without ``sync`` the call returns right after step 2.3. On failure the
response uses the standard error envelope and - when configured - the half-built draft
is removed again.
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
        tag_ids: Optional[list[str]] = None,
        tag_names: Optional[list[str]] = None,
        parse_status: Optional[str] = None,
        enable_status: Optional[str] = None,
    ) -> None:
        self.knowledge_id = knowledge_id
        self.tag_ids = tag_ids
        self.tag_names = tag_names
        self.parse_status = parse_status
        self.enable_status = enable_status

    def as_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"success": True}
        for key in (
            "knowledge_id",
            "tag_ids",
            "tag_names",
            "parse_status",
            "enable_status",
        ):
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
        # sync is a reserved feature: fail fast before any upstream write
        if req.sync and not settings.allow_sync:
            raise bad_request(
                "sync mode is reserved on this deployment: "
                "set publish.allow_sync=true to enable it",
                error_id="SYNC_DISABLED",
            )
        knowledge_id: Optional[str] = None
        resolved_tag_ids: list[str] = []
        resolved_tag_names: list[str] = []

        try:
            # ---------------- 2.1 tag resolution ----------------
            if req.tag_names:
                for name in req.tag_names:
                    if not name:
                        continue
                    tag, action = await self.client.ensure_tag(
                        req.kb_id,
                        api_key,
                        name,
                        create_if_missing=True,
                    )
                    if tag is None:
                        raise bad_request(
                            f"Tag '{name}' does not exist and create_if_missing=false",
                            error_id="TAG_NOT_FOUND",
                        )
                    resolved_tag_ids.append(tag.get("id"))
                    resolved_tag_names.append(tag.get("name"))
                    logger.debug("tag %s -> %s", name, action)

            # ---------------- 2.1 draft article ----------------
            draft = await self.client.create_manual_knowledge(
                req.kb_id,
                api_key,
                title=req.title,
                content=req.content,
                status="draft",
                tag_ids=resolved_tag_ids or None,
                channel=req.channel or settings.default_channel,
            )
            knowledge_id = draft.get("id")
            if not knowledge_id:
                raise upstream_error(
                    "WeKnora did not return a knowledge id for the draft"
                )

            # ---------------- 2.2 custom metas ----------------
            await self._write_metas(req, api_key, knowledge_id)

            # ---------------- 2.3 publish ----------------
            published = await self.client.update_manual_knowledge(
                knowledge_id,
                api_key,
                title=req.title,
                content=req.content,
                status="publish",
            )
            latest = published or {}

            # ---------------- 2.4 wait (sync mode) ----------------
            if req.sync and knowledge_id:
                latest = await self._wait(knowledge_id, api_key)

            return PublishResult(
                knowledge_id=knowledge_id,
                tag_ids=resolved_tag_ids or None,
                tag_names=resolved_tag_names or None,
                parse_status=latest.get("parse_status"),
                enable_status=latest.get("enable_status"),
            )
        except ForgeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise upstream_error(str(exc)) from exc

    # ------------------------------------------------------------------ #
    async def _write_metas(
        self, req: PublishRequest, api_key: str, knowledge_id: str
    ) -> Dict[str, Any]:
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
                await self.client.update_knowledge(
                    knowledge_id, api_key, custom_metadata=payload
                )
            if req.description is not None:
                await self.client.update_knowledge(
                    knowledge_id, api_key, description=req.description
                )
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
        # poll_interval <= 0 would spin hot: fall back to 1s so sync still works
        interval = (
            float(settings.poll_interval_seconds)
            if settings.poll_interval_seconds > 0
            else 1.0
        )
        try:
            result = await self.client.wait_knowledge(
                knowledge_id,
                api_key,
                until=settings.wait_until,
                timeout=float(settings.timeout_seconds),
                interval=interval,
            )
            if result.get("timed_out"):
                logger.warning(
                    "knowledge %s did not reach %s within %ss (parse=%s enable=%s)",
                    knowledge_id,
                    settings.wait_until,
                    settings.timeout_seconds,
                    result.get("parse_status"),
                    result.get("enable_status"),
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
