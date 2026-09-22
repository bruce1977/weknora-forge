# TESTCASE.md — weknora-forge v2 增强功能测试用例

本文档记录了针对 weknora-forge 四项增强功能（见 `app/` 实现）的测试用例。

> **执行环境说明**
> 当前环境无法连通 WeKnora 数据库，因此**不进行任何实质性（live / 依赖真实 DB）测试**。
> 本仓库的接口测试全部基于 `respx` 模拟 WeKnora 上游、基于 `FakeExecutor` 模拟 PostgreSQL，
> 可在无真实后端 / 无数据库的情况下运行。本文档描述**预期的测试用例与断言**，
> 在具备可连通环境的机器上通过 `python -m pytest tests -q` 即可回归。

---

## 0. 通用约定

- **鉴权（v2 接口）**：所有 v2 业务接口均经过 `verify_v2` 依赖，要求
  HMAC 二次签名 `X-Forge-Signature = hex(HMAC_SHA256(api_key, METHOD + FULL_PATH))`
  并通过上游校验 API Key。`tests/conftest.py::auth_headers` 生成合规请求头。
- **测试夹具**：`tests/conftest.py` 写入临时 `config.json` 并设 `FORGE_CONFIG`，
  `client` fixture 提供已启动 lifespan 的 `TestClient`；`build_app` fixture 额外注入
  `FakeDatabase` 以绕过真实 PostgreSQL。
- **上游模拟**：`respx.mock` 拦截 `http://upstream.test/api/v1/*`；`UPSTREAM` /
  `VALIDATE_URL` 见 `conftest.py`。

---

## 1. 离线 Swagger UI（`GET /docs`、`/openapi.json`）

**相关实现**：`app/main.py`（挂载 `app/static/swagger` 本地资产，设置
`swagger_js_url` / `swagger_css_url` / `swagger_favicon_url` 指向本地路径，
`redoc_url=None`）；资产来自 `swagger-ui-dist@5.17.14` 的本地化副本。

**目的**：在内网 / 无外网 CDN 的部署环境下，仍能离线渲染 Swagger UI，并暴露所有 v2 路由。

### TC-1.1 `/docs` 与 `/openapi.json` 可达且覆盖全部 v2 路由
- **测试函数**：`tests/test_api_endpoints.py::test_docs_and_openapi_are_served`
- **前置**：`build_app(SearchExecutor())`，`VALIDATE_URL` 返回 200。
- **步骤**：
  1. `GET /docs`
  2. `GET /openapi.json`
- **预期**：
  - `/docs` 返回 200，且响应体包含 `swagger-ui` 或 `SwaggerUIBundle`（离线 UI 已注入）。
  - `/openapi.json` 返回 200，且 `paths` 同时包含
    `/api/v2/probe`、`/api/v2/publish`、`/api/v2/knowledge/search`。
- **断言要点**：
  ```python
  assert docs.status_code == 200
  assert "swagger-ui" in docs.text.lower() or "SwaggerUIBundle" in docs.text
  assert schema.status_code == 200
  assert "/api/v2/probe" in paths
  assert "/api/v2/publish" in paths
  assert "/api/v2/knowledge/search" in paths
  ```

### TC-1.2 OpenAPI 中 search 请求体字段为 `kb_ids`（数组）而非 `kb_id`
- **同属**：`test_docs_and_openapi_are_served`（与 TC-1.1 合并执行）。
- **预期**：`POST /api/v2/knowledge/search` 的 requestBody schema 含 `kb_ids`、
  **不含** `kb_id`（兼容 `$ref` 与内联两种 schema 写法）。
- **断言要点**：
  ```python
  assert "kb_ids" in props
  assert "kb_id" not in props
  ```

---

## 2. 连通性探针 `GET /api/v2/probe`

**相关实现**：`app/routers/system.py::probe` → `app/upstream.py::WeKnoraClient.probe`。
`probe()` **永不抛异常**，对配置的上游发起 `GET /knowledge-bases?page=1&page_size=200`，
连通且 2xx 则 `ok=True`，否则按 `upstream_status`（504 超时 / 502 连接失败 / 非 2xx 原样透传）
报告失败，避免把后端故障变成 5xx。

**鉴权**：需要 `verify_v2`（HMAC + API Key 校验）。

### TC-2.1 上游可达 → 探针成功
- **测试函数**：`tests/test_api_endpoints.py::test_probe_succeeds_when_weknora_reachable`
- **前置**：
  - `VALIDATE_URL` → 200（`{"success": true, "data": []}`）。
  - `GET {UPSTREAM}/knowledge-bases` → 200，
    `{"success": true, "data": {"data": [{"id": "kb-1"}], "total": 1}}`。
