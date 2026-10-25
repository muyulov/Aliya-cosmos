# 5. 子 agent 接口规范

本文定义 cosmos 与子 agent 之间的完整契约：任务如何下发、结果如何返回、状态如何上报、异常如何表达、取消与补充输入如何工作。契约同时适用于进程内调用与 HTTP 调用。

## 5.1 版本与兼容

| 规则 | 说明 |
| --- | --- |
| 主版本 | `schema_version` 主版本不同时拒绝通信，返回 `CONTRACT_VERSION_MISMATCH` |
| 次版本 | 新增可选字段属于兼容变更，读取方忽略未知字段 |
| 字段删除 | 至少保留一个大版本，旧字段标记 deprecated |
| 枚举扩展 | 接收方必须能忽略未知枚举值并记录警告 |
| 事件类型 | 新增类型属于兼容变更；不得改变既有类型语义 |

cosmos 在任务信封中携带 `schema_version`，agent 在结果与事件中回传相同版本。版本不一致时，cosmos 拒绝任务并在卡片上标记契约错误。

## 5.2 标识与时间

| 标识 | 生成方 | 格式 | 用途 |
| --- | --- | --- | --- |
| `task_id` | cosmos | ULID 或 UUIDv7 | 任务唯一标识，全链路关联 |
| `request_id` | Aliya 或 cosmos | 字符串，建议 64 字符内 | 幂等键 |
| `trace_id` | cosmos | W3C trace-id 或 UUID | 跨 agent 追踪 |
| `event_id` | agent 或 SDK | ULID | 事件去重 |
| `seq` | agent | 从 1 开始的正整数 | 单任务内事件顺序 |
| `plan_id` | cosmos | ULID | 多步骤计划标识 |
| `step_id` | cosmos | 字符串 | 计划内步骤标识 |
| `approval_id` | agent 或 cosmos | ULID | 审批单标识 |
| `input_request_id` | agent | ULID | 输入请求标识 |

时间统一使用 RFC 3339 UTC 毫秒精度。`issued_at`、`deadline_at`、`at` 以 cosmos 时钟为准；agent 本地时钟偏差超过 2 秒时，cosmos 以接收时间修正展示，但保留原始时间用于审计。

## 5.3 任务信封 TaskEnvelope

cosmos 下发给 agent 的完整结构：

