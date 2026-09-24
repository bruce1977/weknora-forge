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
| No second credential beyond the API key | HMAC-SHA256 signature header keyed by a dedicated `api_secret` paired with the API key | every v1 / v2 route |
| Manual knowledge needs multi-step choreography (draft → metadata → publish) | one server-side call, rolled back on failure | `POST /api/v2/publish` |
| Only title/tag/time filtering, no `custom_metadata` search | FMQ query language pushed down to PostgreSQL JSONB | `POST /api/v2/knowledge/search` |
| Soft delete only (writes `deleted_at`), no physical purge | ordered cascade delete + orphan vector sweep | `DELETE /api/v2/management/purge` |

---

## 1. Quick start

### Docker Compose

```bash
cp example.env .env            # WEKNORA_BASE_URL, DB_* and the WEKNORA_API_KEY/SECRET pair
docker compose up -d --build
curl http://localhost:8000/                        # service info
curl http://localhost:8000/api/v2/health           # open endpoint, no auth required
```

### Local development

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt          # Linux/macOS

python -m uvicorn app.main:app --reload --port 8000 --no-proxy-headers --env-file .env
python scripts/show_config.py                       # effective config, secrets masked
```

> `--no-proxy-headers` is required: forwarding headers are handled once, in `app/proxy.py`.
> Uvicorn's own handling only trusts 127.0.0.1, so it would silently ignore `X-Forwarded-*`
> coming from a container or tunnel (see chapter 3).

Tests: `python -m pytest tests -q` (**111 passed, 2 skipped** — live tests need `WEKNORA_BASE_URL`
and `FORGE_API_KEY`/`FORGE_KB_ID`; the rest are mocked with respx, no real WeKnora or database required).

---

## 2. API Reference

### Endpoints Overview

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| **System** | | | |
| `GET` | `/` | ✗ | Service info |
| **v1 Passthrough** | | | |
| `ANY` | `/api/v1/{path}` | HMAC | Forward to WeKnora |
| `ANY` | `/v1/{path}` | HMAC | Alias of `/api/v1/{path}` |
| **v2 System** | | | |
| `GET` | `/api/v2/health` | ✗ | Open health check (returns `{}`) |
| `GET` | `/api/v2/probe` | HMAC | WeKnora + PostgreSQL connectivity probe |
| **v2 Publish** | | | |
| `POST` | `/api/v2/publish` | HMAC | Publish knowledge (multi-tag support) |
| **v2 Metadata Search** | | | |
| `POST` | `/api/v2/knowledge/search` | HMAC | Search by metadata, title, tags |
| **v2 Maintenance** | | | |
| `DELETE` | `/api/v2/management/purge` | HMAC | Purge soft-deleted data |

> **Auth**: ✗ = No authentication, HMAC = Requires `X-API-Key` + `X-Forge-Signature` headers

---

### 2.1 System Endpoints

#### `GET /` {#get-root}

Returns service information including upstream address, v1/v2 prefixes, and auth mode.

**Response**

```JSON
{
  "service": "weknora-forge",
  "version": "0.2.1",
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

**Open endpoint, no auth required**. Returns `{}` directly. Used for liveness probes / load balancer health checks.

**Response**

```JSON
{}
```

---

#### `GET /api/v2/probe` {#get-api-v2-probe}

Requires HMAC auth. Tests both WeKnora and PostgreSQL connectivity.

**Response (success)**

```JSON
{
  "success": true,
  "data": {
    "weknora": {
      "message": "WeKnora is reachable",
      "upstream": "http://localhost:8080",
      "upstream_status": 200,
      "latency_ms": 120.5,
      "knowledge_base_count": 4
    },
    "database": {
      "ok": true,
      "message": "PostgreSQL is reachable",
      "latency_ms": 15.2
    },
    "auth_method": "hmac"
  }
}
```

**Response (partial failure)**

```JSON
{
  "success": false,
  "data": {
    "weknora": {
      "message": "Upstream unreachable: Connection refused",
      "upstream": "http://localhost:8080",
      "upstream_status": 502,
      "latency_ms": 3.1
    },
    "database": {
      "ok": true,
      "message": "PostgreSQL is reachable",
      "latency_ms": 12.1
    },
    "auth_method": "hmac"
  }
}
```

**Generate HMAC signature:**

```bash
# the secret comes from --api-secret, else $WEKNORA_API_SECRET (.env), else keys.json
python scripts/gen_forge_signature.py --method GET --path /api/v2/probe \
    --api-key sk-xxxxx --curl
```

---

### 2.4 v2 Publish

#### `POST /api/v2/publish` {#post-api-v2-publish}

Executes the full publish orchestration: resolve tag names → create/get tags → create draft → set custom metadata → publish.

**Request**

| Field | Type | Required | Limit | Description |
|-------|------|----------|-------|-------------|
| `kb_id` | string | ✓ | | Knowledge base ID |
| `title` | string | ✓ | 1-200 chars | Article title |
| `content` | string | ✓ | 1-10000 chars | Markdown body |
| `description` | string | | | Optional description |
| `tag_names` | string[] | | | Tag name array, e.g. `["docs", "ai"]` |
| `custom_metas` | object | | | Custom metadata |
| `channel` | string | | default `"api"` | Source channel |

```JSON
{
  "kb_id": "kb-00000001",
  "title": "Milvus cluster deployment guide",
  "content": "# Milvus cluster deployment\n\n## Planning\n...\n\n## Steps\n...",
  "description": "Optional description",
  "tag_names": ["docs", "ai", "database"],
  "custom_metas": {
    "level": 3,
    "category": "ops"
  },
  "channel": "api"
}
```

**Response (success)**

```JSON
{
  "success": true,
  "knowledge_id": "k-00000001",
  "tag_ids": ["t-00000001", "t-00000002", "t-00000003"],
  "tag_names": ["docs", "ai", "database"]
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

> With `poll_interval_seconds > 0` the call blocks until post-processing finishes and the response
> then also carries `parse_status` / `enable_status`. Behind Cloudflare keep it `0`: the 100-second
> origin limit would return 524 while the work continues.

---

### 2.5 v2 Metadata Search

#### `POST /api/v2/knowledge/search` {#post-api-v2-knowledge-search}

Search knowledge by custom metadata, title, and tags. The `metas_query` field accepts FMQ expressions for custom metadata filtering, `title` enables full-text search on article titles, and `tags` filters by tag names. See [FMQ.md](./FMQ.md) for the full query language reference.

**Request**

```JSON
{
  "kb_ids": ["kb-00000001"],
  "metas_query": "level >= 3 AND category = 'ops'",
  "title": "Milvus",
  "tags": ["ai", "db"],
  "page": 1,
  "page_size": 20,
  "case_insensitive": true,
  "return_content": false
}
```

**Response**

```JSON
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "k-00000001",
        "title": "Milvus cluster deployment guide",
        "kb_name": "Test Database",
        "metas": {"level": 3, "category": "ops", "hashcode": "abc123"},
        "tag_names": ["ai", "db"]
      }
    ],
    "total": 100,
    "page": 1,
    "page_size": 20,
    "has_more": true
  }
}
```

**Notes**
- `kb_ids` is required and must be accessible by the caller's API key (validated via WeKnora `GET /knowledge-bases`). Returns 403 `KB_ACCESS_DENIED` if any ID is not accessible.
- Response items expose `id`, `title`, `kb_name`, `metas` (the full `custom_metadata` blob for each hit), and `tag_names` (all tags as an array).
- Set `return_content: true` to also return each hit's article body as `items[].content` (from `knowledges.metadata->>'content'`, falling back to concatenated `chunks.content`). Default `false` keeps the payload small.

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
    "dry_run": true,
    "retention_days": 30,
    "cutoff": "2026-08-24T00:00:00+00:00",
    "include_embed": false,
    "counts": {
      "knowledge_bases": 0,
      "knowledges": 150
    },
    "matched": {
      "knowledges": 150,
      "embeddings": 500
    },
    "deleted": {
      "knowledges": 0,
      "embeddings": 0
    },
    "orphan_matched": {
      "embeddings (orphan)": 500
    },
    "orphan_deleted": {
      "embeddings (orphan)": 0
    },
    "skipped_tables": []
  }
}
```

