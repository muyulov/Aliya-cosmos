# 6. Aliya 控制接口

Aliya 通过 cosmos 的控制面完成四类操作：下达指令、操作桌面、干预任务、接收状态。控制面只对 Aliya 开放，端用户没有入口。

## 6.1 基础约定

| 项 | 约定 |
| --- | --- |
| Base URL | `http://127.0.0.1:8080/v1`，仅监听内部地址 |
| 鉴权 | `Authorization: Bearer <aliya-session-token>` |
| 内容类型 | JSON 使用 `application/json`；注册清单可用 `application/yaml` |
| 时间 | RFC 3339 UTC 毫秒精度 |
| 幂等 | 写操作支持 `request_id`；重复提交返回同一结果 |
| 请求追踪 | 响应头返回 `X-Request-Id` 与 `X-Trace-Id` |
| 版本 | 路径版本为 `v1`；破坏性变更升 `v2` |

权限范围：

| scope | 能力 |
| --- | --- |
| `cosmos:read` | 读取桌面、agent、任务、事件 |
| `cosmos:invoke` | 下达指令、调用 agent、取消与输入 |
| `cosmos:approve` | 审批高风险操作 |
| `cosmos:admin` | 注册、启停、下线、修改桌面布局 |
| `cosmos:events` | 订阅事件流 |

会话令牌默认 8 小时有效，刷新通过 `POST /v1/session/refresh`。令牌只保存在 Aliya 侧，不写入 agent 清单或任务信封。

## 6.2 接口总览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/commands` | 下达自然语言指令 |
| `GET` | `/v1/desktop` | 获取桌面快照 |
| `PATCH` | `/v1/desktop/layout` | 修改排序、分组、固定、可见性 |
| `GET` | `/v1/agents` | agent 列表（含隐藏项） |
| `GET` | `/v1/agents/{agent_id}` | agent 详情 |
| `POST` | `/v1/agents/{agent_id}/invoke` | 桌面点击调用 |
| `POST` | `/v1/agents/{agent_id}/enable` | 启用 |
| `POST` | `/v1/agents/{agent_id}/disable` | 禁用 |
| `POST` | `/v1/agents/{agent_id}/probe` | 重新探测 |
| `GET` | `/v1/tasks/{task_id}` | 任务详情 |
| `POST` | `/v1/tasks/{task_id}/cancel` | 取消任务 |
| `POST` | `/v1/tasks/{task_id}/input` | 回答输入请求 |
| `POST` | `/v1/tasks/{task_id}/approve` | 批准审批单 |
| `POST` | `/v1/tasks/{task_id}/reject` | 拒绝审批单 |
| `POST` | `/v1/tasks/{task_id}/retry` | 重试任务 |
| `POST` | `/v1/tasks/{task_id}/reassign` | 改派到其他 agent |
| `POST` | `/v1/tasks/{task_id}/priority` | 调整优先级 |
| `GET` | `/v1/events` | 订阅 SSE 事件流 |
| `POST` | `/v1/notifications/{id}/ack` | 确认通知 |
| `GET` | `/v1/notifications/preferences` | 读取通知偏好 |
| `PATCH` | `/v1/notifications/preferences` | 更新通知偏好 |

注册与下线接口见 [02-registration.md](02-registration.md)。本文不重复注册字段。

## 6.3 下达指令

```http
POST /v1/commands?wait_ms=0
Content-Type: application/json

{
  "request_id": "req-20261025-0001",
  "session_id": "s-01",
  "text": "把 core/agent/registry.py 里的日志字段改成中文",
  "attachments": [],
  "constraints": {"priority": "normal", "deadline_ms": 120000}
}
```

`wait_ms` 为可选的同步等待窗口，默认 0：

| 值 | 行为 |
| --- | --- |
| `0` | 立即返回受理结果 |
| `1` 到 `10000` | 最多等待指定毫秒；任务完成则返回结果，否则返回受理状态 |

响应有四种形态：

### 委派

```json
{
  "mode": "delegated",
  "request_id": "req-20261025-0001",
  "task_id": "01JAV3M8Z9K2Q4W6R8T0Y2B4C6",
  "plan_id": null,
  "route": {
    "source": "natural_language",
    "selected_agent_id": "edit",
    "confidence": 0.93,
    "candidates": [
      {"agent_id": "edit", "score": 0.93},
      {"agent_id": "test", "score": 0.41}
    ]
  },
  "accepted_at": "2026-10-25T02:30:00.000Z"
}
```

