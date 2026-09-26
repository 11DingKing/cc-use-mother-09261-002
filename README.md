# 跨学科知识单元编排

纯 Python 离线服务端，提供版本化状态、幂等命令、SQLite 持久化和 JSON API 边界。

## 能力

- 知识单元状态机：`draft → reviewing → approved/rejected → archived`（`rejected` 可退回 `draft`），版本随流转递增
- 接收带时区的事件：`occurred_at` 必须是含偏移的 ISO 8601（如 `+08:00`、`Z`），归一化为 UTC 存储并保留原始偏移；裸时间一律拒绝
- 审核通过（`approved`）后可冻结不可变快照：内容寻址 `digest`，同版本重复冻结为幂等重放
- 输入不完整/非法时返回可解释拒绝：`error` 错误码 + `message` 说明 + `details` 字段级明细
- SQLite 离线持久化，`Workflow.load(store)` 可在重启后恢复状态

## 接口（`api.dispatch(flow, method, path, body)` → `(status, body)`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cases` | 创建知识单元 `{id, actor, title, disciplines, idempotency_key?}`，`disciplines` 至少两个不同学科 |
| GET | `/cases`、`/cases/{id}` | 查询知识单元 |
| POST | `/cases/{id}/move` | 状态流转 `{state, actor}` |
| POST | `/cases/{id}/freeze` | 冻结审核快照 `{actor}`，仅 `approved` 状态可用 |
| GET | `/cases/{id}/snapshots` | 查询冻结快照 |
| POST | `/events` | 接收事件 `{case_id, actor, kind, occurred_at, payload?, id?}`，`id` 用于幂等重放 |
| GET | `/events`、`/cases/{id}/events` | 查询事件 |

拒绝响应示例（422 输入不完整 / 409 状态冲突 / 404 资源不存在）：

```json
{"error": "invalid_input",
 "message": "occurred_at 必须携带时区偏移（如 +08:00 或 Z）",
 "details": [{"field": "occurred_at", "reason": "timezone_required", "value": "2026-09-26 12:30:00"}]}
```

测试命令：python3 -m unittest discover -s tests -v

编译命令：python3 -m compileall -q service_09261_002 tests