```json
{
  "schema_version": "1.0",
  "task_id": "01JAV3M8Z9K2Q4W6R8T0Y2B4C6",
  "request_id": "req-20261025-0001",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "agent_id": "edit",
  "parent_task_id": null,
  "retry_of": null,
  "issued_by": "cosmos",
  "issued_at": "2026-10-25T02:30:00.000Z",
  "deadline_at": "2026-10-25T02:32:00.000Z",
  "priority": "normal",
  "intent": {
    "summary": "把 registry.py 的日志字段改成中文",
    "raw_text": "把 core/agent/registry.py 里的日志字段改成中文",
    "action": "edit",
    "objects": ["core/agent/registry.py"]
  },
  "input": {
    "target_files": ["core/agent/registry.py"],
    "instruction": "日志字段改成中文",
    "mode": "patch"
  },
  "context": {
    "session_id": "s-01",
    "workspace": "/workspace",
    "locale": "zh-CN",
    "attachments": []
  },
  "callback": {
    "events_url": "http://127.0.0.1:8080/v1/tasks/01JAV3M8Z9K2Q4W6R8T0Y2B4C6/events",
    "artifacts_url": "http://127.0.0.1:8080/v1/tasks/01JAV3M8Z9K2Q4W6R8T0Y2B4C6/artifacts",
    "token": "<task-scoped-token>",
    "heartbeat_ms": 5000
  },
  "policy": {
    "timeout_ms": 120000,
    "max_retries": 0,
    "retry_safe": false,
    "requires_approval": false
  },
  "metadata": {
    "route_source": "natural_language",
    "plan_id": "plan-01JAV5C2",
    "step_id": "s1"
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `parent_task_id` | 子任务指向父任务；顶层任务为 `null` |
| `retry_of` | 重试任务指向原任务；非重试为 `null` |
| `intent` | cosmos 对意图的归一化结果，供 agent 理解上下文 |
| `input` | 结构化参数，必须能通过 agent 的输入 schema 校验 |
| `context.workspace` | 授权工作区根路径；agent 不得越界 |
| `context.attachments` | 附件引用，包含 `artifact_id`、`mime`、`sha256` |
| `callback.events_url` | 事件上报地址，指向 cosmos，不指向 agent |
| `callback.token` | 任务级短期令牌，只能上报该任务的事件与工件 |
| `callback.heartbeat_ms` | 心跳间隔，agent 必须遵守 |
| `policy.timeout_ms` | 执行总时长上限 |
| `policy.retry_safe` | agent 是否声明可安全重试；由清单推导 |
| `metadata` | 路由来源、计划与步骤信息，agent 可只读 |

agent 必须把 `task_id`、`trace_id`、`agent_id` 写入自身日志上下文，便于跨层排查。

## 5.4 任务结果 TaskResult

任务成功时，`task.completed` 携带的 `result` 与任务查询返回的结果使用同一结构：

```json
{
  "schema_version": "1.0",
  "task_id": "01JAV3M8Z9K2Q4W6R8T0Y2B4C6",
  "agent_id": "edit",
  "status": "succeeded",
  "started_at": "2026-10-25T02:30:01.000Z",
  "finished_at": "2026-10-25T02:30:19.000Z",
  "output": {
    "summary": "已将 registry.py 的日志字段改为中文，逻辑未变。",
    "changes": [
      {"path": "core/agent/registry.py", "added": 6, "removed": 6}
    ]
  },
  "artifacts": [
    {
      "artifact_id": "art-01JAV6D1",
      "kind": "diff",
      "name": "registry.py.diff",
      "mime": "text/x-diff",
      "size_bytes": 2048,
      "sha256": "5ff3f7a4dd6310635ab5c8853b209b48f07fa293b3a38d8853478d7d58d12bc9",
      "uri": "cosmos://tasks/01JAV3M8Z9K2Q4W6R8T0Y2B4C6/artifacts/art-01JAV6D1",
      "created_at": "2026-10-25T02:30:18.000Z"
    }
  ],
  "metrics": {
    "elapsed_ms": 18200,
    "steps": 5,
    "tokens": 1200
  },
  "error": null
}
```

| 字段 | 说明 |
| --- | --- |
| `status` | `succeeded`、`failed`、`cancelled`、`timeout` |
| `output` | 结构化结果；必须可 JSON 序列化，建议包含 `summary` |
| `artifacts` | 工件元数据；内容由 cosmos 存储，agent 通过上传接口写入 |
| `metrics` | 耗时、步骤数、token 等可选指标 |
| `error` | 失败或取消时的错误对象；成功为 `null` |

`output` 默认不超过 64 KB。更大内容放入工件，`output` 只保留摘要与工件引用。

## 5.5 任务事件 TaskEvent

agent 通过事件上报进度、心跳、工件、输入请求、审批请求与终态：

```json
{
  "schema_version": "1.0",
  "event_id": "evt-01JAV6E2",
  "task_id": "01JAV3M8Z9K2Q4W6R8T0Y2B4C6",
  "agent_id": "edit",
  "seq": 3,
  "type": "task.progress",
  "at": "2026-10-25T02:30:12.000Z",
  "payload": {
    "progress": 0.4,
    "current_step": "修改 core/agent/registry.py",
    "message": "已完成 2/5 个文件",
    "metrics": {"elapsed_ms": 12000, "tokens": 400}
  }
}
```

### 事件类型

| `type` | 触发时机 | payload 关键字段 |
| --- | --- | --- |
| `task.started` | agent 确认开始执行 | `started_at` |
| `task.progress` | 进度变化 | `progress`、`current_step`、`message`、`metrics` |
| `task.heartbeat` | 周期性存活上报 | `elapsed_ms`、`progress`、`health`、`resources` |
| `task.log` | 需要呈现的日志 | `level`、`message`、`fields`（已脱敏） |
| `task.artifact` | 产出工件 | 单个工件或 `artifacts[]` |
| `task.needs_input` | 缺少必要信息 | `input_request_id`、`question`、`input_schema`、`options`、`expires_at` |
| `task.approval_required` | 运行时需要审批 | `approval_id`、`action`、`risk`、`summary`、`options`、`expires_at` |
| `task.completed` | 成功完成 | `result`（TaskResult） |
| `task.failed` | 执行失败 | `error`、`partial_output` |
| `task.cancelled` | 取消完成 | `reason`、`cancelled_by` |

`task.started`、`task.completed`、`task.failed`、`task.cancelled` 是生命周期事件。除 `task.started` 外，后三者是终态事件；每个任务必须且只能有一个终态事件。

### 事件顺序

- `seq` 从 1 开始，每个任务独立计数，严格递增 1。
- 每个事件都消耗一个 `seq`，包括心跳与日志。
- 同一任务的事件必须按 `seq` 顺序发送；重试时保持原 `event_id` 与 `seq`。
- `task.started` 必须在清单 `transport.start_timeout_ms` 内到达，默认 10 秒；超时视为启动失败。
- 心跳从 `task.started` 后开始，间隔不超过 `callback.heartbeat_ms`。没有进度变化时也要发心跳，`progress` 可保持不变。

### 事件上报语义

```http
POST /v1/tasks/{task_id}/events
Authorization: Bearer <task-scoped-token>
Content-Type: application/json