- **步骤**：`GET /api/v2/probe`（带 `auth_headers("GET", "/api/v2/probe")`）。
- **预期**：
  - HTTP 200，`body.success == True`。
  - `body.data.weknora.knowledge_base_count == 1`（取自 `total`）。
  - `body.data.auth_method` 为当前主体的鉴权方式。
- **断言要点**：
  ```python
  assert resp.status_code == 200
  assert body["success"] is True
  assert body["data"]["weknora"]["knowledge_base_count"] == 1
  ```

### TC-2.2 上游不可达（连接失败）→ 探针失败并以 502 上报
- **测试函数**：`tests/test_api_endpoints.py::test_probe_reports_failure_when_weknora_down`
- **前置**：
  - `VALIDATE_URL` → 200。
  - `GET {UPSTREAM}/knowledge-bases` → `side_effect=httpx.ConnectError("connection refused")`
    （模拟 WeKnora 宕机 / 网络不通）。
- **步骤**：`GET /api/v2/probe`（带 HMAC 头）。
- **预期**：
  - HTTP 200（`probe()` 不抛异常，端点不返回 5xx）。
  - `body.success == False`。
  - `body.data.weknora.upstream_status == 502`（连接类错误映射为 502）。
- **断言要点**：
  ```python
  assert body["success"] is False
  assert body["data"]["weknora"]["upstream_status"] == 502
  ```

### TC-2.3 缺少 HMAC 签名 → 401（鉴权兜底）
- **说明**：复用 `tests/test_api_endpoints.py::test_endpoints_require_second_factor`
  的参数化思想：任何 v2 接口缺少 `X-Forge-Signature` 时应返回 401。探针属于 v2 接口，
  同样适用。
- **预期**：不带 `X-Forge-Signature` 直接 `GET /api/v2/probe` → 401。

---

## 3. 发布接口标签「先创建、冲突则按名查询」(create-first)

**相关实现**：`app/upstream.py::WeKnoraClient.ensure_tag`（create-first 语义）+
`app/services/publish_service.py::PublishService.publish`。
流程：先 `create_tag`；若上游以 **409（标签已存在）** 拒绝创建，则捕获 `upstream_error`，
改用 `find_tag_by_name` 按名查询取回既有 `id`，避免重复创建不同 id 的标签。

### TC-3.1 标签已存在（创建被 409 拒绝）→ 查询复用既有 id
- **测试函数**：`tests/test_publish.py::test_publish_reuses_existing_tag_when_create_conflicts`
- **前置**（respx 模拟）：
  - `VALIDATE_URL` → 200。
  - `POST {UPSTREAM}/knowledge-bases/kb-1/tags` → **409**
    `{"error": {"message": "tag already exists"}}`（创建被拒）。
  - `GET {UPSTREAM}/knowledge-bases/kb-1/tags` → 200，
    `{"success": true, "data": {"data": [{"id": "tag-exists", "name": "技术文档"}], "total": 1}}`
    （回退查询命中既有标签）。
  - `POST .../knowledge/manual`、`GET/PUT .../knowledge/kn-1`、
    `PUT .../knowledge/manual/kn-1` → 200（后续发布流程）。
- **请求体**：
  ```json
  {"kb_id": "kb-1", "title": "t", "content": "c", "tag_names": ["技术文档"]}
  ```
- **步骤**：`POST /api/v2/publish`（带 HMAC 头）。
- **预期**：
  - HTTP 200，`success == True`。
  - `tag_ids == ["tag-exists"]`（复用既有标签，**不是**新建标签）。
  - `tag_names == ["技术文档"]`。
- **断言要点**：
  ```python
  assert resp.status_code == 200
  assert data["success"] is True
  assert data["tag_ids"] == ["tag-exists"]
  assert data["tag_names"] == ["技术文档"]
  ```

### TC-3.2 标签不存在 → 正常创建（回归）
- **相关**：既有 `test_publish_returns_only_success_and_knowledge_id` 等用例。
- **预期**：`POST .../tags` → 200 返回新建 `tag-1`，发布返回 200 且 `tag_ids == ["tag-1"]`，
  证明「标签不存在时仍走创建路径」未被破坏。

### TC-3.3 上游创建失败（非冲突，如 500）→ 502 错误信封
- **相关**：`test_publish_failure_uses_error_envelope`（`fail_on_create=True`）。
- **预期**：`POST .../tags` 返回 500 时，非 409 分支不回退查询，直接上升为
  `UPSTREAM_ERROR` 信封（HTTP 502）。证明只有「冲突」才走查询回退，
  其它上游错误仍按原语义上报。

---

## 4. 跨多知识库检索 `kb_ids`（数组）

