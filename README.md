# WeKnora Forge

> 中文版见 [README_CN.md](./README_CN.md)

An **extension layer** for the native WeKnora API: a thin passthrough plus the pieces the
native API is missing, written in Python 3 + FastAPI and packaged as a container.

```
public client ──HTTPS──► Cloudflare ──HTTP──► Forge ──HTTP──► WeKnora (/api/v1)
internal job  ───────────HTTP──────────────►  │
                                              ├─ v1/*  verbatim passthrough + second factor
                                              └─ v2/*  publish / metadata search / purge
```

What the native API lacks, and how Forge covers it:

| Gap | How it is filled | Endpoint |
| --- | --- | --- |
| No second credential beyond the API key | HMAC-SHA256 signature header, keyed by the API key itself | every v1 / v2 route |
| Manual knowledge needs multi-step choreography (draft → metadata → publish) | one server-side call, rolled back on failure | `POST /api/v2/publish` |
| Only title/tag/time filtering, no `custom_metadata` search | FMQ query language pushed down to PostgreSQL JSONB | `POST·GET /api/v2/knowledge/search` |
| Soft delete only (writes `deleted_at`), no physical purge | ordered cascade delete + orphan vector sweep | `DELETE /api/v2/management/purge` |

---

## 1. Quick start

### Docker Compose

```bash
cp .env.example .env          # at minimum WEKNORA_BASE_URL and the DB_* values
docker compose up -d --build
curl http://localhost:8000/healthz                 # {"status":"ok",...}
curl http://localhost:8000/api/v2/health           # signed, see chapter 3
```

### Local development

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt          # Linux/macOS

uvicorn app.main:app --reload --port 8000 --no-proxy-headers
python scripts/show_config.py                       # effective config, secrets masked
```

> `--no-proxy-headers` is required: forwarding headers are handled once, in `app/proxy.py`.
> Uvicorn's own handling only trusts 127.0.0.1, so it would silently ignore `X-Forwarded-*`
> coming from a container or tunnel (see chapter 2).

Tests: `python -m pytest tests -q` (**98 tests**, upstream mocked with respx - no real
WeKnora instance and no database required).

---

## 2. API Reference

### Endpoints Overview

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| **System** | | | |
| `GET` | `/healthz` | ✗ | Health check |
| `GET` | `/` | ✗ | Service info |
| **v1 Passthrough** | | | |
| `ANY` | `/api/v1/{path}` | HMAC | Forward to WeKnora |
| **v2 System** | | | |
| `GET` | `/api/v2/health` | HMAC | Dependency health |
| **v2 Publish** | | | |
| `POST` | `/api/v2/publish` | HMAC | Publish knowledge |
| **v2 Metadata Search** | | | |
| `POST` | `/api/v2/knowledge/search` | HMAC | Search by metadata, title, tags |
| `POST` | `/api/v2/metas/parse` | HMAC | Parse FMQ expression |
| `GET` | `/api/v2/metas/grammar` | HMAC | FMQ syntax reference |
| **v2 Maintenance** | | | |
| `DELETE` | `/api/v2/management/purge` | HMAC | Purge soft-deleted data |

> **Auth**: ✗ = No authentication, HMAC = Requires `X-Forge-Signature` header

---

### 2.1 System Endpoints

#### `GET /healthz` {#get-healthz}

Health check endpoint. Returns service status, name and version. No authentication required.

**Response**

```JSON
{
  "status": "ok",
  "name": "weknora-forge",
  "version": "1.0.0"
}
```

---

#### `GET /` {#get-root}

Returns service information including upstream address, v1/v2 prefixes, and auth mode.

**Response**

```JSON
{
  "service": "weknora-forge",
  "version": "1.0.0",
  "upstream": "http://localhost:8080",
  "v1_prefix": "/api/v1",
  "v2_prefix": "/api/v2",
  "auth_mode": "hmac"
}
```

---

### 2.2 v1 Passthrough

#### `ANY /api/v1/{path}` {#any-api-v1-path}

Forwards any HTTP method to the upstream WeKnora API verbatim. Supports all methods (GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS).

**Headers forwarded**: Method, query, request headers (`X-API-Key` is forcibly set to caller's credential), body (multipart included)

**Response**: `StreamingResponse` relaying the byte stream (SSE works as-is)

**Headers dropped**: `Host`, `Content-Length`, `X-API-Key`, `X-Forge-*`, `CF-*`, `X-Forwarded-*`

```bash
curl -H "X-API-Key: sk-xxx" -H "X-Forge-Signature: ..." \
  http://localhost:8000/api/v1/knowledge-bases?page=1&page_size=20