> Always check with `dry_run=true` first. `purge` is a long-running call - invoke from internal network.

---

## 3. Deployment

Forge listens on plain HTTP (default port 8000). In production, place a reverse proxy (Cloudflare, Nginx, etc.) in front to terminate TLS.

```
client ──HTTPS──► reverse proxy ──HTTP──► Forge:8000 ──HTTP──► WeKnora
```

### 3.1 Proxy configuration

Reverse proxy awareness is built-in and hardcoded. Forge automatically:
- Reads real client IP from `X-Forwarded-For`, `X-Real-IP` etc.
- Trusted proxy ranges: `127.0.0.1`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `::1`
- Rebuilds and forwards `X-Forwarded-For/Proto/Host` to WeKnora

### 3.2 Verify deployment

```bash
curl http://localhost:8000/                        # service info
curl http://localhost:8000/api/v2/health           # open, no auth required
```

---

## 4. Authentication: two layers

| Layer | Credential | Verification |
| --- | --- | --- |
| 1 | WeKnora `X-API-Key` | **v2 only**: validated against `GET {upstream}/api/v1/knowledge-bases`, cached per key (300 s positive / 30 s negative). `401` means "key rejected", `5xx` means "WeKnora unreachable" (HTTP 502) - the two stay distinguishable. **v1 skips this** and stays a transparent pipe, letting WeKnora return its own status code |
| 2 (second factor) | `X-Forge-Signature` | HMAC-SHA256 keyed by the `api_secret` paired with the caller's API key (see 4.1) |