### 直接回答

```json
{
  "mode": "direct",
  "request_id": "req-20261025-0002",
  "reply": {
    "text": "当前 3 个 agent：Edit agent 空闲，Test agent 运行中，Docs agent 离线。",
    "refs": ["desktop"]
  }
}
```

### 澄清

```json
{
  "mode": "clarify",
  "request_id": "req-20261025-0003",
  "question": "要修改哪个文件？",
  "options": [
    {"id": "registry", "label": "core/agent/registry.py"},
    {"id": "prompt", "label": "core/agent/prompt.py"}
  ],
  "expires_at": "2026-10-25T02:35:00.000Z"
}
```

### 待审批

```json
{
  "mode": "approval",
  "request_id": "req-20261025-0004",
  "task_id": "01JAV7A1B2C3D4E5F6G7H8J9K0",
  "approval": {
    "approval_id": "apr-01JAV7A1",
    "agent_id": "edit",
    "action": "file.bulk_rewrite",
    "risk": "write",
    "summary": "将重写 12 个文件",
    "expires_at": "2026-10-25T02:40:00.000Z"
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `mode` | `delegated`、`direct`、`clarify`、`approval` |
| `task_id` | 委派或待审批时存在 |
| `plan_id` | 多步骤计划时存在 |
| `route` | 路由轨迹；直接回答与桌面调用可以缺省 |
| `reply` | 直接回答的文本与引用 |
| `question` / `options` | 澄清问题与候选 |
| `approval` | 待审批详情 |

同一 `request_id` 重复提交返回首次结果，响应头带 `Idempotent-Replay: true`。

`wait_ms` 内任务完成时，响应体在委派结构上追加 `result` 字段（TaskResult），`mode` 仍为 `delegated`。`wait_ms` 只影响本次响应等待，任务本身继续异步执行。

## 6.4 桌面读取

```http
GET /v1/desktop
Authorization: Bearer <token>
If-None-Match: "sha256:ab12..."
```

响应：

```json
{
  "schema_version": 1,
  "snapshot_at": "2026-10-25T02:30:00.000Z",
  "server_time": "2026-10-25T02:30:00.000Z",
  "stream_seq": 1842,
  "layout_version": 12,
  "etag": "sha256:ab12...",
  "system": {
    "cosmos": {"status": "healthy", "version": "0.1.0"},
    "running_tasks": 2,
    "queued_tasks": 1,
    "alerts": {"critical": 0, "warning": 1},
    "stream": "connected"
  },
  "groups": [
    {"id": "dev", "title": "开发工具", "order": 10, "collapsed": false, "color": "#4C8DFF", "auto_sort": false}
  ],
  "cards": [
    {
      "agent_id": "edit",
      "display_name": "Edit agent",
      "status": "running",
      "status_text": "编辑中",
      "metrics": {"progress": 0.4, "elapsed_ms": 18200}
    }
  ],
  "notifications": {"unread": 2, "critical": 0}
}
```

`cards` 的完整字段见 [03-desktop.md](03-desktop.md)。卡片列表在快照中可以精简，详情通过 `GET /v1/agents/{agent_id}` 获取。

| 响应 | 条件 |
| --- | --- |
| `200` | 返回最新快照 |
| `304` | `If-None-Match` 与当前 `etag` 一致，内容未变化 |
| `401` | 令牌无效或过期 |
| `403` | 缺少 `cosmos:read` |

桌面快照是 Aliya 侧状态的权威来源。事件流只提供增量，缺口或重连后以快照为准。

## 6.5 桌面调用

```http
POST /v1/agents/edit/invoke
Content-Type: application/json

