"""WeKnora upstream client.

Responsibilities:
1. Forward v1 requests verbatim (query / headers / multipart / SSE streams)
2. Expose the semantic calls the v2 business logic needs (tags, manual knowledge,
   metadata updates, polling)
3. Validate a caller's WeKnora API key against upstream with a short-lived cache
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

import httpx

from .config import Config
from .errors import ForgeError, upstream_error
from .logging import get_logger

logger = get_logger(__name__)

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}


@dataclass
class UpstreamResponse:
    status_code: int
    json_body: Any = None
    text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def data(self) -> Any:
        if isinstance(self.json_body, dict) and "data" in self.json_body:
            return self.json_body["data"]
        return self.json_body


class WeKnoraClient:
    def __init__(self, config: Config, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.config = config
        upstream = config.upstream
        self._client = httpx.AsyncClient(
            base_url=upstream.api_base,
            timeout=httpx.Timeout(upstream.timeout_seconds, connect=10.0),
            verify=upstream.verify_ssl,
            follow_redirects=False,
            transport=transport,
        )
        # sha256(key) -> (expires_at, valid, message, upstream_status)
        self._api_key_cache: Dict[str, Tuple[float, bool, str, int]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def reset_cache(self) -> None:
        self._api_key_cache.clear()

    # ------------------------------------------------------------------ #
    # Low level
    # ------------------------------------------------------------------ #
    def _headers(self, api_key: Optional[str], extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        if extra:
            for k, v in extra.items():
                if k.lower() in HOP_BY_HOP:
                    continue
                headers[k] = v
        return headers

    @staticmethod
    def _parse_body(resp: httpx.Response) -> Tuple[Any, str]:
        body_text = resp.text
        ctype = resp.headers.get("content-type", "")
        parsed: Any = None
        if ctype.startswith("application/json") and body_text:
            try:
                parsed = resp.json()
            except ValueError:
                parsed = None
        return parsed, body_text

    async def call(
        self,
        method: str,
        path: str,
        *,
        api_key: Optional[str] = None,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> UpstreamResponse:
        path = path if path.startswith("/") else f"/{path}"
        kwargs: Dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        if files is not None:
            kwargs["files"] = files
        if timeout:
            kwargs["timeout"] = timeout

        try:
            resp = await self._client.request(
                method.upper(),
                path,
                params=dict(params) if params else None,
                headers=self._headers(api_key, headers),
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise upstream_error(f"Upstream request timed out: {method} {path}", str(exc), 504) from exc
        except httpx.HTTPError as exc:
            raise upstream_error(f"Upstream connection failed: {method} {path}", str(exc)) from exc

        json_body_out, body_text = self._parse_body(resp)
        result = UpstreamResponse(
            status_code=resp.status_code,
            json_body=json_body_out,
            text=body_text,
            headers=dict(resp.headers),
        )
        if resp.status_code not in (200, 201, 202, 204):
            detail: Any = result.json_body
            message = "Upstream returned an error"
            if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
                message = detail["error"].get("message", message)
            raise upstream_error(
                f"{message} ({method} {path} -> {resp.status_code})", detail, self._map_status(resp.status_code)
            )
        return result

    async def raw(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        headers: Optional[Mapping[str, str]] = None,
        body: bytes = b"",
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        """Passthrough variant: no JSON parsing, returns the raw httpx.Response (used for streaming)."""
        url = httpx.URL(path if path.startswith("/") else f"/{path}")
        if query:
            url = url.copy_with(query=query.encode() if isinstance(query, str) else query)
        req = self._client.build_request(
            method.upper(),
            url,
            headers=self._headers(None, headers),
            content=body or None,
            timeout=timeout if timeout else None,
        )
        try:
            return await self._client.send(req, stream=True)
        except httpx.HTTPError as exc:
            raise upstream_error(f"Upstream connection failed: {method} {path}", str(exc)) from exc

    @staticmethod
    def _map_status(code: int) -> int:
        if code in (401, 403):
            return code
        if code == 404:
            return 404
        if code == 429:
            return 429
        return 502

    # ------------------------------------------------------------------ #
    # API key validation (layer 1 of v2 auth)
    # ------------------------------------------------------------------ #
    async def validate_api_key(self, api_key: str, force: bool = False) -> Tuple[bool, str, int]:
        """Ask WeKnora whether this key works: GET {upstream}/knowledge-bases.

        Returns (valid, message, upstream_status); upstream_status lets the caller tell
        "the key is rejected" apart from "WeKnora is unreachable".
        """
        upstream = self.config.upstream
        auth = self.config.auth
        if not api_key:
            return False, "Missing WeKnora API key", 0

        cache_key = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        cached = self._api_key_cache.get(cache_key)
        now = time.time()
        if cached and not force and cached[0] > now:
            return cached[1], cached[2], cached[3]

        path = upstream.api_key_validate_path or "/knowledge-bases"
        status = 0
        try:
            resp = await self._client.get(
                path if path.startswith("/") else f"/{path}",
                headers=self._headers(api_key),
                timeout=min(upstream.timeout_seconds, 15.0),
            )
            status = resp.status_code
            if 200 <= status < 300:
                valid, msg = True, "valid"
            elif status in (401, 403):
                valid, msg = False, "WeKnora rejected the API key"
            else:
                valid, msg = False, f"WeKnora responded with HTTP {status}"
        except httpx.TimeoutException as exc:
            status = 504
            valid, msg = False, f"Upstream timed out while validating the API key: {exc}"
        except httpx.HTTPError as exc:
            status = 502
            valid, msg = False, f"Upstream unreachable while validating the API key: {exc}"

        if len(self._api_key_cache) > auth.api_key_cache_max_entries:
            self._api_key_cache.clear()
        ttl = auth.api_key_cache_ttl_seconds if valid else max(auth.api_key_negative_cache_ttl_seconds, 0)
        self._api_key_cache[cache_key] = (now + max(ttl, 1), valid, msg, status)
        return valid, msg, status

    # ------------------------------------------------------------------ #
    # Semantic calls
    # ------------------------------------------------------------------ #
    async def list_tags(self, kb_id: str, api_key: str, keyword: str = "", page_size: int = 100):
        resp = await self.call(
            "GET",
            f"/knowledge-bases/{kb_id}/tags",
            api_key=api_key,
            params={"page": 1, "page_size": page_size, **({"keyword": keyword} if keyword else {})},
        )
        payload = resp.data or {}
        return list(payload.get("data", [])) if isinstance(payload, dict) else list(payload or [])

    async def find_tag_by_name(self, kb_id: str, api_key: str, name: str) -> Optional[Dict[str, Any]]:
        for tag in await self.list_tags(kb_id, api_key, keyword=name):
            if tag.get("name") == name:
                return tag
        return None

    async def create_tag(
        self,
        kb_id: str,
        api_key: str,
        name: str,
        color: Optional[str] = None,
        sort_order: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"name": name}
        if color:
            payload["color"] = color
        if sort_order is not None:
            payload["sort_order"] = sort_order
        resp = await self.call("POST", f"/knowledge-bases/{kb_id}/tags", api_key=api_key, json_body=payload)
        return resp.data or {}

    async def ensure_tag(
        self,
        kb_id: str,
        api_key: str,
        name: str,
        color: Optional[str] = None,
        sort_order: Optional[int] = None,
        create_if_missing: bool = True,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """Return (tag, action); action is one of created | reused | missing."""
        if not name:
            return None, "none"
        existing = await self.find_tag_by_name(kb_id, api_key, name)
        if existing:
            return existing, "reused"
        if not create_if_missing:
            return None, "missing"
        tag = await self.create_tag(kb_id, api_key, name, color, sort_order)
        return tag, "created"

    async def create_manual_knowledge(
        self,
        kb_id: str,
        api_key: str,
        title: str,
        content: str,
        status: str = "draft",
        tag_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"title": title, "content": content, "status": status}
        if tag_id:
            payload["tag_id"] = tag_id
        if channel:
            payload["channel"] = channel
        resp = await self.call("POST", f"/knowledge-bases/{kb_id}/knowledge/manual", api_key=api_key, json_body=payload)
        return resp.data or {}

    async def update_knowledge(
        self,
        knowledge_id: str,
        api_key: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        custom_metadata: Optional[Dict[str, Any]] = None,
    ) -> Any:
        payload: Dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if custom_metadata is not None:
            payload["custom_metadata"] = custom_metadata
        if not payload:
            return {"skipped": True}
        resp = await self.call("PUT", f"/knowledge/{knowledge_id}", api_key=api_key, json_body=payload)
        return resp.data

    async def update_manual_knowledge(
        self,
        knowledge_id: str,
        api_key: str,
        *,
        title: Optional[str] = None,
        content: Optional[str] = None,
        status: Optional[str] = None,
        tag_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if content is not None:
            payload["content"] = content
        if status is not None:
            payload["status"] = status
        if tag_id is not None:
            payload["tag_id"] = tag_id
        if not payload:
            return {}
        resp = await self.call("PUT", f"/knowledge/manual/{knowledge_id}", api_key=api_key, json_body=payload)
        return resp.data or {}

    async def get_knowledge(self, knowledge_id: str, api_key: str) -> Dict[str, Any]:
        resp = await self.call("GET", f"/knowledge/{knowledge_id}", api_key=api_key)
        return resp.data or {}

    async def delete_knowledge(self, knowledge_id: str, api_key: str) -> None:
        await self.call("DELETE", f"/knowledge/{knowledge_id}", api_key=api_key)

    async def wait_knowledge(
        self,
        knowledge_id: str,
        api_key: str,
        *,
        until: str = "enabled",
        timeout: float = 300.0,
        interval: float = 3.0,
    ) -> Dict[str, Any]:
        """Poll until post-publish processing reaches the requested state.

        until:
          - completed: parse_status == completed
          - enabled:   enable_status == enabled
          - terminal:  parse_status in {completed, failed, cancelled}
        """
        deadline = time.time() + timeout
        last: Dict[str, Any] = {}
        attempts = 0
        while True:
            last = await self.get_knowledge(knowledge_id, api_key)
            attempts += 1
            parse_status = str(last.get("parse_status") or "")
            enable_status = str(last.get("enable_status") or "")
            done = (
                (until == "completed" and parse_status == "completed")
                or (until == "enabled" and enable_status == "enabled")
                or (until == "terminal" and parse_status in {"completed", "failed", "cancelled"})
            )
            if done or time.time() >= deadline:
                return {
                    "knowledge": last,
                    "attempts": attempts,
                    "timed_out": not done,
                    "parse_status": parse_status,
                    "enable_status": enable_status,
                }
            await asyncio.sleep(interval)
