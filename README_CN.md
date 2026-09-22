# WeKnora Forge

> English version: [README.md](./README.md)

WeKnora 原生 API 的**扩展层**：在一层薄代理之上补齐原生能力缺口，用 Python 3 + FastAPI 实现，可容器化部署。

```
公网客户端 ──HTTPS──► Cloudflare ──HTTP──► Forge ──HTTP──► WeKnora (/api/v1)
内网任务/脚本 ────────HTTP────────────────►   │
                                              ├─ v1/*  原样透传 + 二次验证
                                              └─ v2/*  发布编排 / 元数据检索 / 软删清理
```

原生能力缺口 → Forge 的补齐方式：

| 缺口 | 实现 | 端点 |
| --- | --- | --- |
| 除 API Key 外没有第二重凭证 | HMAC-SHA256 签名头，密钥即 API Key | 全部 v1 / v2 路由 |
| 手动知识需「建草稿 → 写元数据 → 发布」多步编排 | 服务端一次调用完成，失败回滚 | `POST /api/v2/publish` |
| 只能按标题/标签/时间过滤，无法检索 `custom_metadata` | FMQ 查询语法 → PostgreSQL JSONB 下推 | `POST·GET /api/v2/knowledge/search` |
| 只做软删（写 `deleted_at`），没有物理清理 | 按表顺序级联物理删除 + 孤儿向量清理 | `DELETE /api/v2/management/purge` |

---

## 1. 快速开始

### Docker Compose

```bash
cp .env.example .env          # 至少填 WEKNORA_BASE_URL 与 DB_*
docker compose up -d --build
curl http://localhost:8000/                        # 服务信息
curl http://localhost:8000/api/v2/health           # 需签名，见第 4 节
```

### 本地开发

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt          # Linux/macOS

uvicorn app.main:app --reload --port 8000 --no-proxy-headers
python scripts/show_config.py                       # 看生效配置（密钥已打码）
```

> 必须带 `--no-proxy-headers`：转发头由 `app/proxy.py` 统一处理，uvicorn 自己处理只看 127.0.0.1，
> 会把容器/内网隧道来的 `X-Forwarded-*` 全部忽略（详见第 3 节）。

测试：`python -m pytest tests -q`（**98 项**，上游用 respx 模拟，不需要真实 WeKnora 与数据库）。

---

## 2. API 参考

### 端点总览

| 方法 | 路径 | 认证 | 描述 |
|------|------|------|------|
| **系统** | | | |
| `GET` | `/` | ✗ | 服务信息 |
| **v1 透传** | | | |
| `ANY` | `/api/v1/{path}` | HMAC | 转发至 WeKnora |
| **v2 系统** | | | |
| `GET` | `/api/v2/health` | HMAC | 依赖健康检查 |
| `GET` | `/api/v2/probe` | HMAC | WeKnora 连通性探针 |
| **v2 发布** | | | |
| `POST` | `/api/v2/publish` | HMAC | 发布知识 |
| **v2 元数据搜索** | | | |
| `POST` | `/api/v2/knowledge/search` | HMAC | 按元数据、标题、标签搜索 |
| `POST` | `/api/v2/metas/parse` | HMAC | 解析 FMQ 表达式 |
| `GET` | `/api/v2/metas/grammar` | HMAC | FMQ 语法参考 |
| **v2 维护** | | | |
| `DELETE` | `/api/v2/management/purge` | HMAC | 清理软删除数据 |

> **认证**: ✗ = 无认证, HMAC = 需要 `X-Forge-Signature` 头

---

### 2.1 系统端点

#### `GET /` {#get-root}

返回服务信息，包括上游地址、v1/v2 前缀和认证模式。

**响应**

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

### 2.2 v1 透传

#### `ANY /api/v1/{path}` {#any-api-v1-path}

将任何 HTTP 方法原样转发到上游 WeKnora API。支持所有方法（GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS）。

**转发内容**: method, query, 请求头（`X-API-Key` 强制注入为调用方凭证）, body（含 multipart）

**响应**: `StreamingResponse` 直传字节流（SSE 可直接用）

**丢弃的头**: `Host`, `Content-Length`, `X-API-Key`, `X-Forge-*`, `CF-*`, `X-Forwarded-*`

```bash
curl -H "X-API-Key: sk-xxx" -H "X-Forge-Signature: ..." \
  http://localhost:8000/api/v1/knowledge-bases?page=1&page_size=20
