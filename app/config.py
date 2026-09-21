"""Configuration loading for WeKnora Forge.

Forge is configured by ONE JSON file: ``config.json`` next to the application package
(override the path with the ``FORGE_CONFIG`` environment variable).

Secrets never need to be written into that file: every ``${VAR}`` placeholder found in
the file is expanded from the process environment at load time, with the optional
``${VAR:-default}`` form supported as well.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote_plus

from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_FILENAME = "config.json"
CONFIG_PATH_ENV = "FORGE_CONFIG"

# ${VAR} and ${VAR:-default}
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _expand_env(value: Any) -> Any:
    """Recursively expand environment placeholders inside strings."""
    if isinstance(value, str):
        return _ENV_PLACEHOLDER.sub(
            lambda m: os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else ""),
            value,
        )
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def default_config_path() -> Path:
    override = os.environ.get(CONFIG_PATH_ENV, "").strip()
    if override:
        return Path(override)
    # <repo>/config.json  ->  app/config.py lives one level below the repo root
    return Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_FILENAME


def is_identifier(value: str) -> bool:
    """True when the string is a safe bare SQL identifier (schema drift guard)."""
    return bool(_IDENTIFIER.match(value or ""))


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
class ServiceConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    workers: int = 1


class ProxyConfig(BaseModel):
    """HTTPS is terminated in front of this service (Cloudflare, nginx, traefik...).

    Forge listens on plain HTTP inside the private network; the public entry point is
    HTTPS. Only requests arriving from a *trusted* peer may assert their own scheme /
    client address through ``X-Forwarded-*`` - see ``app/proxy.py``.
    """

    enabled: bool = True
    # "*" (trust anything) or a list of IPs / CIDRs / hostnames. Defaults cover the
    # loopback and the private ranges a tunnel or sidecar container lives in.
    trusted_proxies: List[str] = Field(
        default_factory=lambda: [
            "127.0.0.1",
            "::1",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
        ]
    )
    # Checked in order; the first header carrying a value wins. Cloudflare overwrites
    # CF-Connecting-IP on every request, so it is the most trustworthy source.
    client_ip_headers: List[str] = Field(default_factory=lambda: ["cf-connecting-ip", "x-real-ip"])
    # Re-emit X-Forwarded-For / -Proto / -Host towards WeKnora describing the caller
    forward_client_info: bool = True


class UpstreamConfig(BaseModel):
    """Native WeKnora instance."""

    base_url: str = "http://localhost:8080"
    api_prefix: str = "/api/v1"
    timeout_seconds: float = 60.0
    verify_ssl: bool = True
    # Optional server-held credential used when the caller sends no API key at all
    default_api_key: str = ""
    # Endpoint used to validate a caller API key (cheap + always readable)
    api_key_validate_path: str = "/knowledge-bases?page=1&page_size=1"
    forward_request_id: bool = True

    @property
    def api_base(self) -> str:
        prefix = (self.api_prefix or "").strip("/")
        base = self.base_url.rstrip("/")
        return f"{base}/{prefix}" if prefix else base


class AuthConfig(BaseModel):
    """Second-factor verification + WeKnora API key validation."""

    # off  = no verification (trusted network only)
    # hmac = HMAC-SHA256 signature over METHOD + FULL_PATH, keyed by the caller's API key
    mode: Literal["off", "hmac"] = "hmac"
    require_on_v1: bool = True
    require_on_v2: bool = True

    hmac_header_key: str = "X-Forge-Key"
    hmac_header_signature: str = "X-Forge-Signature"

    # There is no timestamp anymore: a signature is single-use for this TTL, which is
    # what gives the request its short validity window.
    signature_cache_ttl_seconds: int = 300
    signature_cache_max_entries: int = 50000
    # Only state-changing methods are deduplicated; repeating GET is legitimate.
    signature_cache_methods: List[str] = Field(default_factory=lambda: ["POST", "PUT", "PATCH", "DELETE"])

    # L1 check: validate the API key against upstream GET /api/v1/knowledge-bases
    api_key_cache_ttl_seconds: int = 300
    api_key_negative_cache_ttl_seconds: int = 30
    api_key_cache_max_entries: int = 5000

    @property
    def signature_methods(self) -> set:
        return {m.strip().upper() for m in self.signature_cache_methods if m.strip()}


class PublishConfig(BaseModel):
    """Server-side behaviour of POST /api/v2/publish."""

    wait: bool = True
    wait_until: Literal["enabled", "completed", "terminal"] = "enabled"
    timeout_seconds: int = 300
    poll_interval_seconds: float = 3.0
    default_channel: str = "api"
    merge_metas: bool = True
    rollback_on_failure: bool = True


class DatabaseConfig(BaseModel):
    """Direct PostgreSQL access (metadata search + purge)."""

    dsn: str = ""
    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: str = ""
    name: str = "WeKnora"
    sslmode: str = "disable"
    pool_size: int = 5
    max_overflow: int = 5
    statement_timeout_ms: int = 30000

    @property
    def sqlalchemy_dsn(self) -> str:
        """DSN handed to SQLAlchemy.

        ``sslmode`` is deliberately NOT appended as a query parameter: the asyncpg
        dialect forwards unknown query parameters straight into ``asyncpg.connect()``,
        which has no ``sslmode`` argument and would raise a TypeError. TLS is applied
        through ``connect_args`` instead (see ``services/db.py``).
        """
        if self.dsn.strip():
            return self.dsn.strip()
        user = quote_plus(self.user or "")
        password = f":{quote_plus(self.password)}" if self.password else ""
        return f"postgresql+asyncpg://{user}{password}@{self.host}:{self.port}/{self.name}"

    @property
    def configured(self) -> bool:
        return bool(self.dsn.strip() or self.host.strip())


class SortSpec(BaseModel):
    column: str
    desc: bool = True


class MetasJoinConfig(BaseModel):
    """Optional joins used to enrich / resolve result columns."""

    knowledge_base_name: bool = True
    tag_name: bool = True


class VectorConfig(BaseModel):
    """Vector-similarity scoring (only active when the caller supplies a vector).

    The distance is computed per knowledge row with a LATERAL sub-query so that a
    knowledge item owning several chunks still yields exactly one row, scored by its
    closest chunk. Rows without any embedding row get a NULL score (pushed last).
    """

    table: str = "embeddings"
    column: str = "embedding"
    knowledge_column: str = "knowledge_id"
    # <=> cosine | <-> L2 | <#> inner product
    distance_operator: Literal["<=>", "<->", "<#>"] = "<=>"
    # {distance} is replaced by the per-row minimal distance expression
    similarity_expression: str = "1 - ({distance})"
    # Index behaviour overrides; only applied when > 0 (SET LOCAL inside the request txn)
    default_probes: int = 0
    default_ef_search: int = 0
    default_beam_factor: int = 0

    @field_validator("similarity_expression")
    @classmethod
    def _check_expression(cls, v: str) -> str:
        if ";" in v:
            raise ValueError("similarity_expression must not contain ';'")
        return v


class MetasSearchConfig(BaseModel):
    """Tables, columns and defaults for the Custom Metas search."""

    table: str = "knowledges"
    metadata_column: str = "custom_metadata"
    knowledge_base_table: str = "knowledge_bases"
    tag_table: str = "knowledge_tags"
    tag_relation_table: str = "knowledge_tag_relations"

    join: MetasJoinConfig = Field(default_factory=MetasJoinConfig)
    include_deleted: bool = False

    # Result columns, resolved by alias. "kb_name" / "tag_name" come from the joins,
    # "file_name" falls back through the list below when the column does not exist,
    # "similarity" is the vector score (always NULL when no vector is supplied).
    result_column: List[str] = Field(
        default_factory=lambda: ["id", "title", "file_name", "similarity", "kb_name", "tag_name"]
    )
    file_name_candidate: List[str] = Field(
        default_factory=lambda: ["file_name", "filename", "file_path", "source_url", "source", "title"]
    )
    vector: VectorConfig = Field(default_factory=VectorConfig)

    default_order: List[SortSpec] = Field(
        default_factory=lambda: [SortSpec(column="similarity", desc=True), SortSpec(column="updated_at", desc=True)]
    )
    max_rows: int = 5000
    default_page_size: int = 20
    max_page_size: int = 200
    # Extra SQL always ANDed into the WHERE clause (e.g. a tenant restriction).
    # WARNING: server-side config only - never expose this to callers.
    extra_where: str = ""

    @field_validator("extra_where")
    @classmethod
    def _check_extra_where(cls, v: str) -> str:
        if ";" in v:
            raise ValueError("extra_where must not contain ';'")
        return v.strip()


class PurgeTable(BaseModel):
    table: str
    knowledge_base_column: str = "knowledge_base_id"
    knowledge_column: str = "knowledge_id"


class PurgeConfig(BaseModel):
    """Defaults for DELETE /api/v2/management/purge."""

    dry_run: bool = True
    default_retention_days: int = 30
    include_embed: bool = False
    max_rows: int = 200000
    # Child tables first, parents last; knowledge_bases itself is always deleted last.
    tables: List[PurgeTable] = Field(default_factory=list)
    # Orphan sweep: rows pointing at knowledge / chunks that no longer exist.
    orphan_tables: List[PurgeTable] = Field(default_factory=list)


class Config(BaseModel):
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    upstream: UpstreamConfig = Field(default_factory=UpstreamConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    metas_search: MetasSearchConfig = Field(default_factory=MetasSearchConfig)
    purge: PurgeConfig = Field(default_factory=PurgeConfig)

    raw: Dict[str, Any] = Field(default_factory=dict, exclude=True)


_DEFAULT_PURGE_TABLES: List[Dict[str, str]] = [
    {"table": "embeddings", "knowledge_base_column": "knowledge_base_id", "knowledge_column": "knowledge_id"},
    {"table": "chunks", "knowledge_base_column": "knowledge_base_id", "knowledge_column": "knowledge_id"},
    {"table": "chunk_revisions", "knowledge_base_column": "knowledge_base_id", "knowledge_column": "knowledge_id"},
    {"table": "knowledge_tag_relations", "knowledge_base_column": "", "knowledge_column": "knowledge_id"},
    {"table": "knowledge_processing_spans", "knowledge_base_column": "", "knowledge_column": "knowledge_id"},
    {"table": "wiki_page_revisions", "knowledge_base_column": "knowledge_base_id", "knowledge_column": ""},
    {"table": "wiki_page_issues", "knowledge_base_column": "knowledge_base_id", "knowledge_column": ""},
    {"table": "wiki_pages", "knowledge_base_column": "knowledge_base_id", "knowledge_column": ""},
    {"table": "wiki_folders", "knowledge_base_column": "knowledge_base_id", "knowledge_column": ""},
    {"table": "data_sources", "knowledge_base_column": "knowledge_base_id", "knowledge_column": ""},
    {"table": "knowledges", "knowledge_base_column": "", "knowledge_column": "id"},
    {"table": "kb_shares", "knowledge_base_column": "knowledge_base_id", "knowledge_column": ""},
    {"table": "knowledge_tags", "knowledge_base_column": "knowledge_base_id", "knowledge_column": ""},
]

_DEFAULT_ORPHAN_TABLES: List[Dict[str, str]] = [
    {"table": "embeddings", "knowledge_base_column": "knowledge_base_id", "knowledge_column": "knowledge_id"},
    {"table": "chunks", "knowledge_base_column": "knowledge_base_id", "knowledge_column": "knowledge_id"},
    {"table": "chunk_revisions", "knowledge_base_column": "knowledge_base_id", "knowledge_column": "knowledge_id"},
]


@lru_cache(maxsize=8)
def load_config(path: Optional[str] = None) -> Config:
    """Load (and cache) the configuration file."""
    resolved = Path(path) if path else default_config_path()
    data: Dict[str, Any] = {}
    if resolved.is_file():
        data = json.loads(resolved.read_text(encoding="utf-8"))
    data = _expand_env(data)

    if not data.get("purge", {}).get("tables"):
        data.setdefault("purge", {})["tables"] = _DEFAULT_PURGE_TABLES
    if not data.get("purge", {}).get("orphan_tables"):
        data.setdefault("purge", {})["orphan_tables"] = _DEFAULT_ORPHAN_TABLES

    config = Config.model_validate(data)
    config.raw = data
    return config


def reload_config(path: Optional[str] = None) -> Config:
    """Clear the cache and load again (used by tests)."""
    load_config.cache_clear()
    return load_config(path)


def get_config() -> Config:
    return load_config()


def config_template() -> Dict[str, Any]:
    """Return a documented default configuration (used by --print-config)."""
    cfg = Config()
    purge = cfg.purge
    return {
        "service": cfg.service.model_dump(),
        "proxy": cfg.proxy.model_dump(),
        "upstream": cfg.upstream.model_dump(),
        "auth": cfg.auth.model_dump(),
        "publish": cfg.publish.model_dump(),
        "database": cfg.database.model_dump(),
        "metas_search": cfg.metas_search.model_dump(),
        "purge": {
            **purge.model_dump(),
            "tables": _DEFAULT_PURGE_TABLES,
            "orphan_tables": _DEFAULT_ORPHAN_TABLES,
        },
    }