### 4.1 Signature scheme

```
payload   = HTTP_METHOD + HTTP_FULL_PATH        # e.g. "POST/api/v2/publish?dry_run=1"
signature = hex(HMAC_SHA256(api_secret, payload))  # -> X-Forge-Signature
```

- `api_secret` never travels with the request: Forge looks it up in an in-process
  keystore (`app/keystore.py`) by the `X-API-Key` value. The dictionary cache is merged from
  1. the environment - `WEKNORA_API_KEY` + `WEKNORA_API_SECRET` (local testing), and
  2. `keys.json` next to `config.json` - `data/keys.json` locally, `/data/keys.json` in
     the container (deployments, one entry per pair):

     ```json
     [
       { "api_key": "sk-aaaa", "api_secret": "..." },
       { "api_key": "sk-bbbb", "api_secret": "..." }
     ]
     ```

  A `keys.json` entry whose `api_key` equals `WEKNORA_API_KEY` overrides the environment
  secret. The file is re-read automatically whenever it changes, so secrets can be rotated
  **without a restart**.
- The API key itself is never the signing key any more: possessing the key alone no longer
  lets anyone forge signatures. An `api_key` without a registered `api_secret` is rejected
  with `401 INVALID_SIGNATURE`.
- No timestamp, no nonce and no body digest: the signed payload is exactly
  `METHOD + FULL_PATH`, and state-changing methods (POST/PUT/PATCH/DELETE) use a fresh
  signature per request.
- `HTTP_FULL_PATH` is the raw path plus the raw query string. Forge signs the ASGI `raw_path`
  and performs **no normalisation and no decoding**: percent-encoding is part of the signature.
- Headers: `X-API-Key` (identity) and `X-Forge-Signature` (the HMAC).

```bash
# the secret comes from --api-secret, else $WEKNORA_API_SECRET (.env), else keys.json
python scripts/hmac_request.py --base http://localhost:8000 --api-key sk-xxxxx \
    GET /api/v2/probe

# print the headers only (paste into curl / Postman / a job runner)
python scripts/hmac_request.py --api-key sk-xxxxx --dry-run DELETE '/api/v2/management/purge?retention_days=30'

# disable the second factor (fully trusted internal network only)
# config.json -> auth.mode = "off"
```

### 4.2 Signature generation script

The `scripts/gen_forge_signature.py` script generates `X-Forge-Signature` headers for testing or integrating with external tools (Postman, job runners, etc.).