```

---

### 2.3 v2 System Endpoints

#### `GET /api/v2/health` {#get-api-v2-health}

Checks upstream WeKnora connectivity and PostgreSQL database connectivity.

**Response**

```JSON
{
  "status": "ok",
  "upstream": "ok",
  "database": "ok"
}
```

---

### 2.4 v2 Publish

#### `POST /api/v2/publish` {#post-api-v2-publish}

Executes the full publish orchestration: create/get tag → create draft → set custom metadata → publish.

**Request**

```JSON
{
  "kb_id": "kb-00000001",
  "title": "Milvus cluster deployment guide",
  "content": "# Milvus\n\n## Planning\n...",
  "description": "Optional description",
  "tag": {
    "name": "docs",
    "color": "#1890ff",
    "create_if_missing": true
  },
  "custom_metas": {
    "level": 3,
    "category": "ops",
    "tags": ["db", "ai"]
  },
  "channel": "manual"
}
```

**Response (success)**

```JSON
{
  "success": true,
  "knowledge_id": "k-00000001",
  "tag_id": "t-00000001",
  "status": "published"
}
```

**Response (failure)**

```JSON
{
  "success": false,
  "error_id": "UPSTREAM_ERROR",
  "error_message": "..."
}
```

> With `publish.wait=true` the call blocks until post-processing finishes. Behind Cloudflare keep
> it `false`: the 100-second origin limit would return 524 while the work continues.

---

### 2.5 v2 Metadata Search

#### `POST /api/v2/knowledge/search` {#post-api-v2-knowledge-search}

Search knowledge by custom metadata, title, and tags. The `metas_query` field accepts FMQ expressions for custom metadata filtering, `title` enables full-text search on article titles, and `tags` filters by tag names.

**Request**

```JSON
{
  "kb_id": "kb-00000001",
  "metas_query": "level >= 3 AND category = 'ops'",
  "title": "Milvus",
  "tags": ["ai", "db"],
  "page": 1,
  "page_size": 20,
  "case_insensitive": true,
  "include_deleted": false
}
```

**Response**

```JSON
{
  "success": true,
  "data": {
    "rows": [
      {
        "id": "k-00000001",
        "title": "Milvus cluster deployment guide",
        "custom_metadata": {...},
        "similarity": 0.95
      }
    ],
    "total": 100,
    "page": 1,
    "page_size": 20,
    "has_more": true,
    "scanned": 150,
    "truncated": false
  }
}
```

---

#### `POST /api/v2/metas/parse` {#post-api-v2-metas-parse}

Parse FMQ expression and return AST (debug tool).

**Request**

```JSON
{
  "query": "level >= 3 AND tags CONTAINS 'ai'"
}
```

**Response**

```JSON
{
  "success": true,
  "data": {
    "query": "level >= 3 AND tags CONTAINS 'ai'",
    "ast": {...},
    "fields": ["level", "tags"]
  }
}
```

---

#### `GET /api/v2/metas/grammar` {#get-api-v2-metas-grammar}

Returns FMQ syntax reference and built-in fields list.

```bash
curl -H "X-API-Key: sk-xxx" -H "X-Forge-Signature: ..." \
  http://localhost:8000/api/v2/metas/grammar