{
  "request_id": "req-20261025-0010",
  "session_id": "s-01",
  "input": {
    "target_files": ["core/agent/registry.py"],
    "instruction": "把日志字段改成中文",
    "mode": "patch"
  },
  "constraints": {"priority": "normal", "deadline_ms": 120000}
}
```

响应：

```json
{
  "mode": "delegated",
  "request_id": "req-20261025-0010",
  "task_id": "01JAV3M8Z9K2Q4W6R8T0Y2B4C6",
  "agent_id": "edit",
  "route": {"source": "desktop", "selected_agent_id": "edit", "confidence": 1.0},
  "accepted_at": "2026-10-25T02:30:00.000Z"
}
```

桌面调用跳过意图匹配，仍然执行以下校验：

1. agent 存在且 `lifecycle=active`。
2. `enabled=true`，否则返回 `AGENT_DISABLED`。
3. 心跳未离线，否则返回 `AGENT_OFFLINE`。
4. `input` 通过能力 schema 校验，否则返回 `INVALID_INPUT` 与字段级错误。
5. 调用方具备所需 scope，且能力未命中未审批的风险规则。
6. 队列与并发未饱和，否则返回 `RESOURCE_EXHAUSTED`。

桌面调用与自然语言调用共享任务模型、事件流与桌面更新，区别只在 `route.source`。命中审批规则时，响应 `mode` 为 `approval`，`task_id` 对应一个停在 `waiting_approval` 的任务。

## 6.6 桌面布局

```http
PATCH /v1/desktop/layout
Content-Type: application/json

{
  "operations": [
    {"op": "move", "agent_id": "edit", "group": "dev", "order": 1},
    {"op": "pin", "agent_id": "edit", "pinned": true},
    {"op": "set_visibility", "agent_id": "docs", "visible": false},
    {"op": "set_group_order", "group": "dev", "order": 5},
    {"op": "create_group", "group": {"id": "research", "title": "研究工具", "order": 20}},
    {"op": "set_auto_sort", "group": "dev", "enabled": false}
  ]
}
```

| 操作 | 字段 | 说明 |
| --- | --- | --- |
| `move` | `agent_id`、`group`、`order` | 移动卡片并设置顺序 |
| `pin` | `agent_id`、`pinned` | 固定或取消固定 |
| `set_visibility` | `agent_id`、`visible` | 显示或隐藏 |
| `set_group_order` | `group`、`order` | 调整分组顺序 |
| `create_group` | `group` | 创建分组 |
| `rename_group` | `group`、`title` | 重命名分组 |
| `set_auto_sort` | `group`、`enabled` | 开关自动排序 |

响应返回应用后的布局版本：

```json
{
  "layout_version": 12,
  "updated_at": "2026-10-25T02:31:00.000Z",
  "applied": 6
}
```

布局操作是幂等的。并发修改使用 `If-Match: <layout_version>`，版本不一致返回 `409 LAYOUT_VERSION_CONFLICT`，Aliya 侧重新拉取后再提交。

## 6.7 agent 启停

```http
POST /v1/agents/edit/disable
Content-Type: application/json

