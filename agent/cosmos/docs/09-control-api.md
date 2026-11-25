# 09 Aliya 控制接口

`ControlService` 是 cosmos 向 Aliya 暴露的全部能力面。Aliya 只依赖这个接口与 `contracts` 里的模型，不接触任何内部实现。

设计原则：**能力够用、权限最小、错误可读**。

---

## 1. 能力清单

| 域 | 方法 | 用途 |
| --- | --- | --- |
| 会话 | `handshake()` | 版本与能力协商 |
| 会话 | `inbound()` | 接收用户消息信封（异步迭代） |
| 会话 | `describe_desktop()` | 拉取桌面全量快照 |
| 会话 | `get_agent(agent_id)` | 单卡详情 |
| 会话 | `open_agent(agent_id)` | 桌面打开：只读详情与参数 schema，不启动任务 |
| 会话 | `subscribe(topics)` | 事件订阅 |
| 调度 | `plan(intent)` | 生成调度计划，不执行 |
| 调度 | `invoke_agent(agent_id, params)` | 显式调用（桌面点击路径） |
| 调度 | `execute_plan(plan)` | 执行计划 |
| 调度 | `cancel_task(task_id)` | 取消单个任务 |
| 调度 | `cancel_dispatch(dispatch_id)` | 取消整批 |
| 调度 | `retry_task(task_id)` | 重试 |
| 调度 | `list_tasks(agent_id=None, limit=20)` | 任务列表 |
| 桌面 | `patch_layout(patch)` | 排序 / 分组 / 置顶 / 折叠 |
| 状态 | `get_presence()` | 用户状态快照 |
| 状态 | `set_presence_visibility(fields)` | 要求不看 / 恢复某些状态字段 |
| 状态 | `report_presence(signal)` | 外部通道上报状态（不经过 Aliya） |
| 会话 | `get_link_state()` | 当前通讯链路状态 |
| 世界 | `get_world_view(force=False)` | 世界推演结果 |
| 世界 | `refresh_world()` | 强制刷新推演 |
| 世界 | `set_aliya_context(ctx)` | 提供 Aliya 侧上下文 |
| 消息 | `deliver(text, session_id=...)` | 回应用户 |
| 消息 | `notify(text)` | 进展通知（受节流） |
| 消息 | `send_proactive(text, urgency=0.5)` | 主动发起对话（受节流；`urgency >= 0.8` 可突破小时预算） |
| 消息 | `history(session_id, limit=20)` | 读取对话历史 |

### 1.1 传输与 RPC 映射

生产环境 Aliya 通过 Unix 域套接字 + NDJSON 调用 `ControlServerService`：

| 行类型 | 方向 | 说明 |
| --- | --- | --- |
| `hello` | 客户端 → 服务端 | 首行，携带 token 与 `contract_version` |
| `hello_ok` | 服务端 → 客户端 | 能力、服务端时间、时区 |
| `request` / `response` | 双向 | `method` + `params`；响应带 `id` 与 `ok` |
| `inbound` / `ack_inbound` | 服务端 → 客户端 / 反向 | 用户消息与确认；未确认消息保留在会话 pending |
| `subscribe` / `subscribed` | 双向 | 主题与游标；重连后按游标补偿 |
| `event` | 服务端 → 客户端 | 事件推送 |
| `ping` / `pong` | 双向 | 心跳 |

RPC 方法名与 `ControlService` 同名；`now` 是额外方法，返回 `server_time` 与 `timezone`。`deliver` / `notify` / `send_proactive` 接受可选 `client_msg_id`，服务端按该 id 去重，客户端重连后可安全重放 outbox。

---

## 2. 会话与协商

```python
class CosmosCapabilities(BaseModel):
    api_version: str                     # 如 "1.0"
    contract_version: str                # 契约模型版本
    event_topics: list[str]              # 支持订阅的主题
    features: list[str]                  # "world" / "presence.mood" / "dispatch.auto" ...
    limits: dict[str, int]               # max_parallel / max_content_chars / ...


async def handshake(self) -> CosmosCapabilities: ...
```

分进程部署时必须先 `handshake()`，据此决定：

- 订阅哪些主题（不订阅不存在的主题会失败）。
- 是否启用依赖 `world` 的自主行动逻辑。
- 文本长度上限，避免超大 `content` 传输失败。

同进程部署可跳过，但**契约版本仍需校验**：`contract_version` 主版本不一致时直接报错，不做兼容猜测。

消息入口是独立流：

```python
async def inbound(self) -> AsyncIterator[InboundEnvelope]: ...
```