```

---

### 2.6 v2 Maintenance

#### `DELETE /api/v2/management/purge` {#delete-api-v2-management-purge}

Purges soft-deleted data based on retention days. Executes cascade delete in table order.

**Query Parameters**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `retention_days` | 30 | Delete rows with `deleted_at` older than N days |
| `include_embed` | false | Also sweep orphan vector/chunk rows |
| `dry_run` | true | Count only, don't delete |

**Response**

```JSON
{
  "success": true,
  "data": {
    "matched": 150,
    "deleted": 150,
    "orphan_matched": 500,
    "orphan_deleted": 500,
    "skipped_tables": [],
    "sample": ["k-00000001", "k-00000002"]
  }
}
```

> ⚠️ Always check with `dry_run=true` first. `purge` is a long-running call - invoke from internal network.

---

## 3. Deployment

Forge listens on plain HTTP (default port 8000). In production, place a reverse proxy (Cloudflare, Nginx, etc.) in front to terminate TLS.

```
client ──HTTPS──► reverse proxy ──HTTP──► Forge:8000 ──HTTP──► WeKnora
```

### 3.1 Proxy configuration

If Forge sits behind a reverse proxy, enable proxy awareness in `config.json`:

```JSON
"proxy": {
  "enabled": true,
  "trusted_proxies": ["127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
  "client_ip_headers": ["x-forwarded-for", "x-real-ip"],
  "forward_client_info": true
}
```

- `trusted_proxies`: IPs/CIDRs of your reverse proxy(s). Only these peers can set `X-Forwarded-*` headers.
- `client_ip_headers`: headers to read the real client IP from (first valid wins).
- `forward_client_info`: rebuild and forward `X-Forwarded-For/Proto/Host` to WeKnora.

### 3.2 Verify deployment

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/api/v2/whoami  # requires HMAC signature
```

---

## 4. Authentication: two layers

| Layer | Credential | Verification |
| --- | --- | --- |
| 1 | WeKnora `X-API-Key` | **v2 only**: validated against `GET {upstream}/api/v1/knowledge-bases`, cached per key (300 s positive / 30 s negative). `401` means "key rejected", `5xx` means "WeKnora unreachable" (HTTP 502) - the two stay distinguishable. **v1 skips this** and stays a transparent pipe, letting WeKnora return its own status code |
| 2 (second factor) | `X-Forge-Signature` | HMAC-SHA256 keyed by the caller's own API key |

### 4.1 Signature scheme

```
payload   = HTTP_METHOD + HTTP_FULL_PATH        # e.g. "POST/api/v2/publish?dry_run=1"
signature = hex(HMAC_SHA256(api_key, payload))  # -> X-Forge-Signature
```

- No timestamp, no nonce, no body digest: **the API key is the signing key**, so no extra shared
  secret has to be distributed.
- `HTTP_FULL_PATH` is the raw path plus the raw query string. Forge signs the ASGI `raw_path`
  and performs **no normalisation and no decoding**: percent-encoding is part of the signature.
- Freshness comes from single-use signatures, see 2.4.
- Headers: `X-API-Key` (also the signing key) and `X-Forge-Signature`; `X-Forge-Key` is an
  optional human-readable label used in logs.

```bash
python scripts/hmac_request.py --base http://localhost:8000 --api-key sk-xxxxx \
    GET /api/v2/whoami

# print the headers only (paste into curl / Postman / a job runner)
python scripts/hmac_request.py --api-key sk-xxxxx --dry-run DELETE '/api/v2/management/purge?retention_days=30'

# disable the second factor (fully trusted internal network only)
# config.json -> auth.mode = "off"
```

---

## 5. Configuration

**`config.json` is the single source of truth** (point `FORGE_CONFIG` elsewhere if needed). Every
`${VAR}` / `${VAR:-default}` placeholder in it is expanded from the process environment at load
time, so secrets never have to be written into the file:

```jsonc
{
  "service":  { "host": "0.0.0.0", "port": 8000, "log_level": "INFO", "workers": 1 },
  "proxy":    { "enabled": true, "trusted_proxies": ["127.0.0.1", "172.16.0.0/12", "..."],
                "client_ip_headers": ["cf-connecting-ip", "x-real-ip"], "forward_client_info": true },
  "upstream": { "base_url": "${WEKNORA_BASE_URL:-http://localhost:8080}", "api_prefix": "/api/v1",
                "timeout_seconds": 60, "verify_ssl": true, "default_api_key": "${WEKNORA_DEFAULT_API_KEY:-}",
                "api_key_validate_path": "/knowledge-bases?page=1&page_size=1" },
  "auth":     { "mode": "hmac", "require_on_v1": true, "require_on_v2": true,
                "hmac_header_key": "X-Forge-Key", "hmac_header_signature": "X-Forge-Signature",
                "signature_cache_ttl_seconds": 300, "signature_cache_max_entries": 50000,
                "signature_cache_methods": ["POST", "PUT", "PATCH", "DELETE"],
                "api_key_cache_ttl_seconds": 300, "api_key_negative_cache_ttl_seconds": 30 },
  "publish":  { "wait": false, "wait_until": "enabled", "timeout_seconds": 90,
                "poll_interval_seconds": 3.0, "merge_metas": true, "rollback_on_failure": true },
  "database": { "dsn": "${FORGE_DB_DSN:-}", "host": "${DB_HOST:-localhost}", "port": "${DB_PORT:-5432}",
                "user": "${DB_USER:-postgres}", "password": "${DB_PASSWORD:-}", "name": "${DB_NAME:-WeKnora}",
                "sslmode": "${DB_SSLMODE:-disable}", "pool_size": 5, "statement_timeout_ms": 30000 },
  "metas_search": { "table": "knowledges", "metadata_column": "custom_metadata", "include_deleted": false,
                    "result_column": ["id", "title", "file_name", "similarity", "kb_name", "tag_name"],
                    "vector": { "distance_operator": "<=>", "similarity_expression": "1 - ({distance})" },
                    "max_rows": 5000, "default_page_size": 20, "extra_where": "" },
  "purge":    { "dry_run": true, "default_retention_days": 30, "include_embed": false, "max_rows": 200000,
                "tables": [ /* see chapter 7 */ ], "orphan_tables": [ /* ... */ ] }
}
```

Notes:

- `auth.mode`: `hmac` or `off` (use `off` only on a fully trusted internal network).
- `metas_search.extra_where`, `vector.similarity_expression` and `purge.tables` are **server-side**
  settings containing SQL fragments - never expose them to callers. Table/column names are validated
  against an identifier whitelist.
- `database.sslmode` uses asyncpg's vocabulary (`disable|allow|prefer|require|verify-ca|verify-full`)
  and is passed as the `ssl` connect argument. Do **not** put `sslmode` into the DSN query string:
  asyncpg does not accept it there.
- `scripts/show_config.py` prints the effective configuration (placeholders resolved, secrets masked);
  `scripts/show_config.py --template` prints the documented template.

Error envelope:

```json
{"success": false, "error_id": "UPSTREAM_ERROR", "error_message": "...", "details": {...}}
```

Common `error_id` values: `INVALID_SIGNATURE` / `INVALID_API_KEY` / `UNAUTHORIZED` / `UPSTREAM_ERROR` /
`DATABASE_ERROR` / `DB_NOT_CONFIGURED` / `UNSAFE_IDENTIFIER` / `INVALID_RETENTION_DAYS` / `BAD_REQUEST`.

---

## 6. Layout

```
app/
├── main.py                # wiring (middleware + v1 passthrough + v2 routers + lifespan)
├── proxy.py               # reverse-proxy awareness: scheme / client IP / forwarded headers
├── config.py              # config.json loading + ${ENV} expansion
├── security.py            # second factor (HMAC over METHOD + FULL_PATH) + single-use cache
├── upstream.py            # WeKnora client (passthrough, semantic calls, API key cache)
├── deps.py / errors.py    # dependency wiring / error envelope
├── routers/               # proxy_v1 / publish / metas / maintenance / system
└── services/
    ├── db.py              # PostgreSQL engine and Executor (FakeExecutor for tests)
    ├── meta_dsl.py        # FMQ lexer + parser + evaluator + jsonb SQL pushdown
    ├── metas_search.py    # metadata search (SQL assembly, paging, vector scoring)
    ├── publish_service.py # publish orchestration (tag -> draft -> metas -> publish -> optional wait)
    └── purge_service.py   # physical purge (cascade delete + orphan sweep)
scripts/                   # hmac_request.py (signed request helper) / show_config.py
tests/                     # 98 tests, upstream mocked with respx
```

---

## 7. Known constraints

- **The signature cache is per process**: with several replicas the replay window is per replica.
  Run a single worker, or move the cache to Redis.
- **The v1 passthrough buffers the request body in memory**: for very large uploads (Cloudflare caps
  them at 100 MB anyway) talk to WeKnora directly on the internal network. Responses are streamed.
- **Metadata search talks to PostgreSQL directly**: if a WeKnora upgrade changes the schema, adjust
  table/column names in `config.json → metas_search` instead of touching code. With
  `include_deleted=false` soft-deleted rows are excluded.
- **Purge only covers database rows**: `include_embed=true` also sweeps unattached vector/chunk rows,
  but residue inside the vector store itself may still need the upstream tooling.
- **v1 does not modify request or response bodies**, so the native limitations remain - which is
  exactly why the v2 extensions exist.
- The internal leg is cleartext HTTP, see 2.4: draw the trust boundary in front of Forge.