{"reason": "维护中", "cancel_running": false}
```

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `reason` | 空 | 记录到审计与卡片提示 |
| `cancel_running` | `false` | 为 `true` 时取消该 agent 的运行中任务 |

| 操作 | 行为 |
| --- | --- |
| 禁用 | `enabled=false`，拒绝新任务；已有任务默认继续 |
| 启用 | `enabled=true`，重新允许路由；若未探测则先探测 |
| 探测 | 调用 health 与 describe，更新 `health` 与 `version` |
| 下线 | 从桌面移除，保留历史任务；有运行中任务时需 `cancel_running=true` |

禁用是软操作，随时可以恢复。下线是强操作，恢复需要重新注册。

## 6.8 任务查询与干预

任务详情：

```http
GET /v1/tasks/01JAV3M8Z9K2Q4W6R8T0Y2B4C6
```

```json
{
  "task_id": "01JAV3M8Z9K2Q4W6R8T0Y2B4C6",
  "agent_id": "edit",
  "status": "running",
  "progress": 0.4,
  "current_step": "修改 core/agent/registry.py",
  "priority": "normal",
  "route": {"source": "natural_language", "selected_agent_id": "edit", "confidence": 0.93},
  "created_at": "2026-10-25T02:30:00.000Z",
  "started_at": "2026-10-25T02:30:01.000Z",
  "deadline_at": "2026-10-25T02:32:00.000Z",
  "events_cursor": 4,
  "result": null,
  "error": null
}
```

干预接口：

| 方法 | 路径 | 请求体 | 行为 |
| --- | --- | --- | --- |
| `POST` | `/v1/tasks/{id}/cancel` | `{"reason": "aliya_cancelled"}` | 协作式取消，传播到下游任务 |
| `POST` | `/v1/tasks/{id}/input` | `{"input_request_id": "...", "value": {...}}` | 回答未决输入请求 |
| `POST` | `/v1/tasks/{id}/approve` | `{"approval_id": "...", "reason": null}` | 批准审批单并继续 |
| `POST` | `/v1/tasks/{id}/reject` | `{"approval_id": "...", "reason": "风险过高"}` | 拒绝审批单，任务取消或收敛 |
| `POST` | `/v1/tasks/{id}/retry` | `{"request_id": "...", "from_step": null}` | 创建重试任务，记录 `retry_of` |
| `POST` | `/v1/tasks/{id}/reassign` | `{"agent_id": "test", "input": {...}}` | 取消原任务并改派 |
| `POST` | `/v1/tasks/{id}/priority` | `{"priority": "high"}` | 调整未开始任务的优先级 |

| 干预 | 允许的任务状态 | 失败错误码 |
| --- | --- | --- |
| 取消 | `queued`、`waiting_*`、`dispatched`、`running` | `TASK_NOT_CANCELLABLE` |
| 输入 | `waiting_input` | `NO_PENDING_INPUT` |
| 审批 | `waiting_approval` | `NO_PENDING_APPROVAL` |
| 重试 | 终态且 `error.retryable=true`，或 `succeeded` 且允许复跑 | `TASK_NOT_RETRYABLE` |
| 改派 | 未进入终态或失败任务 | `AGENT_DISABLED`、`TASK_NOT_REASSIGNABLE` |
| 优先级 | `queued` | `TASK_ALREADY_STARTED` |

取消、拒绝与改派都会写入审计日志，并在任务详情中保留操作记录。

## 6.9 事件流

```http
GET /v1/events?since_seq=0
Authorization: Bearer <token>
Accept: text/event-stream
```

事件格式：

```text
id: 1842
event: desktop.card.updated
data: {"stream_seq":1842,"at":"2026-10-25T02:30:12.000Z","agent_id":"edit","patch":{"status":"running","status_text":"编辑中","metrics":{"progress":0.4}}}

id: 1843
event: notification.created
data: {"stream_seq":1843,"at":"2026-10-25T02:30:19.000Z","notification":{"notification_id":"ntf-01JAV6K6","level":"info","title":"Edit agent 已完成","task_id":"01JAV3M8Z9K2Q4W6R8T0Y2B4C6"}}
```

| 事件 | 说明 |
| --- | --- |
| `desktop.card.updated` | 卡片字段补丁，包含 `agent_id` 与 `patch` |
| `desktop.layout.updated` | 分组、顺序、可见性变化，附带 `layout_version` |
| `agent.registered` | 新 agent 注册成功 |
| `agent.updated` | 探测、版本、健康变化 |
| `agent.retired` | agent 下线 |
| `task.updated` | 任务状态、进度、结果变化 |
| `notification.created` | 新通知 |
| `system.alert` | cosmos 自身告警 |
| `stream.heartbeat` | 每 15 秒一次，维持连接并携带当前 `stream_seq` |
| `stream.gap` | 缺口超出保留窗口，客户端应重新拉取快照 |

| 规则 | 说明 |
| --- | --- |
| 续传 | 使用 `Last-Event-ID` 或 `since_seq`；保留最近 10 分钟或 10000 条 |
| 去重 | 客户端按 `stream_seq` 丢弃重复事件 |
| 补丁应用 | 卡片补丁为浅合并；数组字段整体替换 |
| 连接保持 | 服务端每 15 秒发送 `stream.heartbeat`；客户端 45 秒无事件则重连 |
| 背压 | 单连接缓冲超过 1 MB 时，服务端发送 `stream.gap` 并断开，客户端改走快照 |
| 鉴权 | SSE 连接使用同一 Bearer 令牌；令牌过期时服务端发送 `system.alert` 后关闭 |

事件流断开期间，桌面保留最后状态并标注「数据可能过期」；通知通过快照与通知中心补发。

## 6.10 通知偏好

```http
PATCH /v1/notifications/preferences
Content-Type: application/json

