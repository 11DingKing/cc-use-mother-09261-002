# 跨学科知识单元编排

纯 Python 离线服务端（仅标准库），提供版本化状态、幂等命令、带时区事件、审核快照冻结、SQLite 持久化和 JSON API 边界。

## 接口

- `POST /units` — 创建知识单元：`{id, title, disciplines[], actor, occurred_at, idempotency_key?}`
- `GET /units` / `GET /units/{id}` — 列表 / 详情（含事件流与冻结快照）
- `POST /units/{id}/events` — 追加事件：`{type, actor, occurred_at}`，类型：`submit_review / approve / reject / revise / cancel`
- `POST /units/{id}/freeze` — 冻结快照：`{actor, occurred_at}`，仅 `approved` 状态可冻结，重复冻结幂等

## 约定

- **带时区事件**：`occurred_at` 必须是含时区偏移的 ISO-8601（如 `2026-09-26T10:00:00+08:00` 或 `...Z`），统一归一化为 UTC；朴素时间戳拒绝。
- **快照冻结**：审核通过后冻结，快照含 `content_hash`（规范 JSON 的 SHA-256）、`approved_at`、`frozen_at`、`frozen_by`，并写入 SQLite；冻结后拒绝后续事件。
- **可解释拒绝**：
  - `422 invalid_input` — `details[]` 逐项列出 `{field, reason}`
  - `409 invalid_transition / invalid_state / frozen / duplicate` — 附当前状态与允许操作
  - `404 not_found` — 单元或路由不存在

测试命令：python3 -m unittest discover -s tests -v

编译命令：python3 -m compileall -q service_09261_002 tests
