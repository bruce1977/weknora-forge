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
| 除 API Key 外没有第二重凭证 | HMAC-SHA256 签名头，密钥为与 API Key 配对的独立 `api_secret` | 全部 v1 / v2 路由 |
| 手动知识需「建草稿 → 写元数据 → 发布」多步编排 | 服务端一次调用完成，失败回滚 | `POST /api/v2/publish` |
| 只能按标题/标签/时间过滤，无法检索 `custom_metadata` | FMQ 查询语法 → PostgreSQL JSONB 下推 | `POST /api/v2/knowledge/search` |
| 只做软删（写 `deleted_at`），没有物理清理 | 按表顺序级联物理删除 + 孤儿向量清理 | `DELETE /api/v2/management/purge` |

---

## 1. 快速开始

### Docker Compose

```bash
cp example.env .env            # 至少填 WEKNORA_BASE_URL、DB_* 与 WEKNORA_API_KEY/SECRET 密钥对
docker compose up -d --build
curl http://localhost:8000/                        # 服务信息
curl http://localhost:8000/api/v2/health           # 开放端点，无需签名
```

### 本地开发

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt          # Linux/macOS

python -m uvicorn app.main:app --reload --port 8000 --no-proxy-headers --env-file .env
python scripts/show_config.py                       # 看生效配置（密钥已打码）
```

> 必须带 `--no-proxy-headers`：转发头由 `app/proxy.py` 统一处理，uvicorn 自己处理只看 127.0.0.1，
> 会把容器/内网隧道来的 `X-Forwarded-*` 全部忽略（详见第 3 节）。

测试：`python -m pytest tests -q`（**116 通过 / 2 跳过**，live 需 `WEKNORA_BASE_URL` 与 `FORGE_API_KEY`/`FORGE_KB_ID`；其余用 respx 模拟，不需要真实 WeKnora 与数据库）。

---

## 2. API 参考

### 端点总览

| 方法 | 路径 | 认证 | 描述 |
|------|------|------|------|
| **系统** | | | |
| `GET` | `/` | ✗ | 服务信息 |
| **v1 透传** | | | |
| `ANY` | `/api/v1/{path}` | HMAC | 转发至 WeKnora |
| `ANY` | `/v1/{path}` | HMAC | `/api/v1/{path}` 的别名 |
| **v2 系统** | | | |
| `GET` | `/api/v2/health` | ✗ | 开放健康检查（返回 `{}`） |
| `GET` | `/api/v2/probe` | HMAC | WeKnora + PostgreSQL 连通性探针 |
| **v2 发布** | | | |
| `POST` | `/api/v2/publish` | HMAC | 发布知识（支持多标签） |
| **v2 元数据搜索** | | | |
| `POST` | `/api/v2/knowledge/search` | HMAC | 按元数据、标题、标签搜索 |
| **v2 维护** | | | |
| `DELETE` | `/api/v2/management/purge` | HMAC | 清理软删除数据 |

> **认证**: ✗ = 无认证, HMAC = 需要 `X-API-Key` + `X-Forge-Signature` 头

---

### 2.1 系统端点

#### `GET /` {#get-root}

返回服务信息，包括上游地址、v1/v2 前缀和认证模式。

**响应**

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

**开放端点，无需鉴权**。直接返回 `{}`，用于冷启动存活探针 / 负载均衡探活。

**响应**

```JSON
{}
```

---

#### `GET /api/v2/probe` {#get-api-v2-probe}

需要 HMAC 鉴权。测试 WeKnora 和 PostgreSQL 双重连通性。

**响应（成功）**

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

**响应（部分失败）**

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

**生成 HMAC 签名：**

```bash
# 签名密钥依次取自 --api-secret、$WEKNORA_API_SECRET（或 .env）、keys.json 中的对应条目
python scripts/gen_forge_signature.py --method GET --path /api/v2/probe \
    --api-key sk-xxxxx --curl