`inbound()` 与 `subscribe()` 是两条并行流：前者只出用户消息信封，后者只出状态与任务事件。合成一条会让「对话」与「内部状态」在同一队列里争顺序。

---

## 3. 桌面操作

```python
async def describe_desktop(self) -> DesktopSnapshot: ...
async def get_agent(self, agent_id: str) -> AgentDetail: ...
async def open_agent(self, agent_id: str) -> AgentDetail: ...
async def patch_layout(self, patch: DesktopLayoutPatch) -> DesktopSnapshot: ...
```

```python
class AgentDetail(BaseModel):
    card: AgentCard
    status: AgentStatus
    task: AgentTaskView | None
    readable: ReadableState
    recent_tasks: list[TaskBrief]     # 最近 5 条
```

规则：

| 规则 | 说明 |
| --- | --- |
| `describe_desktop()` 返回全量 | 含所有分组与条目，`revision` 用于判断是否需要重绘 |
| `patch_layout()` 幂等 | 同一补丁重复应用结果不变；成功后 `revision + 1` |
| 布局偏好持久化 | 写入 `data/state/desktop.json`，重启保留 |
| 不存在 / 已注销的 agent | `AgentNotFoundError`；`disabled` 的仍可查详情 |

Aliya **不能**通过 `patch_layout` 改变 `visible=false` 的条目——可见性是注册元数据，不是用户偏好。它只能调顺序、分组、置顶、折叠。

---

## 4. 调度

```python
async def plan(self, intent: DispatchIntent) -> DispatchPlan: ...
async def invoke_agent(
    self,
    agent_id: str,
    params: dict[str, object],
    *,
    session_id: str,
) -> TaskHandle: ...
async def execute_plan(self, plan: DispatchPlan) -> DispatchHandle: ...
async def cancel_task(self, task_id: str) -> None: ...
async def cancel_dispatch(self, dispatch_id: str) -> None: ...
async def retry_task(self, task_id: str) -> TaskHandle: ...
async def list_tasks(self, agent_id: str | None = None, limit: int = 20) -> list[TaskBrief]: ...
```

```python
class TaskHandle(BaseModel):
    task_id: str
    agent_id: str
    label: str
    accepted_at: datetime
    queued: bool          # 是否进入队列（并发已满且 queue=true）


class DispatchHandle(BaseModel):
    dispatch_id: str
    task_ids: list[str]
    accepted_at: datetime
    status: Literal["accepted", "partial"]   # partial 表示部分任务被拒（附 rejected）
    rejected: list[RejectedTask] = []        # 被拒任务及原因
```

`execute_plan()` 返回 `DispatchHandle` 而不是 `DispatchOutcome`：提交是异步的，整批结果由 `dispatch.finished` 事件携带 `DispatchOutcome` 送达。想同步等结果只能在测试里做，生产链路一律等事件。

| 方法 | 是否阻塞 | 说明 |
| --- | --- | --- |
| `plan()` | 是（≤ 3s） | 只算计划，不产生副作用 |
| `invoke_agent()` | 否 | 立即返回 `task_id`，结果经事件推送 |
| `execute_plan()` | 否 | 同上，整批提交并返回 `DispatchHandle`；结果等 `dispatch.finished` |
| `cancel_*()` | 是（≤ 5s） | 等宽限期结束；未退出时返回成功但状态标 `stalled` |
| `retry_task()` | 否 | 仅 `retryable=true` 且上次失败；否则 `AgentInvokeError` |
| `list_tasks()` | 是 | 只含摘要，不含 `content` |

`invoke_agent()` 与 `execute_plan()` 的区别：前者是「我知道用哪个」，后者是「这是计划，照做」。两者最终都进 `AgentRuntimeService.submit()`。

---

## 5. 状态读取

```python
async def get_presence(self) -> PresenceSnapshot: ...
async def set_presence_visibility(self, fields: list[PresenceField]) -> None: ...
async def get_link_state(self) -> LinkState: ...
async def report_presence(self, signal: PresenceInput) -> PresenceSnapshot: ...
async def get_world_view(self, force: bool = False) -> WorldInference | None: ...
async def refresh_world(self) -> WorldInference | None: ...
async def set_aliya_context(self, ctx: AliyaContext) -> None: ...
```

