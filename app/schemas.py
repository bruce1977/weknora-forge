"""Request and response models for the v2 endpoints."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Publish
# --------------------------------------------------------------------------- #
class TagSpec(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    color: Optional[str] = None
    sort_order: Optional[int] = None
    create_if_missing: bool = True


class PublishRequest(BaseModel):
    """Publish one article: draft -> custom metas -> publish.

    wait / timeout / polling behaviour is not part of the request any more, it is
    configured per deployment in config.json (section ``publish``).
    """

    kb_id: str = Field(..., description="Knowledge base ID")
    title: str = Field(..., description="Article title")
    content: str = Field(..., description="Markdown body")
    description: Optional[str] = None
    tag: Optional[TagSpec] = None
    custom_metas: Dict[str, Any] = Field(default_factory=dict, description="Custom metadata (custom_metadata)")
    channel: Optional[str] = Field(None, description="Source channel; defaults to publish.default_channel")


class PublishResponse(BaseModel):
    """Success body - failures use the standard {success, error_id, error_message} envelope."""

    success: bool = True
    knowledge_id: Optional[str] = None
    tag_id: Optional[str] = None
    tag_name: Optional[str] = None
    parse_status: Optional[str] = None
    enable_status: Optional[str] = None


# --------------------------------------------------------------------------- #
# Metadata search
# --------------------------------------------------------------------------- #
class MetaSearchRequest(BaseModel):
    """Search knowledge by metadata, title, and tags.

    The `metas_query` field accepts FMQ expressions for custom metadata filtering.
    The `title` field enables full-text search on article titles.
    The `tags` field filters by tag names.
    """

    kb_id: Optional[str] = Field(None, description="Restrict to one knowledge base")
    metas_query: str = Field(..., description="FMQ expression for custom metadata, e.g. level >= 3 AND category = 'ops'")
    title: Optional[str] = Field(None, description="Full-text search on article title")
    tags: Optional[List[str]] = Field(None, description="Filter by tag names, e.g. ['ai', 'db']")
    page: int = Field(1, ge=1)
    # None => metas_search.default_page_size from config.json
    page_size: Optional[int] = Field(None, ge=1)
    case_insensitive: bool = False
    include_deleted: Optional[bool] = Field(None, description="Overrides metas_search.include_deleted")
    vector: Optional[Sequence[float]] = Field(None, description="Query embedding; enables similarity scoring")


class MetaParseRequest(BaseModel):
    query: str