{
  "min_level": "info",
  "quiet_hours": {"enabled": true, "start": "23:00", "end": "08:00"},
  "channels": {
    "desktop": true,
    "webhook": {"enabled": true, "url": "http://127.0.0.1:9000/aliya/notifications"}
  },
  "digest": {"enabled": false, "interval_ms": 300000}
}
```

| 字段 | 说明 |
| --- | --- |
| `min_level` | `info`、`warning`、`critical` 的最低推送级别 |
| `quiet_hours` | 静默时段；`critical` 不受影响 |
| `channels.desktop` | 桌面轻提示与通知中心 |
| `channels.webhook` | 推送到 Aliya 侧回调地址，失败按 1、2、4、8 分钟重试 |
| `digest` | 聚合低优先级通知，减少打扰 |

webhook 载荷与通知对象一致，带 `X-Cosmos-Signature` 签名头。Aliya 侧校验签名后再处理。

确认通知：

```http
POST /v1/notifications/ntf-01JAV6K6/ack
Content-Type: application/json

{"acknowledged_at": "2026-10-25T02:31:00.000Z"}
```

`critical` 通知在确认前每 5 分钟重复提醒，最多 3 次。

## 6.11 错误格式

```json
{
  "error": {
    "code": "AGENT_DISABLED",
    "message": "Edit agent 已禁用，无法发起调用。",
    "details": {"agent_id": "edit"},
    "request_id": "req-20261025-0010",
    "retryable": false
  }
}
```

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| `UNAUTHENTICATED` | 401 | 缺少或无效令牌 |
| `FORBIDDEN` | 403 | scope 不足 |
| `AGENT_NOT_FOUND` | 404 | agent 不存在 |
| `AGENT_DISABLED` | 409 | agent 已禁用 |
| `AGENT_OFFLINE` | 503 | agent 离线或探测失败 |
| `INVALID_INPUT` | 400 | 输入不满足 schema |
| `ROUTE_AMBIGUOUS` | 409 | 需要澄清 |
| `TASK_NOT_FOUND` | 404 | 任务不存在 |
| `TASK_NOT_CANCELLABLE` | 409 | 任务已终态 |
| `NO_PENDING_INPUT` | 409 | 没有未决输入请求 |
| `NO_PENDING_APPROVAL` | 409 | 没有未决审批 |
| `TASK_NOT_RETRYABLE` | 409 | 任务不可重试 |
| `TASK_NOT_REASSIGNABLE` | 409 | 任务不可改派 |
| `TASK_ALREADY_STARTED` | 409 | 任务已开始，无法调整优先级 |
| `APPROVAL_REQUIRED` | 409 | 需要先审批 |
| `RESOURCE_EXHAUSTED` | 429 | 队列或并发饱和 |
| `RATE_LIMITED` | 429 | 超过请求频率 |
| `LAYOUT_VERSION_CONFLICT` | 409 | 布局版本冲突 |
| `INTERNAL` | 500 | 未预期错误 |

429 响应带 `Retry-After` 头。错误响应的 `message` 面向 Aliya，可直接展示；`details` 用于界面定位，不包含堆栈。

## 6.12 限流与审计

| 接口 | 默认限流 |
| --- | --- |
| `POST /v1/commands` | 30 次/分钟 |
| `POST /v1/agents/{id}/invoke` | 60 次/分钟 |
| `PATCH /v1/desktop/layout` | 60 次/分钟 |
| 任务干预 | 120 次/分钟 |
| 事件流 | 单账号最多 3 条并发连接 |

限流按账号与会话组合计算，`critical` 审批不受命令限流影响。

审计记录字段：

```json
{
  "audit_id": "aud-01JAV7M1",
  "at": "2026-10-25T02:31:00.000Z",
  "actor": "aliya",
  "action": "agent.disable",
  "target": "edit",
  "request_id": "req-20261025-0012",
  "before": {"enabled": true},
  "after": {"enabled": false},
  "result": "ok"
}
```

审计覆盖注册、启停、下线、桌面布局、指令、调用、取消、输入、审批、重试、改派与通知偏好变更。审计只追加，不提供修改接口。