```

---

### 2.4 v2 发布

#### `POST /api/v2/publish` {#post-api-v2-publish}

执行完整的发布编排：解析标签名 → 创建/获取标签 → 创建草稿 → 设置自定义元数据 → 发布。

**请求**

| 字段 | 类型 | 必填 | 限制 | 描述 |
|------|------|------|------|------|
| `kb_id` | string | ✓ | | 知识库 ID |
| `title` | string | ✓ | 1-200 字符 | 文章标题 |
| `content` | string | ✓ | 1-20000 字符 | Markdown 正文 |
| `description` | string | | | 可选描述 |
| `tag_names` | string[] | | | 标签名称数组，如 `["技术文档", "AI"]` |
| `custom_metas` | object | | | 自定义元数据 |
| `channel` | string | | 默认 `"api"` | 来源渠道 |
| `sync` | boolean | | 默认 `false` | 同步模式（**预留**：需 `publish.allow_sync=true`，否则 400 `SYNC_DISABLED`）：阻塞等待文章后处理完成后再返回，响应携带 `parse_status` / `enable_status`（等待目标/超时/间隔取 `publish.*` 配置） |

```JSON
{
  "kb_id": "kb-00000001",
  "title": "Milvus 集群部署指南",
  "content": "# Milvus 集群部署\n\n## 规划\n...\n\n## 步骤\n...",
  "description": "可选描述",
  "tag_names": ["技术文档", "人工智能", "数据库"],
  "custom_metas": {
    "level": 3,
    "category": "ops"
  },
  "channel": "api"
}
```

**响应（成功）**

```JSON
{
  "success": true,
  "knowledge_id": "k-00000001",
  "tag_ids": ["t-00000001", "t-00000002", "t-00000003"],
  "tag_names": ["技术文档", "人工智能", "数据库"]
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

> `sync` 为**预留**功能：除非 `publish.allow_sync=true`（默认 `false`），否则请求在产生
> 任何上游写操作前即返回 400 `SYNC_DISABLED`。开启后 `sync: true` 阻塞至后处理达到
> `publish.wait_until`（受 `timeout_seconds` 限制，按 `poll_interval_seconds` 轮询），
> 成功返回携带 `parse_status` / `enable_status`。经 Cloudflare 访问请保持默认 `false`：
> 100 秒回源限制会在处理继续时返回 524，且高并发下长连接堆积可能拖垮服务。

---

### 2.5 v2 元数据搜索

#### `POST /api/v2/knowledge/search` {#post-api-v2-knowledge-search}

按自定义元数据、标题和标签搜索知识。`metas_query` 字段接受 FMQ 表达式用于自定义元数据过滤，`title` 字段支持文章标题全文搜索，`tags` 字段按标签名称过滤。完整查询语法详见 [FMQ.md](./FMQ.md)。

**请求**

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

**响应**

```JSON
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "k-00000001",
        "title": "Milvus 集群部署指南",
        "kb_name": "测试数据库",
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

**说明**
- `kb_ids` 为必填项，且必须是当前 API KEY 可访问的知识库（通过 WeKnora `GET /knowledge-bases` 校验）。若无权限返回 403 `KB_ACCESS_DENIED`。
- 响应条目包含 `id`、`title`、`kb_name`、`metas`（每条命中的完整 custom_metadata）、`tag_names`（全部标签，数组）。
- 设 `return_content: true` 可额外返回正文 `items[].content`（优先取 `knowledges.metadata->>'content'`，为空时拼接 `chunks.content`）。默认 `false`，保持响应精简。

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

> ⚠️ 真删之前先 `dry_run=true` 核对。`purge` 是长任务，请从内网调用。

---

## 3. 部署形态

Forge 监听 HTTP（默认端口 8000）。生产环境需在前面放反向代理（Cloudflare、Nginx 等）终结 TLS。

```
客户端 ──HTTPS──► 反向代理 ──HTTP──► Forge:8000 ──HTTP──► WeKnora
```

### 3.1 代理配置

反向代理感知已内置硬编码，无需配置。Forge 会自动：
- 从 `X-Forwarded-For`、`X-Real-IP` 等头读取真实客户端 IP
- 信任的代理范围：`127.0.0.1`、`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`、`::1`
- 向 WeKnora 重建并转发 `X-Forwarded-For/Proto/Host`

### 3.2 验证部署

```bash
curl http://localhost:8000/                        # 服务信息
curl http://localhost:8000/api/v2/health           # 开放，无需签名
```

---

## 4. 鉴权：两层校验

| 层 | 凭证 | 校验方式 |
| --- | --- | --- |
| 第一层 | WeKnora `X-API-Key` | **v2** 强制回源校验：`GET {upstream}/api/v1/knowledge-bases`，结果按 key 缓存（正缓存 300s / 负缓存 30s）；`401` 判为 key 无效、`5xx` 判为上游不可用（返回 502，两者可区分）。**v1 不做**，保持透明管道，让 WeKnora 自己返回它的状态码 |
| 第二层（二次验证） | `X-Forge-Signature` | HMAC-SHA256，密钥为与调用方 API Key 配对的 `api_secret`（见 4.1） |

### 4.1 签名算法

```
payload   = HTTP_METHOD + HTTP_FULL_PATH        # 例如 "POST/api/v2/publish?dry_run=1"
signature = hex(HMAC_SHA256(api_secret, payload))  # → X-Forge-Signature
```

- `api_secret` 不随请求传输：Forge 通过 `X-API-Key` 值在进程内 keystore
  （`app/keystore.py`）中查表得到。字典缓存合并自
  1. 环境变量 `WEKNORA_API_KEY` + `WEKNORA_API_SECRET`（本地环境测试），以及
  2. `config.json` 同目录的 `keys.json` —— 本地为 `data/keys.json`，容器内为
     `/data/keys.json`（实际生成环境部署，可配置多组密钥）：

     ```json
     [
       { "api_key": "sk-aaaa", "api_secret": "..." },
       { "api_key": "sk-bbbb", "api_secret": "..." }
     ]
     ```

  `keys.json` 中与 `WEKNORA_API_KEY` 相同的 `api_key` 会**覆盖**环境变量里的 secret；
  文件一旦修改，缓存**自动刷新**，轮换密钥**无需重启**。
- API Key 本身**不再是**签名密钥：仅拿到 key 无法伪造签名；未注册 `api_secret` 的
  `api_key` 一律返回 `401 INVALID_SIGNATURE`。
- 无时间戳、无 nonce、无 body 摘要：签名载荷就是 `METHOD + FULL_PATH`，
  写操作（POST/PUT/PATCH/DELETE）每次请求使用新签名。
- `HTTP_FULL_PATH` = 原始 path + 原始 query（Forge 取 ASGI `raw_path`，**不做任何规范化/解码**，
  百分号编码原样参与签名）。
- 请求头：`X-API-Key`（身份凭证）、`X-Forge-Signature`（以配对 secret 计算的 HMAC）。

```bash
# 签名密钥依次取自 --api-secret、$WEKNORA_API_SECRET（或 .env）、keys.json 中的对应条目
python scripts/hmac_request.py --base http://localhost:8000 --api-key sk-xxxxx \
    GET /api/v2/probe

# 只看签名头（复制进 curl / Postman / 定时任务）
python scripts/hmac_request.py --api-key sk-xxxxx --dry-run DELETE '/api/v2/management/purge?retention_days=30'

# 关掉二次验证（仅限完全可信的内网）
# config.json → auth.mode = "off"
```

---

## 5. 配置

**配置文件自动查找 `data/config.json`**（优先），回退到项目根目录 `config.json`。
可用 `FORGE_CONFIG` 环境变量指定其他路径。文件里的 `${VAR}` 与 `${VAR:-默认值}`
在加载时从环境变量展开，所以**密码/密钥不必写进文件**：

```jsonc
{
  "service":  { "host": "0.0.0.0", "port": 8000, "log_level": "INFO", "workers": 1 },
  "swagger":  { "enabled": "${SWAGGER_ENABLED:-true}" },
  "upstream": { "base_url": "${WEKNORA_BASE_URL:-http://localhost:8080}", "api_prefix": "/api/v1",
                "timeout_seconds": 60, "api_key_validate_path": "/knowledge-bases?page=1&page_size=1" },
  "auth":     { "mode": "hmac", "require_on_v1": true, "require_on_v2": true,
                "hmac_header_signature": "X-Forge-Signature" },
  "publish":  { "allow_sync": false, "wait_until": "enabled", "timeout_seconds": 300,
                "poll_interval_seconds": 3.0, "default_channel": "api",
                "merge_metas": true, "rollback_on_failure": true },
  "database": { "dsn": "${FORGE_DB_DSN:-}", "host": "${DB_HOST:-localhost}", "port": "${DB_PORT:-5432}",
                "user": "${DB_USER:-postgres}", "password": "${DB_PASSWORD:-}", "name": "${DB_NAME:-WeKnora}",
                "sslmode": "${DB_SSLMODE:-disable}", "pool_size": 5, "max_overflow": 5, "statement_timeout_ms": 30000 },
  "metas_search": { "table": "knowledges", "metadata_column": "custom_metadata",
                    "result_column": ["id", "title", "file_name", "kb_name", "tag_name"],
                    "max_rows": 5000, "default_page_size": 20, "extra_where": "" },
  "purge":    { "dry_run": true, "default_retention_days": 30, "include_embed": false, "max_rows": 200000,
                "tables": [ /* 完整列表见 data/config.json */ ], "orphan_tables": [ /* ... */ ] }
}
```

要点：

- `auth.mode`：`hmac` | `off`（`off` 只建议用于完全可信的内网）。
- **签名密钥**不放进 `config.json`（见第 4 章）：本地环境测试用环境变量
  `WEKNORA_API_KEY` / `WEKNORA_API_SECRET`；部署环境在 `config.json` **同目录**的
  `keys.json` 中配置一组或多组密钥（`data/keys.json`，容器内即 `/data/keys.json`）：

  ```json
  [
    { "api_key": "sk-aaaa...", "api_secret": "..." },
    { "api_key": "sk-bbbb...", "api_secret": "..." }
  ]
  ```

  `api_key` 与 `WEKNORA_API_KEY` 相同时，`keys.json` 的 `api_secret` 覆盖环境变量；
  文件修改后缓存自动刷新，轮换无需重启。
- `swagger.enabled`：控制 Swagger UI 与 OpenAPI 文档的开关。设 `SWAGGER_ENABLED=false`
  （或 `0` / `no` / `off`）可彻底关闭 `GET /docs` 与 `GET /openapi.json`，便于生产环境隐藏接口文档。
  默认 `true`。UI 资源本地化内置（`app/static/swagger`，来自 `swagger-ui-dist@5.17.14`），无需任何外网 CDN 即可渲染。
- `metas_search.extra_where` / `purge.tables` 是**服务端**配置，
  含 SQL 片段，绝不能暴露给调用方；表名列名只做标识符白名单校验。
- `database.sslmode` 用 asyncpg 的取值（`disable|allow|prefer|require|verify-ca|verify-full`），
  Forge 会把它作为 `ssl` 连接参数传给 asyncpg（**不能**写进 DSN 的 query，asyncpg 不认 `sslmode`）。
- `scripts/show_config.py` 打印生效配置（含 `${VAR}` 展开结果，密钥打码）；
  `scripts/show_config.py --template` 打印带默认值的完整模板。

错误信封：

```json
{"success": false, "error_id": "UPSTREAM_ERROR", "error_message": "...", "details": {...}}
```

常见 `error_id`：`INVALID_SIGNATURE` / `INVALID_API_KEY` / `UNAUTHORIZED` / `FORBIDDEN` /
`KB_ACCESS_DENIED` / `UPSTREAM_ERROR` / `DATABASE_ERROR` / `DB_NOT_CONFIGURED` / `UNSAFE_IDENTIFIER` /
`TABLE_NOT_FOUND` / `METADATA_COLUMN_MISSING` / `TAG_NOT_FOUND` / `PURGE_LIMIT_EXCEEDED` /
`INVALID_RETENTION_DAYS` / `INVALID_REQUEST` / `INTERNAL_ERROR` / `BAD_REQUEST`。

---

## 6. 目录结构

```
app/
├── main.py                # 应用装配（中间件 + v1 透传 + v2 路由 + 生命周期）
├── proxy.py               # 反向代理感知：scheme / 真实客户端 IP / 转发头重建（硬编码）
├── config.py              # config.json 加载 + ${ENV} 展开（自动查找 data/config.json）
├── security.py            # 二次验证（HMAC over METHOD + FULL_PATH）
├── keystore.py            # api_key → api_secret 缓存（环境变量 + keys.json，自动刷新）
├── upstream.py            # WeKnora 客户端（透传、语义化调用、API Key 校验缓存）
├── schemas.py             # 请求/响应模型（发布、元数据检索）
├── logging.py             # 日志初始化
├── deps.py / errors.py    # 依赖注入 / 错误信封
├── routers/               # proxy_v1 / publish / metas / maintenance / system
└── services/
    ├── db.py              # PostgreSQL 引擎与 Executor（含测试替身 FakeExecutor）
    ├── meta_dsl.py        # FMQ 词法 + 语法 + 求值 + jsonb SQL 下推
    ├── metas_search.py    # 元数据检索（SQL 组装、分页）
    ├── publish_service.py # 发布编排（tags → 草稿 → 元数据 → 发布 → 可选等待）
    └── purge_service.py   # 物理清理（级联删除 + 孤儿清理）
scripts/                   # gen_forge_signature.py / hmac_request.py / show_config.py
tests/                     # 116 项（live 跳过 2），respx 模拟上游
FMQ.md                     # FMQ 查询语法完整参考
pyproject.toml             # pytest 配置（asyncio_mode、testpaths）
data/                      # 运行时数据（config.json、keys.json 等）
```

---

## 7. 已知约束

- **签名缓存是进程内的**：多副本部署时重放窗口按副本各算一份，建议只跑 1 个 worker，
  或换成 Redis 等共享存储。
- **v1 透传会在内存中缓冲请求体**：单个大文件上传（经 Cloudflare 还有 100 MB 上限）建议内网直连
  WeKnora；v1 的响应是流式的，不受影响。
- **元数据检索直连 PostgreSQL**：WeKnora 升级若改表结构，用 `config.json → metas_search`
  调整表名/列名即可，不必改代码；软删记录始终排除。
- **purge 只覆盖数据库行**：`include_embed=true` 会顺手清理无关联的向量/分块，但向量库自身
  （Milvus 等）的残留索引仍可能需要按上游工具处理。
- **v1 透传不改请求/响应体**，原生接口的能力限制依旧存在（这正是 v2 扩展存在的理由）。
- 内网链路是明文 HTTP，见 2.4；请把可信边界画在 Forge 之前。