<TaskEvent>
```

| 项 | 约定 |
| --- | --- |
| 成功响应 | `202 Accepted`，表示已接收，不表示已处理完成 |
| 去重 | cosmos 按 `event_id` 去重；重复事件返回 `202` 并丢弃 |
| 顺序 | cosmos 按 `seq` 排序；乱序事件缓存最多 10 秒 |
| 缺口 | 缓存超时后仍有缺口，cosmos 调用 `GET /v1/tasks/{task_id}` 向 agent 对账 |
| 重试 | 网络错误或 5xx 时，agent 按 1、2、4、8、16、32、60 秒退避重试，最多 10 分钟 |
| 4xx | 属于契约错误，不重试；agent 记录错误并尽量继续执行 |
| 体积 | 单个事件不超过 256 KB；超出时改为上传工件并在事件中引用 |
| 鉴权 | 令牌绑定 `task_id` 与 `agent_id`，不能上报其他任务 |

## 5.6 心跳与健康

心跳是存活信号，独立于进度：

```json
{
  "schema_version": "1.0",
  "event_id": "evt-01JAV6F3",
  "task_id": "01JAV3M8Z9K2Q4W6R8T0Y2B4C6",
  "agent_id": "edit",
  "seq": 4,
  "type": "task.heartbeat",
  "at": "2026-10-25T02:30:15.000Z",
  "payload": {
    "elapsed_ms": 15000,
    "progress": 0.4,
    "health": "healthy",
    "resources": {"cpu_pct": 3.2, "mem_mb": 86},
    "message": "正在写入文件"
  }
}
```

| `health` | 含义 | cosmos 行为 |
| --- | --- | --- |
| `healthy` | 正常 | 维持 running |
| `degraded` | 可继续但性能下降 | 卡片降级，记录原因 |
| `unhealthy` | 难以继续 | 触发告警，等待 agent 终态或超时 |

心跳缺失处理：

| 缺失窗口 | 默认阈值 | cosmos 行为 |
| --- | --- | --- |
| 3 个 | 15 秒 | agent 标记 `degraded`，任务保持 `running` 并标记为停滞，通知 Aliya |
| 6 个 | 30 秒 | agent 标记 `offline`，任务进入超时流程，发送取消请求 |

## 5.7 取消、输入与审批

### 取消

cosmos 调用 agent：

```http
POST /v1/tasks/{task_id}/cancel
Authorization: Bearer <cosmos-agent-token>
Content-Type: application/json

{"reason": "aliya_cancelled", "grace_ms": 5000}
```

| 规则 | 说明 |
| --- | --- |
| 受理 | 立即返回 `202`；agent 在一个心跳内确认收到 |
| 协作式 | agent 停止发起新的副作用操作，尽快收敛 |
| 终态 | agent 在宽限期内上报 `task.cancelled`，或 `task.failed` 且 `error.code=CANCELLED` |
| 超时 | 超过宽限期未上报，cosmos 直接标记 `cancelled` 并关闭任务 |
| 幂等 | 重复取消返回 `202`，不产生副作用 |
| 不可取消点 | agent 应尽量把长任务切成可中断步骤；无法中断时必须在清单中声明 |

### 输入请求

agent 需要补充信息时上报：

```json
{
  "schema_version": "1.0",
  "event_id": "evt-01JAV6G4",
  "task_id": "01JAV3M8Z9K2Q4W6R8T0Y2B4C6",
  "agent_id": "edit",
  "seq": 5,
  "type": "task.needs_input",
  "at": "2026-10-25T02:30:20.000Z",
  "payload": {
    "input_request_id": "inp-01JAV6G4",
    "question": "registry.py 中有两处日志，修改哪一处？",
    "input_schema": {
      "type": "object",
      "properties": {"scope": {"type": "string", "enum": ["tool", "registry", "both"]}},
      "required": ["scope"]
    },
    "options": [
      {"id": "tool", "label": "工具执行日志"},
      {"id": "registry", "label": "注册表日志"},
      {"id": "both", "label": "两处都改"}
    ],
    "expires_at": "2026-10-25T02:35:20.000Z",
    "allow_free_text": false
  }
}
```

cosmos 将任务置为 `waiting_input`，通知 Aliya，并调用 agent：

```http
POST /v1/tasks/{task_id}/input
Content-Type: application/json