**Basic usage:**

```bash
# Generate signature for a POST request
python scripts/gen_forge_signature.py --method POST --path /api/v2/publish \
    --api-key sk-xxxxx

# Include a query string
python scripts/gen_forge_signature.py --method GET \
    --path /api/v2/knowledge/search --query "metas_query=level%20%3E%3D%203" \
    --api-key sk-xxxxx --curl

# Generate a ready-to-run curl command with JSON body
python scripts/gen_forge_signature.py --method POST --path /api/v2/publish \
    --api-key sk-xxxxx --curl --json '{"kb_id":"kb-1"}'
```

**Parameters:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `--method` | Yes | HTTP method (GET, POST, PUT, PATCH, DELETE) |
| `--path` | Yes | Request path starting with `/` (include query string or use `--query`) |
| `--api-key` | Yes* | WeKnora API key sent as `X-API-Key` (defaults to `$WEKNORA_API_KEY`, else the repo `.env`) |
| `--api-secret` | No | HMAC signing secret; falls back to `$WEKNORA_API_SECRET` / `.env`, then to the `keys.json` entry for the key |
| `--keys-file` | No | Explicit `keys.json` path (default: next to `config.json`, then `data/keys.json`) |
| `--query` | No | Raw query string without the leading `?` |
| `--json` | No | JSON body string, or `@file` to read from a file (only printed with `--curl`) |
| `--curl` | No | Output a complete curl command with signed headers |

> \* Required when `$WEKNORA_API_KEY` is not set.
> **Note:** For state-changing methods (POST/PUT/PATCH/DELETE), generate a fresh signature for each request. GET signatures may be reused.

---

## 5. Configuration

**Config file auto-detection: `data/config.json`** (preferred), fallback to project root `config.json`.
Override with `FORGE_CONFIG` environment variable. Every `${VAR}` / `${VAR:-default}` placeholder
in it is expanded from the process environment at load time, so secrets never have to be written
into the file:

```jsonc
{
  "service":  { "host": "0.0.0.0", "port": 8000, "log_level": "INFO", "workers": 1 },
  "swagger":  { "enabled": "${SWAGGER_ENABLED:-true}" },
  "upstream": { "base_url": "${WEKNORA_BASE_URL:-http://localhost:8080}", "api_prefix": "/api/v1",
                "timeout_seconds": 60, "api_key_validate_path": "/knowledge-bases?page=1&page_size=1" },
  "auth":     { "mode": "hmac", "require_on_v1": true, "require_on_v2": true,
                "hmac_header_signature": "X-Forge-Signature" },
  "publish":  { "wait_until": "enabled", "timeout_seconds": 300,
                "poll_interval_seconds": 3.0, "default_channel": "api",
                "merge_metas": true, "rollback_on_failure": true },
  "database": { "dsn": "${FORGE_DB_DSN:-}", "host": "${DB_HOST:-localhost}", "port": "${DB_PORT:-5432}",
                "user": "${DB_USER:-postgres}", "password": "${DB_PASSWORD:-}", "name": "${DB_NAME:-WeKnora}",
                "sslmode": "${DB_SSLMODE:-disable}", "pool_size": 5, "max_overflow": 5, "statement_timeout_ms": 30000 },
  "metas_search": { "table": "knowledges", "metadata_column": "custom_metadata",
                    "result_column": ["id", "title", "file_name", "kb_name", "tag_name"],
                    "max_rows": 5000, "default_page_size": 20, "extra_where": "" },
  "purge":    { "dry_run": true, "default_retention_days": 30, "include_embed": false, "max_rows": 200000,
                "tables": [ /* full list: data/config.json */ ], "orphan_tables": [ /* ... */ ] }
}
```

Notes:

- `auth.mode`: `hmac` or `off` (use `off` only on a fully trusted internal network).
- **Signature secrets** are deliberately not part of `config.json` (chapter 4): local testing
  uses the `WEKNORA_API_KEY` / `WEKNORA_API_SECRET` environment variables, deployments put one
  or more pairs into `keys.json` **next to** `config.json` (`data/keys.json`, i.e.
  `/data/keys.json` in the container):

  ```json
  [
    { "api_key": "sk-aaaa...", "api_secret": "..." },
    { "api_key": "sk-bbbb...", "api_secret": "..." }
  ]
  ```

  A `keys.json` entry overrides `WEKNORA_API_SECRET` when the `api_key` matches, and edits to
  the file are picked up automatically (secrets rotate without a restart).
- `swagger.enabled`: controls the Swagger UI and OpenAPI spec. Set `SWAGGER_ENABLED=false`
  (or `0` / `no` / `off`) to fully disable `GET /docs` and `GET /openapi.json` - useful in
  production. Defaults to `true`. The UI is vendored locally (`app/static/swagger`, from
  `swagger-ui-dist@5.17.14`) so it renders without any external CDN.
- `metas_search.extra_where` and `purge.tables` are **server-side**
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

Common `error_id` values: `INVALID_SIGNATURE` / `INVALID_API_KEY` / `UNAUTHORIZED` / `FORBIDDEN` /
`KB_ACCESS_DENIED` / `UPSTREAM_ERROR` / `DATABASE_ERROR` / `DB_NOT_CONFIGURED` / `UNSAFE_IDENTIFIER` /
`TABLE_NOT_FOUND` / `METADATA_COLUMN_MISSING` / `TAG_NOT_FOUND` / `PURGE_LIMIT_EXCEEDED` /
`INVALID_RETENTION_DAYS` / `INVALID_REQUEST` / `INTERNAL_ERROR` / `BAD_REQUEST`.

---

## 6. Layout

```
app/
├── main.py                # wiring (middleware + v1 passthrough + v2 routers + lifespan)
├── proxy.py               # reverse-proxy awareness: scheme / client IP / forwarded headers (hardcoded)
├── config.py              # config.json loading + ${ENV} expansion (auto-detects data/config.json)
├── security.py            # second factor (HMAC over METHOD + FULL_PATH)
├── keystore.py            # api_key -> api_secret cache (env + keys.json, auto-refresh)
├── upstream.py            # WeKnora client (passthrough, semantic calls, API key cache)
├── schemas.py             # request/response models (publish, metas search)
├── logging.py             # log setup
├── deps.py / errors.py    # dependency wiring / error envelope
├── routers/               # proxy_v1 / publish / metas / maintenance / system
└── services/
    ├── db.py              # PostgreSQL engine and Executor (FakeExecutor for tests)
    ├── meta_dsl.py        # FMQ lexer + parser + evaluator + jsonb SQL pushdown
    ├── metas_search.py    # metadata search (SQL assembly, paging)
    ├── publish_service.py # publish orchestration (tags -> draft -> metas -> publish -> optional wait)
    └── purge_service.py   # physical purge (cascade delete + orphan sweep)
scripts/                   # gen_forge_signature.py / hmac_request.py / show_config.py
tests/                     # 111 tests (2 live skipped), respx mocked upstream
FMQ.md                     # FMQ query language full reference
pyproject.toml             # pytest config (asyncio_mode, testpaths)
data/                      # runtime data (config.json, keys.json, ...)
```

---

## 7. Known constraints

- **The signature cache is per process**: with several replicas the replay window is per replica.
  Run a single worker, or move the cache to Redis.
- **The v1 passthrough buffers the request body in memory**: for very large uploads (Cloudflare caps
  them at 100 MB anyway) talk to WeKnora directly on the internal network. Responses are streamed.
- **Metadata search talks to PostgreSQL directly**: if a WeKnora upgrade changes the schema, adjust
  table/column names in `config.json → metas_search` instead of touching code. Soft-deleted rows
  are always excluded.
- **Purge only covers database rows**: `include_embed=true` also sweeps unattached vector/chunk rows,
  but residue inside the vector store itself may still need the upstream tooling.
- **v1 does not modify request or response bodies**, so the native limitations remain - which is
  exactly why the v2 extensions exist.
- The internal leg is cleartext HTTP, see 2.4: draw the trust boundary in front of Forge.