**相关实现**：
- `app/schemas.py::MetaSearchRequest.kb_ids: Optional[List[str]]`（替换原 `kb_id: str`）。
- `app/routers/metas.py::_resolve` 透传 `payload.kb_ids`。
- `app/services/metas_search.py::SearchRequest.kb_ids` +
  `_build_filters` 生成 `CAST(knowledge_base_id AS TEXT) IN :forge_kb_ids`
  （SQLAlchemy `bindparam(expanding=True)` 展开数组）。

### TC-4.1 单库（`kb_ids: ["kb-1"]`）→ 生成 IN 过滤且返回命中
- **测试函数**：`tests/test_api_endpoints.py::test_search_endpoint_post`
- **请求体**：
  ```json
  {"metas_query": "level >= 3", "kb_ids": ["kb-1"], "page": 1, "page_size": 20}
  ```
- **预期**：HTTP 200，`data.total == 1`，且注入到 SQL 的参数
  `executor.params[-1]["forge_kb_ids"] == ["kb-1"]`。
- **断言要点**：
  ```python
  assert executor.params[-1]["forge_kb_ids"] == ["kb-1"]
  ```

### TC-4.2 多库数组直接传入服务层 → 参数为字符串数组
- **测试函数**：`tests/test_metas_search.py::test_search_kb_filter_and_include_deleted`
- **步骤**：直接构造 `SearchRequest(query="level = 3", kb_ids=["kb-x"])` 调用
  `MetasSearchService.search(...)`。
- **预期**：最终 SQL 绑定参数 `last_params["forge_kb_ids"] == ["kb-x"]`，
  且生成 `IN` 子句（支持同时在多个知识库检索）。
- **断言要点**：
  ```python
  assert last_params["forge_kb_ids"] == ["kb-x"]
  ```

### TC-4.3 不传 `kb_ids` → 跨全库检索（无该过滤）
- **说明**：既有 `test_search_endpoint_post_with_vector` / `..._with_title_and_tags` 等用例
  均不传 `kb_ids`，断言正常返回，证明「缺省为全库」行为未被破坏。
- **预期**：`data` 正常返回，`forge_kb_ids` 不应出现在绑定参数中。

---

## 5. 回归范围与命令

```bash
cd D:/workspace/weknora-forge
python -m pytest tests -q
```

四项增强均带有 mock（respx + FakeExecutor），**不触碰真实 WeKnora / PostgreSQL**。
预期：92 项用例全绿。

---

## 6. 开放健康检查 `GET /api/v2/health`

**相关实现**：`app/routers/system.py::health`。
该端点**不鉴权、不调用任何资源**（不验 HMAC、不连 PostgreSQL、不连 WeKnora），直接返回 `{}`。
用于冷启动存活探针 / 负载均衡探活。

### TC-6.1 无鉴权直接返回 `{}`
- **步骤**：`GET /api/v2/health`（**不带**任何请求头）。
- **预期**：HTTP 200，`body == {}`。
- **断言要点**：区别于旧的 health（旧实现会校验 HMAC 并 `db.ping()`，未鉴权返回 401/502）。

### TC-6.2 在 OpenAPI 中标注为开放
- **预期**：`/openapi.json` 的 `paths["/api/v2/health"]["get"]["security"] == []`，
  而其它 v2 端点继承全局 `security`（要求 `X-API-Key` + `X-Forge-Signature`）。

---

## 7. Swagger 安全凭证（内联可编辑请求头）

**相关实现**：`app/main.py` 覆盖 `openapi()`，注入 `components.securitySchemes`
（`X-API-Key`、`X-Forge-Signature`，均为 `apiKey` in `header`）并设全局 `security`
要求两者；同时把这两个头作为**可编辑的 operation 级 `parameters`** 注入到每一个受保护的 v2
接口（开放路径 `/`、`/api/v2/health` 除外）。这样在 Swagger 的「Try it out」里
就能直接看到并填写 `X-API-Key` / `X-Forge-Signature`，无需走全局 Authorize 弹窗。
注意：FastAPI 内置 `/docs` 路由不会透传自定义头，因此必须自己渲染 `get_swagger_ui_html`
并覆盖 `openapi()` 才能让 Swagger 显示这些头字段。

### TC-7.1 每个受保护 v2 接口内联暴露两个 HMAC 头
- **预期**：`/openapi.json` 任意 v2 接口（如 `/api/v2/probe` 的 `get`、`/api/v2/publish` 的 `post`）
  的 `parameters` 同时包含 `name == "X-API-Key"` 与 `name == "X-Forge-Signature"`，且 `in == "header"`。
- **断言要点**：
  ```python
  params = [p["name"] for p in op["parameters"]]
  assert "X-API-Key" in params
  assert "X-Forge-Signature" in params
  ```
- 开放路径（`/api/v2/health`）的 `parameters` **不应**包含这两个头（已豁免）。