| 方法 | 说明 |
| --- | --- |
| `get_presence()` | 返回最新快照；采集关闭时返回 `unknown` 快照，不抛错 |
| `set_presence_visibility()` | 她可以要求「别让我看这个」：列出的字段停止推送并折叠；传空列表恢复。cosmos 不问原因 |
| `get_link_state()` | `up` / `unstable` / `down`；`down` 期间 `deliver` 会进队列而不是失败；心跳超时后自动降级 |
| `report_presence()` | 外部通道上报在线/输入/设备状态；Aliya 不需要也不应该伪造用户状态 |
| `get_world_view()` | 走缓存；`force=True` 同步等一次新推演（受 10s 超时，默认不复用缓存） |
| `refresh_world()` | 绕过窗口，但允许复用相同输入的缓存；事件链路常用，返回结果或 `None` |
| `set_aliya_context()` | 只存不解读；覆盖式写入，`None` 字段表示清除 |

`get_presence()` 是同步读取内存，不是实时采集。需要变更通知时用订阅，不要轮询。

---

## 6. 消息

```python
async def deliver(self, text: str, *, session_id: str) -> None: ...
async def notify(self, text: str, *, dispatch_id: str | None = None) -> ProactiveReceipt: ...
async def send_proactive(
    self, text: str, *, urgency: float = 0.5
) -> ProactiveReceipt: ...
async def history(self, session_id: str, limit: int = 20) -> list[TerminalMessage]: ...
```

| 方法 | 受节流 | 用途 |
| --- | --- | --- |
| `deliver()` | 否 | 用户正在对话时的直接回应，必须送达 |
| `notify()` | 是（免打扰） | 进展通知，可被延迟 |
| `send_proactive()` | 是（全部策略） | 主动发起对话；普通建议受每小时预算，`urgency >= 0.8` 只突破小时预算 |

`deliver()` 不节流是刻意的：用户问了一句，Aliya 必须能回答。节流只作用于「Aliya 单方面想说」的场景。

---

## 7. 订阅

```python
async def subscribe(
    self,
    topics: list[str],
    *,
    since_sequence: int | None = None,
) -> AsyncIterator[CosmosEvent]: ...
```

```python
class CosmosEvent(BaseModel):
    event_id: str
    topic: str
    sequence: int
    at: datetime
    payload: dict[str, object]
```

主题清单：

| 主题 | 载荷 |
| --- | --- |
| `agent.state.changed` | `AgentRuntimeState` |
| `agent.progress` | `AgentProgress` |
| `agent.result` | `AgentResult` + `dispatch_id` |
| `agent.abnormal` | `AgentId` + `reason` + `AgentErrorInfo` |
| `desktop.layout.changed` | `DesktopSnapshot` |
| `presence.changed` | `PresenceChanged` |
| `world.inference` | `WorldInferenceEvent` |
| `user.active` | `session_id` + 时间戳 |
| `link.state.changed` | `LinkState` + 积压条数 |
| `dispatch.finished` | `DispatchOutcome` |

规则：

| 规则 | 说明 |
| --- | --- |
| 断点续订 | `since_sequence` 补发缺口，最多 100 条；超出时 `subscribe()` 自动返回「当前快照 + 实时事件」 |
| 去重 | 按 `event_id`，Aliya 侧必须实现 |
| 顺序 | 同一主题内 `sequence` 递增；跨主题不保证 |
| 慢消费 | 缓冲上限 1000 条，超出丢弃最旧并推 `slow_consumer` 告警 |
| 取消订阅 | `async for` 退出即取消，不需要显式 `unsubscribe` |

---

## 8. 权限矩阵

| 能力 | Aliya | 管理面 | 子 agent |
| --- | --- | --- | --- |
| 读桌面 / 任务 / 用户状态 / 链路状态 | 是 | 是 | 否 |
| 要求关闭某些状态字段 | 是 | 是 | 否 |
| 读用户状态原始字段 | 否（只给 `readable` 与快照） | 是 | 否（白名单除外） |
| 调度子 agent | 是 | 是 | 否 |
| 取消 / 重试 | 是 | 是 | 否 |
| 调整桌面布局 | 是 | 是 | 否 |
| 启用 / 禁用 agent | **否** | 是 | 否 |
| 注册 / 注销 agent | **否** | 是（需显式开启） | 否 |
| 修改采集开关 | **否** | 是 | 否 |
| 修改世界推理配置 | **否** | 是 | 否 |
| 向用户发消息 | 是 | 否 | 否 |

最后一行反向也成立：管理面不能代替 Aliya 与用户对话。部署者改配置，但不能借通道说话。

越权调用返回 `CosmosPermissionError`（`CosmosError` 子类），带 `capability` 与 `principal`。

---

## 9. 错误