```

---

### 2.3 v2 系统端点

#### `GET /api/v2/health` {#get-api-v2-health}

检查上游 WeKnora 连通性和 PostgreSQL 数据库连通性。

**响应**

```JSON
{
  "status": "ok",
  "upstream": "ok",
  "database": "ok"
}
```

---

#### `GET /api/v2/probe` {#get-api-v2-probe}

轻量级连通性探针，测试配置的 WeKnora 后端是否可达。执行实时测试请求（列出知识库）并返回结果，不会抛出异常。

**响应（成功）**

```JSON
{
  "success": true,
  "data": {
    "ok": true,
    "auth_method": "hmac",
    "bases": [...]
  }
}
```

**响应（失败）**

```JSON
{
  "success": false,
  "data": {
    "ok": false,
    "auth_method": "hmac",
    "error": "Connection refused"
  }
}
```

**生成 HMAC 签名：**

```bash
python scripts/gen_forge_signature.py --method GET --path /api/v2/probe \
    --api-key sk-xxxxx --curl
```

---

### 2.4 v2 发布

#### `POST /api/v2/publish` {#post-api-v2-publish}

执行完整的发布编排：创建/获取标签 → 创建草稿 → 设置自定义元数据 → 发布。

**请求**

```JSON
{
  "kb_id": "kb-00000001",
  "title": "Milvus 集群部署指南",
  "content": "# Milvus 集群部署\n\n## 规划\n...",
  "description": "可选描述",
  "tag": {
    "name": "技术文档",
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

**响应（成功）**

```JSON
{
  "success": true,
  "knowledge_id": "k-00000001",
  "tag_id": "t-00000001",
  "status": "published"
}
```

**响应（失败）**

```JSON
{
  "success": false,
  "error_id": "UPSTREAM_ERROR",
  "error_message": "..."
}
```

> `publish.wait=true` 时调用会被同步阻塞到后处理结束——经 Cloudflare 访问请保持 `false`

---

### 2.5 v2 元数据搜索

#### `POST /api/v2/knowledge/search` {#post-api-v2-knowledge-search}

按自定义元数据、标题和标签搜索知识。`metas_query` 字段接受 FMQ 表达式用于自定义元数据过滤，`title` 字段支持文章标题全文搜索，`tags` 字段按标签名称过滤。

**请求**

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

**响应**

```JSON
{
  "success": true,
  "data": {
    "rows": [
      {
        "id": "k-00000001",
        "title": "Milvus 集群部署指南",
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

解析 FMQ 表达式并返回 AST（调试工具）。

**请求**

```JSON
{
  "query": "level >= 3 AND tags CONTAINS 'ai'"
}
```

**响应**

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

返回 FMQ 语法参考和内置字段列表。

```bash
curl -H "X-API-Key: sk-xxx" -H "X-Forge-Signature: ..." \
  http://localhost:8000/api/v2/metas/grammar
```

---

### 2.6 v2 维护

#### `DELETE /api/v2/management/purge` {#delete-api-v2-management-purge}

根据保留天数清理软删除数据。按表顺序执行级联删除。

**查询参数**

| 参数 | 默认值 | 描述 |
|------|--------|------|
| `retention_days` | 30 | 删除 `deleted_at` 早于 N 天的记录 |
| `include_embed` | false | 同时清理孤立的向量/分块行 |
| `dry_run` | true | 仅统计，不删除 |

**响应**

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

> ⚠️ 真删之前先 `dry_run=true` 核对。`purge` 是长任务，请从内网调用。

---

## 3. 部署形态

Forge 监听 HTTP（默认端口 8000）。生产环境需在前面放反向代理（Cloudflare、Nginx 等）终结 TLS。

```
客户端 ──HTTPS──► 反向代理 ──HTTP──► Forge:8000 ──HTTP──► WeKnora
```

### 3.1 代理配置

若 Forge 部署在反向代理后，需在 `config.json` 中启用代理感知：

```JSON
"proxy": {
  "enabled": true,
  "trusted_proxies": ["127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
  "client_ip_headers": ["x-forwarded-for", "x-real-ip"],
  "forward_client_info": true
}
```

- `trusted_proxies`：反向代理的 IP/CIDR。只有这些对端可以设置 `X-Forwarded-*` 头。
- `client_ip_headers`：按顺序读取真实客户端 IP 的头（第一个有效值胜出）。
- `forward_client_info`：是否向 WeKnora 重建并转发 `X-Forwarded-For/Proto/Host`。

### 3.2 验证部署

```bash
curl http://localhost:8000/                        # 服务信息
curl http://localhost:8000/api/v2/whoami  # 需 HMAC 签名
```

---

## 4. 鉴权：两层校验

| 层 | 凭证 | 校验方式 |
| --- | --- | --- |
| 第一层 | WeKnora `X-API-Key` | **v2** 强制回源校验：`GET {upstream}/api/v1/knowledge-bases`，结果按 key 缓存（正缓存 300s / 负缓存 30s）；`401` 判为 key 无效、`5xx` 判为上游不可用（返回 502，两者可区分）。**v1 不做**，保持透明管道，让 WeKnora 自己返回它的状态码 |
| 第二层（二次验证） | `X-Forge-Signature` | HMAC-SHA256，密钥就是调用方自己的 API Key |

### 4.1 签名算法

```
payload   = HTTP_METHOD + HTTP_FULL_PATH        # 例如 "POST/api/v2/publish?dry_run=1"
signature = hex(HMAC_SHA256(api_key, payload))  # → X-Forge-Signature
```

- 无时间戳、无 nonce、无 body 摘要：**API Key 就是签名密钥**，不需要再分发额外 secret。
- `HTTP_FULL_PATH` = 原始 path + 原始 query（Forge 取 ASGI `raw_path`，**不做任何规范化/解码**，
  百分号编码原样参与签名）。
- 新鲜度靠「一次性签名」实现，见 2.4。
- 请求头：`X-API-Key`（也是签名密钥）、`X-Forge-Signature`（HMAC）。不再有独立的 `X-Forge-Key`
  头，日志中的调用方标识由脱敏后的 API Key 派生。

```bash
python scripts/hmac_request.py --base http://localhost:8000 --api-key sk-xxxxx \
    GET /api/v2/whoami

# 只看签名头（复制进 curl / Postman / 定时任务）
python scripts/hmac_request.py --api-key sk-xxxxx --dry-run DELETE '/api/v2/management/purge?retention_days=30'

# 关掉二次验证（仅限完全可信的内网）
# config.json → auth.mode = "off"
```

---

## 5. 配置

**唯一配置源是 `config.json`**（用 `FORGE_CONFIG` 指向别处）。文件里的 `${VAR}` 与 `${VAR:-默认值}`
在加载时从环境变量展开，所以**密码/密钥不必写进文件**：

```jsonc
{
  "service":  { "host": "0.0.0.0", "port": 8000, "log_level": "INFO", "workers": 1 },
  "swagger":  { "enabled": "${SWAGGER_ENABLED:-true}" },
  "proxy":    { "enabled": true, "trusted_proxies": ["127.0.0.1", "172.16.0.0/12", "..."],
                "client_ip_headers": ["cf-connecting-ip", "x-real-ip"], "forward_client_info": true },
  "upstream": { "base_url": "${WEKNORA_BASE_URL:-http://localhost:8080}", "api_prefix": "/api/v1",
                "timeout_seconds": 60, "verify_ssl": true, "default_api_key": "${WEKNORA_DEFAULT_API_KEY:-}",
                "api_key_validate_path": "/knowledge-bases?page=1&page_size=1" },
  "auth":     { "mode": "hmac", "require_on_v1": true, "require_on_v2": true,
                "hmac_header_signature": "X-Forge-Signature",
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
                "tables": [ /* 见第 7 节 */ ], "orphan_tables": [ /* ... */ ] }
}
```

要点：

- `auth.mode`：`hmac` | `off`（`off` 只建议用于完全可信的内网）。
- `swagger.enabled`：控制 Swagger UI 与 OpenAPI 文档的开关。设 `SWAGGER_ENABLED=false`
  （或 `0` / `no` / `off`）可彻底关闭 `GET /docs` 与 `GET /openapi.json`，便于生产环境隐藏接口文档。
  默认 `true`。UI 资源本地化内置（`app/static/swagger`，来自 `swagger-ui-dist@5.17.14`），无需任何外网 CDN 即可渲染。
  每个受 HMAC 保护的 v2 接口都在「Try it out」里直接暴露可编辑的 `X-API-Key` 与 `X-Forge-Signature`
  请求头，可不经全局 Authorize 弹窗直接在浏览器里调试。
- `metas_search.extra_where` / `vector.similarity_expression` / `purge.tables` 是**服务端**配置，
  含 SQL 片段，绝不能暴露给调用方；表名列名只做标识符白名单校验。
- `database.sslmode` 用 asyncpg 的取值（`disable|allow|prefer|require|verify-ca|verify-full`），
  Forge 会把它作为 `ssl` 连接参数传给 asyncpg（**不能**写进 DSN 的 query，asyncpg 不认 `sslmode`）。
- `scripts/show_config.py` 打印生效配置（含 `${VAR}` 展开结果，密钥打码）；
  `scripts/show_config.py --template` 打印带默认值的完整模板。

错误信封：

```json
{"success": false, "error_id": "UPSTREAM_ERROR", "error_message": "...", "details": {...}}
```

常见 `error_id`：`INVALID_SIGNATURE` / `INVALID_API_KEY` / `UNAUTHORIZED` / `UPSTREAM_ERROR` /
`DATABASE_ERROR` / `DB_NOT_CONFIGURED` / `UNSAFE_IDENTIFIER` / `INVALID_RETENTION_DAYS` / `BAD_REQUEST`。

---

## 6. 目录结构

```
app/
├── main.py                # 应用装配（中间件 + v1 透传 + v2 路由 + 生命周期）
├── proxy.py               # 反向代理感知：scheme / 真实客户端 IP / 转发头重建
├── config.py              # config.json 加载 + ${ENV} 展开
├── security.py            # 二次验证（HMAC over METHOD + FULL_PATH）+ 一次性签名缓存
├── upstream.py            # WeKnora 客户端（透传、语义化调用、API Key 校验缓存）
├── deps.py / errors.py    # 依赖注入 / 错误信封
├── routers/               # proxy_v1 / publish / metas / maintenance / system
└── services/
    ├── db.py              # PostgreSQL 引擎与 Executor（含测试替身 FakeExecutor）
    ├── meta_dsl.py        # FMQ 词法 + 语法 + 求值 + jsonb SQL 下推
    ├── metas_search.py    # 元数据检索（SQL 组装、分页、向量评分）
    ├── publish_service.py # 发布编排（标签 → 草稿 → 元数据 → 发布 → 可选等待）
    └── purge_service.py   # 物理清理（级联删除 + 孤儿清理）
scripts/                   # hmac_request.py（签名请求助手）/ show_config.py
tests/                     # 98 项，respx 模拟上游
```

---

## 7. 已知约束

- **签名缓存是进程内的**：多副本部署时重放窗口按副本各算一份，建议只跑 1 个 worker，
  或换成 Redis 等共享存储。
- **v1 透传会在内存中缓冲请求体**：单个大文件上传（经 Cloudflare 还有 100 MB 上限）建议内网直连
  WeKnora；v1 的响应是流式的，不受影响。
- **元数据检索直连 PostgreSQL**：WeKnora 升级若改表结构，用 `config.json → metas_search`
  调整表名/列名即可，不必改代码；`include_deleted=false` 时默认不返回软删记录。
- **purge 只覆盖数据库行**：`include_embed=true` 会顺手清理无关联的向量/分块，但向量库自身
  （Milvus 等）的残留索引仍可能需要按上游工具处理。
- **v1 透传不改请求/响应体**，原生接口的能力限制依旧存在（这正是 v2 扩展存在的理由）。
- 内网链路是明文 HTTP，见 2.4；请把可信边界画在 Forge 之前。