### TC-7.2 顶层 securitySchemes 含两个头、无 `X-Forge-Key`
- **预期**：`securitySchemes` 含 `X-API-Key`、`X-Forge-Signature`（**不再有** `X-Forge-Key`）；
  顶层 `security == [{"X-API-Key": [], "X-Forge-Signature": []}]`。

### TC-7.3 无 `X-Forge-Key`
- 按约定，签名头固定只 `X-Forge-Signature`；`X-Forge-Key` 已从 `AuthConfig`、`security._client_label`、
  `config.json`、`tests/conftest.auth_headers`、`scripts/hmac_request.py` 与 README 中英版移除。

---

## 8. HMAC 签名脚本 `scripts/gen_forge_signature.py`

**用途**：根据参数生成 v2 请求所需的 `X-Forge-Signature`，算法与 `tests/conftest.sign()` 一致：

```
payload   = METHOD + FULL_PATH          # 例如 "POST/api/v2/publish?kb_id=kb-1"
signature = hex(HMAC_SHA256(api_key, payload))
```

**用法**：
```bash
python scripts/gen_forge_signature.py --method POST --path /api/v2/publish \
    --api-key <WEKNORA_API_KEY> [--query "kb_id=kb-1"] [--curl] [--json '{...}']
```
输出 `X-API-Key` / `X-Forge-Signature` 及可选的可直接执行的 `curl` 命令。

**重要**：POST/PUT/PATCH/DELETE 的签名是**一次性**的（在 `auth.signature_cache_ttl_seconds`
内重放会被拒），Swagger 测写接口每次都要重新生成；GET 可复用。

---

## 9. 通过环境变量关闭 Swagger（`SWAGGER_ENABLED`）

**相关实现**：`app/config.py::SwaggerConfig.enabled`（默认值 `"${SWAGGER_ENABLED:-true}"`，
`field_validator` 将 `false`/`0`/`no`/`off` 等转为 `False`）→ `app/main.py::create_app` 据此决定
`openapi_url` 是否为 `None`、以及是否挂载自定义 `/docs` 路由与本地静态资源。

### TC-9.1 默认开启
- **预期**：不设 `SWAGGER_ENABLED` 时，`GET /docs` 与 `GET /openapi.json` 均返回 200，
  且 `/openapi.json` 暴露全部 v2 路由。

### TC-9.2 `SWAGGER_ENABLED=false` 彻底关闭
- **步骤**：以 `SWAGGER_ENABLED=false` 启动进程。
- **预期**：
  - `GET /docs` → 404（路由未挂载）。
  - `GET /openapi.json` → 404（`openapi_url=None`，内置路由未注册）。
  - 业务接口（如 `/api/v2/health`）不受影响，仍按原逻辑工作。
- **断言要点**：
  ```python
  assert client.get("/docs").status_code == 404
  assert client.get("/openapi.json").status_code == 404
  ```

### TC-9.3 其它关闭取值
- `SWAGGER_ENABLED=0` / `=no` / `=off` 行为同 `false`；`=true` / `=1` / `=yes` / 任意非空值同 `true`。

---

## 10. Publish 接口字段约束

**相关实现**：`app/schemas.py::PublishRequest`。

### TC-10.1 title 超过 200 字符 → 422
- **请求体**：`title` 为 201 个字符。
- **预期**：HTTP 422，`error_id == "INVALID_REQUEST"`。

### TC-10.2 content 超过 10000 字符 → 422
- **请求体**：`content` 为 10001 个字符。
- **预期**：HTTP 422，`error_id == "INVALID_REQUEST"`。

### TC-10.3 title 或 content 为空字符串 → 422
- **请求体**：`title: ""` 或 `content: ""`。
- **预期**：HTTP 422（`min_length=1`）。

### TC-10.4 支持多标签 tag_names 数组
- **请求体**：`tag_names: ["技术文档", "人工智能", "数据库"]`。
- **预期**：HTTP 200，响应中 `tag_names` 和 `tag_ids` 均为长度 3 的数组。

### TC-10.5 tag_names 中部分标签已存在、部分不存在 → 自动创建不存在的
- **请求体**：`tag_names: ["技术文档", "new-tag-xyz"]`（"技术文档"已存在，"new-tag-xyz"不存在）。
- **预期**：HTTP 200，两个标签均被关联（"new-tag-xyz"被自动创建）。

### TC-10.6 tag_names 为空数组或不传 → 无标签
- **请求体**：`tag_names: []` 或不传 `tag_names` 字段。
- **预期**：HTTP 200，响应中 `tag_names` 和 `tag_ids` 为 null。

---

> **文档更新**：README.md / README_CN.md 已同步 `tag_names`（替代 `tag`）、
> `tag_ids`（替代 `tag_id`）、开放 health、probe 双重连通性检查、
> 移除 proxy 配置、移除 keystore 等变更。