| 场景 | 异常 | Aliya 的合理反应 |
| --- | --- | --- |
| `agent_id` 不存在 | `AgentNotFoundError` | 刷新桌面后重试或改选 |
| 参数不符合 schema | `AgentInvokeError(code="invalid_params")` | 补参数或转 `clarify` |
| 并发已满且不排队 | `AgentBusyError` | 等待或换 agent |
| 任务不存在 / 已归档 | `AgentNotFoundError(code="task_not_found")` | 忽略 |
| 重试不可重试的任务 | `AgentInvokeError(code="not_retriable")` | 不重试 |
| 越权 | `CosmosPermissionError` | 不重试，属实现错误 |
| 世界推演未启用 | —— | `get_world_view()` 返回 `None`，不抛错 |
| 接口版本不匹配 | `ContractVersionError` | 终止，需升级 |

所有异常继承 `CosmosError`，带 `code` / `message` / `retriable` 三个属性，Aliya 可按 `retriable` 决定是否重试，不必解析异常类型。

---

## 10. 幂等与超时

| 方法 | 幂等 | 超时 |
| --- | --- | --- |
| `handshake` | 是 | 3s |
| `describe_desktop` | 是 | 2s |
| `get_agent` / `list_tasks` | 是 | 2s |
| `patch_layout` | 是 | 2s |
| `plan` | 是（同样输入同结果，允许缓存 30s） | 5s |
| `invoke_agent` | 否（每次新任务；如需去重由 `session_id + params` 决定） | 2s 内返回 `task_id` |
| `execute_plan` | 是（按 `dispatch_id`） | 2s |
| `cancel_*` | 是 | 5s |
| `deliver` / `notify` / `send_proactive` | 否（可带 `client_msg_id` 去重） | 3s |
| `get_presence` / `get_world_view` | 是 | 1s（`force=True` 时 12s） |

超时语义：调用方超时不代表操作未生效。`invoke_agent` 超时后应先用 `list_tasks()` 核对，再决定是否重发。

---

## 11. 最小使用示例

```python
caps = await control.handshake()

desktop = await control.describe_desktop()
print(desktop.summary)                       # "2 个 agent 空闲，1 个正在执行"

# 用户在对话里提了个改文件的需求
plan = await control.plan(
    DispatchIntent(
        source="natural_language",
        session_id=session_id,
        user_text="把 logger 里的 log 改成 logger，顺便更新一下测试",
        hint="优先改 core/logger",
    )
)
if plan.decision == "clarify":
    await control.deliver(f"我需要再确认一下：{'、'.join(plan.missing)}", session_id=session_id)
elif plan.decision in {"single", "parallel"}:
    handle = await control.execute_plan(plan)
    await control.notify(f"开始处理：{plan.rationale}")

# 订阅进展
async for event in control.subscribe(["agent.progress", "agent.result", "presence.changed"]):
    ...
```

---

## 12. 日志

| 事件 | 级别 | 字段 |
| --- | --- | --- |
| 调用完成 | DEBUG | `方法`、`耗时毫秒`、`成功`、`错误码` |
| 越权 | WARNING | `方法`、`所需能力`、`调用方` |
| 慢调用（> 1s） | INFO | `方法`、`耗时毫秒` |
| 订阅建立 / 断开 | INFO | `主题数`、`since_sequence` |

不记录 `params` 与 `text` 内容。

---

## 13. 运行时事件载荷字段

下面两个模型由 `agent/cosmos/contracts/runtime.py` 定义，是 `agent.state.changed` 与 `agent.progress` 的载荷。

### 13.1 AgentRuntimeState

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `agent_id` | `str` | agent 标识 |
| `status` | `AgentStatus` | `idle` / `queued` / `running` / `stalled` / `abnormal` / `disabled` |
| `running` | `int` | 正在执行的任务数 |
| `queued` | `int` | 排队中的任务数 |
| `current_task_id` | `str \| None` | 当前任务；空闲时为 `None` |
| `last_heartbeat_at` | `datetime \| None` | 最后一次进度或心跳时间 |
| `last_error` | `AgentErrorInfo \| None` | 最近一次失败信息 |
| `error_streak` | `int` | 连续失败次数，成功后清零 |
| `last_used_at` | `datetime \| None` | 最近一次结束时间 |

### 13.2 AgentProgress

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | `str` | 任务标识（仅内部使用，不给用户看） |
| `agent_id` | `str` | agent 标识 |
| `label` | `str` | 任务的一句话描述，展示用 |
| `progress` | `float \| None` | 0–100；`None` 表示无法量化 |
| `step` | `str \| None` | 当前步骤短语 |
| `message` | `str \| None` | 可选的一句话，Aliya 决定是否转成用户通知 |
| `at` | `datetime` | 取 `ClockService.now()` |

`agent.abnormal` 使用 `AgentAbnormal`：`agent_id` + `reason`（`timeout` / `stalled` / `error` / `internal`）+ 可选 `error` + `at`。
