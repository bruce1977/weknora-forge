# TODO

> 待办与研究方向。条目完成后直接删除。

## 研究方向

### 1. publish 同步等待（`sync=true`）的通用语义

**状态**：预留功能，默认关闭——`publish.allow_sync=false` 时传 `sync: true` 返回
400 `SYNC_DISABLED`（快速失败，不产生任何上游写操作）。

**背景**：知识库对不同内容类型（纯文本文档、文件上传、URL 抓取等）的后处理路径与
耗时差异较大，统一的"等待完成"语义难以定义。实测（2026-09-24，kb
`6a15167f-…`）：`wait_until=enabled` 在 3.3s 返回时 `parse_status` 仍为
`finalizing`，数秒后才变为 `completed`——「启用」与「解析完成」是两个独立维度。

**已具备的基础**（日后打开开关即可复用）：

- `POST /api/v2/publish` 请求字段 `sync` + `app/services/publish_service.py::_wait` 轮询等待
- 响应 `wait` 摘要：完成/超时 `{timed_out, attempts}`，轮询出错 `{failed}`
- 等待失败或超时**不掩盖**发布结果：`parse_status` / `enable_status` 回退为发布调用本身的状态
- `wait_knowledge` 按 deadline 有界睡眠；部署 `publish.timeout_seconds=55` 小于 nginx 60s，
  避免代理 504/524 掐断干净的超时
- 测试覆盖：TC-10.7 ~ TC-10.13（`tests/test_publish.py`）

**待研究问题**：

- [ ] 各内容类型/渠道的 `parse_status`、`enable_status` 状态机与终态集合（含 failed/cancelled）
- [ ] `wait_until` 三档（`enabled` / `completed` / `terminal`）对各类场景何者为正确默认
- [ ] 部分失败的呈现方式（如向量化失败但文章已启用）——是否应有独立于 HTTP 状态的业务码
- [ ] 长连接与并发等待的资源上限（等待超时之外：并发数、排队、背压）
- [ ] 与反代超时的配合策略（nginx 60s / Cloudflare 100s → 524）是否需要按部署拓扑可配

**参考**：README 中 sync「预留」说明、TESTCASE TC-10.7 ~ TC-10.13、
`example.env` 的 `PUBLISH_ALLOW_SYNC`、`data/config.json` 的 `publish.*`。

## 可选优化

- [ ] 发布流程中执行版本号 bump（`FastAPI(version=…)` + git tag + push），
      此前两次 Docker 发布均按默认跳过（docker-publish skill Step 5，需明确要求才执行）