{"input_request_id": "inp-01JAV6G4", "value": {"scope": "both"}}
```

| 规则 | 说明 |
| --- | --- |
| 等待状态 | 任务不占用 agent 并发槽位，但保留任务上下文 |
| 过期 | 到期后 agent 决定使用默认值或失败；cosmos 同时标记等待超时 |
| 撤销 | Aliya 取消任务时，未回答的输入请求一并失效 |
| 多请求 | 同一任务同时最多一个未决输入请求，避免状态分叉 |

### 审批

下发前审批由 cosmos 处理，不进入 agent。运行时审批由 agent 发起：

```json
{
  "schema_version": "1.0",
  "event_id": "evt-01JAV6H5",
  "task_id": "01JAV3M8Z9K2Q4W6R8T0Y2B4C6",
  "agent_id": "edit",
  "seq": 6,
  "type": "task.approval_required",
  "at": "2026-10-25T02:30:30.000Z",
  "payload": {
    "approval_id": "apr-01JAV6H5",
    "action": "file.bulk_rewrite",
    "risk": "write",
    "summary": "将重写 12 个文件，预计删除 340 行、新增 410 行",
    "details": {"files": ["core/agent/tool.py", "core/agent/registry.py"]},
    "options": [
      {"id": "approve", "label": "批准"},
      {"id": "reject", "label": "拒绝"}
    ],
    "expires_at": "2026-10-25T02:40:30.000Z"
  }
}
```

cosmos 将任务置为 `waiting_approval` 并通知 Aliya。Aliya 通过控制面响应后，cosmos 调用：

```http
POST /v1/tasks/{task_id}/approval
Content-Type: application/json

{"approval_id": "apr-01JAV6H5", "approved": true, "reason": null}
```

| 规则 | 说明 |
| --- | --- |
| 单次有效 | 审批绑定 `approval_id`、`task_id`、动作与有效期 |
| 拒绝 | agent 停止该操作，任务可以继续执行其余安全步骤，或直接失败 |
| 过期 | 默认 10 分钟，过期按拒绝处理 |
| 审计 | 审批人、时间、决定、理由全部记录 |

## 5.8 工件

工件用于承载 diff、报告、截图、生成文件等大内容。agent 先上传，再在事件或结果中引用。

```http
POST /v1/tasks/{task_id}/artifacts
Authorization: Bearer <task-scoped-token>
Content-Type: multipart/form-data

file=<二进制内容>
metadata={"kind":"diff","name":"registry.py.diff","mime":"text/x-diff"}
```

响应：

```json
{
  "artifact_id": "art-01JAV6D1",
  "uri": "cosmos://tasks/01JAV3M8Z9K2Q4W6R8T0Y2B4C6/artifacts/art-01JAV6D1",
  "size_bytes": 2048,
  "sha256": "5ff3f7a4dd6310635ab5c8853b209b48f07fa293b3a38d8853478d7d58d12bc9"
}
```

| 规则 | 默认 | 说明 |
| --- | --- | --- |
| 单文件上限 | 50 MB | 超出返回 `ARTIFACT_TOO_LARGE` |
| 单任务数量 | 32 | 超出返回 `ARTIFACT_LIMIT_EXCEEDED` |
| 存储 | cosmos 管理 | agent 不上传可执行文件，不上传含密钥内容 |
| 生命周期 | 跟随任务保留策略 | 任务清理时一并清理 |
| 引用 | 事件与结果只放元数据 | 不把二进制塞进事件 |

## 5.9 错误模型

```json
{
  "code": "EDIT_CONFLICT",
  "message": "目标文件已被其他任务修改",
  "retryable": false,
  "details": {"path": "core/agent/registry.py", "expected_sha256": "5ff3f7a4dd6310635ab5c8853b209b48f07fa293b3a38d8853478d7d58d12bc9"}
}
```

| 字段 | 说明 |
| --- | --- |
| `code` | 大写下划线错误码；通用错误使用标准码，业务错误加 agent 前缀 |
| `message` | 面向 Aliya 的简短说明，中文，避免堆栈 |
| `retryable` | 是否可安全重试；仅在无副作用或 agent 声明可重试时为 `true` |
| `details` | 结构化上下文，禁止包含密钥与完整文件内容 |

标准错误码：

| 错误码 | 含义 | 可重试 |
| --- | --- | --- |
| `INVALID_INPUT` | 输入不满足 schema | 否 |
| `UNAUTHORIZED` | 令牌无效或过期 | 否 |
| `FORBIDDEN` | 越权访问 | 否 |
| `NOT_FOUND` | 目标资源不存在 | 否 |
| `CONFLICT` | 版本或资源冲突 | 否 |
| `TIMEOUT` | 执行超时 | 视场景 |
| `CANCELLED` | 已取消 | 否 |
| `DEPENDENCY_FAILED` | 依赖任务失败 | 否 |
| `RESOURCE_EXHAUSTED` | 资源不足 | 是 |
| `INTERNAL` | 未预期错误 | 视场景 |
| `CONTRACT_VERSION_MISMATCH` | 契约版本不兼容 | 否 |

堆栈只写 agent 日志，不进入 `message` 与事件。

## 5.10 HTTP 接口清单

### cosmos 调用 agent

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 存活与版本 |
| `GET` | `/describe` | 自述能力、特性、限制 |
| `POST` | `/v1/tasks` | 接收任务，返回 `202` |
| `GET` | `/v1/tasks/{task_id}` | 查询任务快照，用于事件缺口对账 |
| `POST` | `/v1/tasks/{task_id}/cancel` | 取消任务 |
| `POST` | `/v1/tasks/{task_id}/input` | 提交输入请求的回答 |
| `POST` | `/v1/tasks/{task_id}/approval` | 提交审批结果 |

### agent 调用 cosmos

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/tasks/{task_id}/events` | 上报任务事件 |
| `POST` | `/v1/tasks/{task_id}/artifacts` | 上传工件 |

两边的 `/v1/tasks` 路径形态相同，但主机与鉴权不同：agent 侧使用 cosmos 签发的任务令牌，cosmos 侧使用 agent 注册时配置的服务令牌。实现时不要把两者混用。

## 5.11 进程内协议

进程内 agent 实现以下协议，cosmos 提供 `EventEmitter`：

```python
from collections.abc import Awaitable, Callable
from typing import Any, Protocol


class EventEmitter(Protocol):
    async def emit(self, type: str, payload: dict[str, Any]) -> None:
        """由 cosmos 填充 event_id、seq、task_id、agent_id 与 at。"""
        ...


class SubAgent(Protocol):
    agent_id: str

    async def describe(self) -> dict[str, Any]:
        """返回与 /describe 一致的自述结构。"""
        ...

    async def execute(self, task: "TaskEnvelope", emit: EventEmitter) -> "TaskResult":
        """执行任务；事件通过 emit 上报，终态结果作为返回值。"""
        ...

    async def cancel(self, task_id: str, reason: str) -> None:
        """协作式取消；应在宽限期内让 execute 收敛。"""
        ...

    async def provide_input(
        self, task_id: str, input_request_id: str, value: dict[str, Any]
    ) -> None:
        """提交输入；未决的 execute 应被唤醒。"""
        ...

    async def resolve_approval(self, task_id: str, approval_id: str, approved: bool) -> None:
        """提交审批结果。"""
        ...
```

进程内模式下，`emit` 负责事件身份与顺序，agent 只提供 `type` 与 `payload`。HTTP 模式下由 agent SDK 完成同样工作。两种模式的 TaskEnvelope、TaskResult 与任务事件结构完全一致。

## 5.12 契约一致性检查

接入新 agent 时逐项验证：

| 检查项 | 通过标准 |
| --- | --- |
| schema | 信封、结果、事件均通过 `schemas/` 校验 |
| 启动 | 收到任务后 `start_timeout_ms` 内上报 `task.started` |
| 心跳 | 空闲与执行中都按 `heartbeat_ms` 上报 |
| 顺序 | `seq` 连续递增，无重复或跳跃 |
| 终态 | 每个任务恰好一个终态事件 |
| 取消 | 取消后宽限期内收敛，重复取消幂等 |
| 输入 | 输入请求可回答、可过期、可随任务取消 |
| 审批 | 审批单绑定任务与动作，过期按拒绝处理 |
| 工件 | 上传成功后再引用，元数据与内容校验和一致 |
| 错误 | 业务错误码稳定，`message` 不含堆栈与密钥 |
| 幂等 | 同一 `task_id` 重复下发不产生二次副作用 |
| 大输入 | 超过 256 KB 的输入被拒绝并返回明确错误 |

通过以上检查后，agent 才允许在桌面 `visible=true` 展示。
