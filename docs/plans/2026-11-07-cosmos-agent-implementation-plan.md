# cosmos 主 agent 全量实现计划

> **给执行者：** 本计划按任务逐条执行，设计依据是 `agent/cosmos/docs/01..11` 与
> `agent/cosmos/config/agent-settings.example.yaml`。实施中若与设计冲突，以设计文档为准，
> 并在文末「实施补记」记录偏差。每个任务结束都必须通过判据三条才允许提交。

**目标：** 把 `agent/cosmos/` 从「设计规范」落成可运行的进程内主 agent：9 个常驻 Service +
契约模型 + 进程内总线 + 配置接入 + 装配入口，跑通 `docs/10-flows.md` 的五条端到端流程。

**架构：** `agent.cosmos` 依赖 `core`（单向），`core` 不 import `agent`。契约模型全部是
pydantic 模型且可 JSON 序列化；传输层是端口（`MessageBus` / `EventBus`），进程内实现由一个
`BusService` 承载生命周期；9 个服务全部是 `core.service.Service` 子类，由 `ServiceManager`
按类型分层启停。

**技术栈：** Python 3.12+ / uv / pydantic 2 / pytest + pytest-asyncio / ruff / basedpyright。
不新增第三方依赖。

**本计划范围：**

- 包含：`agent/cosmos` 的 contracts / errors / transport / 9 个 services / 配置模型 /
  装配入口 / 协议端口 / 端到端测试 / 文档同步。
- 不包含：`agent/aliya` 的实现、真实 Edit agent 的业务逻辑、真实用户通道（WebSocket / CLI）、
  真实世界推理模型。这三者通过端口注入口留在计划外。

**不在本计划内但必须留好接口：** `WorldReasoningPort`、`UserChannelPort`、
`AgentHandler`。实现它们不属于 cosmos，属于部署方或子 agent。

---

## 0. 动工前基线

```bash
cd /home/cosmos/项目/Aliya-cosmos
uv sync --no-install-project
.venv/bin/python -m pytest | tail -3
uv run ruff check . && uv run ruff format --check .
uvx basedpyright core tests
```

基线必须全绿。`agent/` 目前只有文档与空 `__init__.py`，`basedpyright` 暂不覆盖它；
从 Task 1 起判据改为：

```bash
uv run ruff check . && uv run ruff format --check .
uvx basedpyright agent core tests
.venv/bin/python -m pytest | tail -3
```

---

## 1. 先读：六个必须提前定死的落地决策

设计文档给了语义，但没说清怎么接现有容器。以下六条先定死，后续任务不再重复讨论。

### D1 配置注入：统一注入 `AgentSettings`

`ServiceManager._validate_contract()`（`core/service/manager.py:497`）只接受三类构造器参数：
`Settings`、`core.service.Service` 子类、**Settings 顶层字段**的 pydantic 节点。`AgentCosmosSettings`
是 `AgentSettings` 的嵌套字段，容器不会索引它。

因此：所有 cosmos 服务的构造器统一声明 `config: AgentSettings`，在内部读
`self._config.cosmos.<节>`。不注入 `AgentCosmosSettings`，也不改 `core/service/manager.py`。

### D2 端口与总线：`BusService` 承载，端口用 setter 注入

容器同样不能注入 `UserChannelPort`、`WorldReasoningPort`、`AgentHandler` 这类 Protocol。

- 新建 `BusService(Service)`，内部持有 `MessageBus` 与 `EventBus` 的进程内实现；其余服务把它
  当普通依赖注入。这是「传输层是端口不是组件」的落地方式：端口是接口，`BusService` 只是
  进程内默认实现的生命周期宿主。
- `UserChannelPort`、`WorldReasoningPort` 通过 `set_*()` 方法在 `start()` 前注入。默认实现：
  - `MemoryUserChannel`：把发出的 `TerminalMessage` 存进内存队列，提供 `drain()`，供测试与
    CLI 读取。
  - 世界推理默认实现从配置动态加载：`agent.cosmos.world.port_module` +
    `port_factory`（新增两个配置项，见 Task 2）；未配置而 `world.enabled=true` 时，
    `start()` 抛 `WorldBridgeError`（对齐设计 07 章 8 节）。

### D3 装配入口：不改 `core.service.registry.build_manager`

`core` 不得 import `agent`。装配放在 `agent/cosmos/assembly.py`：

```python
def build_cosmos_manager(settings: Settings) -> ServiceManager:
    manager = build_manager(settings)          # core 的 4 个服务
    manager.register(...9 个 cosmos 服务...)    # 顺序即同层先后
    return manager
```

为了让新入口复用启停与信号处理，把 `core/main.py` 的 `_run` 提升为公开
`run(manager: ServiceManager) -> None`（行为不变，`main()` 内部改为调用它）。新入口
`agent/cosmos/app.py` 只做三件事：读配置、`setup_logging`、`asyncio.run(run(manager))`。
启动命令：`uv run python -m agent.cosmos.app`。

### D4 标识生成：不引新依赖

设计里写 ULID / `tsk_01H...`，只作示例。实现统一用 `prefix + uuid4().hex`：
`msg_` / `ses_` / `tsk_` / `dsp_` / `inf_` / `evt_` / `req_`。测试断言只校验前缀与非空，不校验长度。

### D5 时间与时钟

所有时间取 `ClockService.now()`；服务构造器注入 `ClockService`。禁止
`datetime.now()`、`time.time()`（`time.perf_counter()` 用于耗时测量是允许的）。

### D6 错误分层

`agent/cosmos/errors.py` 定义 `CosmosError`，带 `code` / `message` / `retriable` 三个属性：

```python
class CosmosError(Exception):
    def __init__(self, message: str, *, code: str = "internal", retriable: bool = False): ...
```

子类沿用设计 01 章 8 节：`AgentRegistrationError`、`AgentNotFoundError`、`AgentInvokeError`、
`AgentBusyError`、`DispatchError`、`PresenceError`、`PresencePrivacyError`、`WorldBridgeError`、
`TerminalRoutingError`、`MessageDeliveryError`，另加 `ContractVersionError`、
`CosmosPermissionError`（设计 09 章 9 节）。

- 输入问题 → 抛错，`retriable=False`
- 运行期失败 → 事件 + 降级，不杀进程
- 配置/编程错误 → 启动期 fail fast

---

## 2. 契约模型的总原则

- 全部 `model_config = ConfigDict(extra="ignore")`：为后续加字段留兼容空间（设计 11 章 5 节）。
- 不做裸 dict 跨层传递；只有 `params` / `payload` 这类「交给下游自解释」的字段保留 dict。
- `AgentCard`、`AgentRequest`、`AgentResult`、`InboundEnvelope`、`PresenceSnapshot`、
  `WorldInput`、`WorldInference` 七个模型必须有 JSON round-trip 测试。
- 文档未给字段的模型（`ConversationTurn`、`AgentRuntimeState`、`AgentProgress`、
  `Attachment`、`PresenceInput`）在本计划里补齐，落地后回写 `agent/cosmos/docs/` 对应章节。

---

# M1 地基：契约、错误、配置、总线

### Task 1: 错误树 + 测试夹具

**Files:**
- Create: `agent/cosmos/errors.py`
- Create: `tests/cosmos/__init__.py`
- Create: `tests/cosmos/conftest.py`
- Test: `tests/cosmos/test_errors.py`

**接口**

```python
class CosmosError(Exception):
    code: str
    retriable: bool
    def __init__(self, message: str, *, code: str = "internal", retriable: bool = False) -> None: ...

# 12 个子类，均只固定 code 默认值与 retriable 默认值
```

**实现要点**

- `code` 用类属性作默认值，实例可覆盖；`str(exc)` 只返回 message，日志侧用
  `self.log_error("…", exc)` 自动拆出类型与消息。
- `AgentNotFoundError` 额外带 `reason: str | None`（`"disabled"` / `"task_not_found"`）。
- `AgentBusyError` / `AgentInvokeError` 额外带 `agent_id: str | None`。
- `CosmosPermissionError` 额外带 `capability` 与 `principal`。
- `conftest.py` 提供三个夹具：
  - `clock`：真实 `ClockService(ClockSettings(tz="Asia/Shanghai"))`，已 `start()`。
  - `settings`：`Settings()`，直接构造，不读 YAML。
  - `bus`：`BusService(settings)`，已 `start()`。

**验收测试**

- 每个子类 `isinstance(exc, CosmosError)`、默认 `code`、默认 `retriable`。
- `AgentNotFoundError("x", reason="disabled")` 保留 `reason`。
- 未知 code 不影响 `str(exc)`。

**提交：** `feat(cosmos): 错误树与测试夹具`

---

### Task 2: 配置模型

**Files:**
- Modify: `core/config/settings.py`（在 `Settings` 前新增模型，`Settings` 加 `agent` 字段）
- Modify: `core/config/__init__.py`（导出 `AgentSettings` 及各子节点）
- Modify: `data/config/cosmos.yaml`（追加 `agent:` 段，取自 `agent/cosmos/config/agent-settings.example.yaml`）
- Modify: `agent/cosmos/config/agent-settings.example.yaml`（补 `world.port_module` / `port_factory`，`world.enabled` 改 `false`）
- Test: `tests/cosmos/test_settings.py`

**接口**

```python
class AgentContractSettings(BaseModel):
    api_version: str = "1.0"
    contract_version: str = "1.0.0"
    state_dir: str = "data/state"

class AgentLinkSettings(BaseModel):
    heartbeat_seconds: int = 30
    down_after_seconds: int = 90
    queue_limit: int = 1000
    replay_on_recover: bool = True
    suppress_proactive_when_down: bool = True

class AgentDispatchSettings(BaseModel): ...      # 字段见配置样例 28–38 行
class AgentDesktopSettings(BaseModel): ...       # 40–50 行；groups 为 list[DesktopGroupSettings]
class AgentPresenceSettings(BaseModel): ...      # 52–93 行；含 MoodSettings / PushSettings / PanelSettings / PrivacySettings / WordingSettings
class AgentTerminalSettings(BaseModel): ...      # 95–124 行；含 IntentSettings / RedactionSettings / DeliverySettings / ProactiveSettings
class AgentWorldSettings(BaseModel):             # 126–142 行
    enabled: bool = False
    port_module: str | None = None
    port_factory: str = "build"
    ...
class AgentRegistrationSettings(BaseModel):      # 单个 agents[] 条目
    id: str
    module: str
    handler: str = "handle"
    card: AgentCardConfig
class AgentCosmosSettings(BaseModel):
    contract: AgentContractSettings
    link: AgentLinkSettings
    dispatch: AgentDispatchSettings
    desktop: AgentDesktopSettings
    presence: AgentPresenceSettings
    terminal: AgentTerminalSettings
    world: AgentWorldSettings
    allow_runtime_registration: bool = False
    agents: list[AgentRegistrationSettings] = []
class AgentSettings(BaseModel):
    cosmos: AgentCosmosSettings = AgentCosmosSettings()
```

**实现要点**

- 数值字段全部加约束：`timeout_seconds > 0`、`max_concurrent >= 1`、`daily_limit >= 0`、
  `queue_limit >= 1`、`min_confidence` / `auto_threshold` / `high_threshold` 在 `[0, 1]`。
- `dispatch.mode` 用 `Literal["suggest", "auto"]`；`presence.mood.provider` 用
  `Literal["decision", "llm", "off"]`；`backend` 复用 `core.config.Backend`。
- 未知键告警：在 `AgentCosmosSettings` 上写一个 `model_validator(mode="before")`，用
  `type(self).model_fields` 对账输入 dict 的键，多余的 `log.warning("配置含未知键，已忽略", 键=…)`。
  每个子节点都复用同一个 helper（放 `core/config/settings.py` 顶部私有函数）。
  注意：校验函数里不要 import logger 造成环，`core.logger` 无反向依赖，可安全 import。
- `AgentCardConfig` 是 `AgentCard` 的 YAML 形态：`AgentCard` 从 `agent.cosmos.contracts` 导入会
  让 `core.config` 反向依赖 agent。**因此 `AgentCardConfig` 定义在 `core/config/settings.py` 里，
  只描述字段，不引用 agent 契约；由 `agent.cosmos.registry.bootstrap` 转成 `AgentCard`。**
- `data/config/cosmos.yaml` 的 `agent` 段只启示例 `echo` agent（Task 17 创建），
  `world.enabled: false`，其余保留样例默认值。

**验收测试**

- 空配置全部走默认值；完整样例 YAML 能 `model_validate` 通过。
- 非法值逐个报 `ValidationError`：`timeout_seconds: 0`、`max_parallel: 0`、
  `auto_threshold: 1.5`、`mode: yolo`。
- 未知键产生 WARNING（用 `caplog` 或 `loguru` 的 sink 捕获），且不阻断加载。
- `AgentSettings()` 可直接构造，`Settings().agent.cosmos.dispatch.mode == "suggest"`。

**提交：** `feat(cosmos): 新增 agent 配置模型并接入配置骨架`

---

### Task 3: 契约模型 · agent / desktop

**Files:**
- Create: `agent/cosmos/contracts/__init__.py`
- Create: `agent/cosmos/contracts/agent.py`
- Create: `agent/cosmos/contracts/desktop.py`
- Test: `tests/cosmos/test_contracts_agent.py`

**接口（字段以设计 02 / 03 / 08 章为准）**

```python
# agent.py
type AgentHandler = Callable[[AgentRequest, AgentContext], Awaitable[AgentResult]]

class AgentExample(BaseModel):
    input: str
    note: str | None = None

class DesktopMeta(BaseModel):
    group: str = "general"; order: int = 100; visible: bool = True
    pinned: bool = False; badge: str | None = None

class ExecutionPolicy(BaseModel):
    timeout_seconds: float = 120; max_concurrent: int = 1; queue: bool = False
    cancel_on_supersede: bool = False; retryable: bool = False; heartbeat_seconds: float = 30

class AgentPermissions(BaseModel):
    read_presence: bool = False; presence_fields: list[PresenceField] = []
    notify_user: bool = False; read_conversation: bool = False

class AgentCard(BaseModel):
    id: str; name: str; version: str = "0.1.0"; icon: str; summary: str
    description: str; capabilities: list[str]; params_schema: dict[str, object]
    examples: list[AgentExample] = []; desktop: DesktopMeta
    execution: ExecutionPolicy; permissions: AgentPermissions

class AgentRequest(BaseModel):
    request_id: str; agent_id: str; params: dict[str, object]; label: str
    created_at: datetime; deadline_at: datetime | None = None; correlation_id: str

class Artifact(BaseModel):
    kind: str; name: str; uri: str; mime: str | None = None; size: int | None = None

class AgentErrorInfo(BaseModel):
    code: str; message: str; retriable: bool; user_hint: str | None = None

class AgentMetrics(BaseModel):
    steps: int | None = None; tokens: int | None = None; files_touched: int | None = None

class AgentResult(BaseModel):
    request_id: str; status: Literal["ok", "partial", "failed", "cancelled"]
    summary: str; content: str | None = None; artifacts: list[Artifact] = []
    error: AgentErrorInfo | None = None; metrics: AgentMetrics | None = None
    truncated: bool = False
    # 构造辅助：ok / partial / failed / cancelled

class AgentContext(Protocol):
    request_id: str
    cancelled: bool
    remaining_seconds: float | None
    async def report(self, *, progress: float | None = None, step: str | None = None,
                     message: str | None = None) -> None: ...
    async def heartbeat(self) -> None: ...
    async def emit(self, artifact: Artifact) -> None: ...
    def child_scope(self, name: str) -> AbstractContextManager[None]: ...

class AgentMatch(BaseModel):
    agent_id: str; score: float; reason: str
```

```python
# desktop.py
type AgentStatus = Literal["idle", "queued", "running", "stalled", "abnormal", "disabled"]
type ReadableTone = Literal["calm", "busy", "strained", "alarming", "muted"]

class AgentTaskView(BaseModel): ...      # 设计 03 章 1 节
class ReadableState(BaseModel): ...
class DesktopEntry(BaseModel): ...
class DesktopGroup(BaseModel): ...
class DesktopSnapshot(BaseModel): ...
class DesktopLayoutPatch(BaseModel):
    reorder: list[str] | None = None
    move: dict[str, str] | None = None
    pin: dict[str, bool] | None = None
    collapse: dict[str, bool] | None = None
class AgentDetail(BaseModel):
    card: AgentCard; status: AgentStatus; task: AgentTaskView | None
    readable: ReadableState; recent_tasks: list[TaskBrief]
```

**实现要点**

- 字段约束用 `Field(pattern=..., min_length=..., max_length=...)`：
  `AgentCard.id` 正则 `^[a-z][a-z0-9_]{1,31}$`；`summary` 1–20 字；`description` 20–400 字；
  `capabilities` 每项 `^[a-z][a-z0-9_]*$` 且至少 1 项。
- `summary` 超长不抛错，由注册服务截断并 WARNING（设计 02 章 5 节）——因此模型层
  `max_length` 只对 `id` / `capabilities` 生效，`summary` / `description` 留给服务层校验。
- `AgentResult` 加 `model_validator(mode="after")` 校验设计 08 章 4.2 的四条：
  `ok` 无 error；`partial` / `failed` 必有 error；`cancelled` 的 `error.code == "cancelled"`；
  `summary` 非空。校验失败抛 `ValueError`（pydantic 包装成 `ValidationError`），不抛 `CosmosError`。
- `AgentContext` 用 `Protocol` + `runtime_checkable` 不需要；只作类型约束。
- `TaskBrief` 定义在 `contracts/terminal.py`（Task 5），本文件用 `TYPE_CHECKING` 前向引用，
  避免循环 import。若循环难拆，把 `TaskBrief` 提前到 `contracts/agent.py` 并在 terminal 再导出。

**验收测试**

- `AgentCard` 非法 id（`Edit`、`1edit`、`a`）逐个报错。
- `AgentResult.ok` / `.failed` / `.cancelled` 四条约束各一个反例。
- `AgentResult` / `AgentCard` / `DesktopSnapshot` JSON round-trip。
- `DesktopLayoutPatch` 只给 `pin` 时其余字段为 `None`。

**提交：** `feat(cosmos): 契约模型（agent / desktop）`

---

### Task 4: 契约模型 · presence / world

**Files:**
- Create: `agent/cosmos/contracts/presence.py`
- Create: `agent/cosmos/contracts/world.py`
- Modify: `agent/cosmos/contracts/__init__.py`
- Test: `tests/cosmos/test_contracts_presence.py`
- Test: `tests/cosmos/test_contracts_world.py`

**接口（字段以设计 04 / 07 章为准）**

```python
# presence.py
type PresenceField = Literal["online", "activity", "typing", "mood", "task_context", "env"]
type PresenceTone = Literal["calm", "engaged", "waiting", "away", "unknown"]

class OnlineInfo(BaseModel): ...
class ActivityInfo(BaseModel): ...
class TypingInfo(BaseModel): ...
class MoodInfo(BaseModel): ...
class TaskContextInfo(BaseModel): ...
class EnvInfo(BaseModel): ...
class PresenceFields(BaseModel):
    online: OnlineInfo; activity: ActivityInfo; typing: TypingInfo
    mood: MoodInfo; task_context: TaskContextInfo; env: EnvInfo
class Availability(BaseModel):
    available: bool; reason: Literal["channel_not_supported", "disabled_by_config", "no_evidence"] | None = None
class ReadablePresence(BaseModel): ...
class PresenceSnapshot(BaseModel): ...
class PresenceChanged(BaseModel): ...
class PresenceInput(BaseModel):        # 通道上报（文档未给，本计划补齐）
    online: Literal["online", "away", "busy", "offline"] | None = None
    is_typing: bool | None = None
    device: Literal["desktop", "mobile", "web"] | None = None
    timezone: str | None = None
    busy: bool | None = None
```

```python
# world.py
class TimeSignal(BaseModel): ...
class LinkSignal(BaseModel): ...
class PresenceSignal(BaseModel): ...
class TaskSignal(BaseModel): ...
class OutcomeSignal(BaseModel): ...
class TaskSignals(BaseModel): ...
class AgentSignals(BaseModel): ...
class HistoryPattern(BaseModel): ...
class AliyaContext(BaseModel): ...
class WorldInput(BaseModel): ...
class Hypothesis(BaseModel): ...
class ActionWindow(BaseModel): ...
class WorldInference(BaseModel): ...
class WorldInferenceEvent(BaseModel): ...
```

**实现要点**

- `PresenceSnapshot.availability` 的键类型是 `PresenceField`；用 `dict[PresenceField, Availability]`。
  Pydantic 不校验 dict 键的 Literal，服务层自己保证。
- `ReadablePresence.summary` ≤ 30 字、`detail` ≤ 50 字、`hint` ≤ 30 字：模型层不设
  `max_length`（派生文案超长时应截断而非抛错），由 `PresenceService` 截断。
- `MoodInfo.valence` ∈ [-1, 1]、`energy` / `confidence` ∈ [0, 1] 用 `Field(ge=, le=)`。
- `WorldInference.hypotheses` 至少 1 条、至多 3 条；`action_windows` 至多 3 条。
  `signals_used` 是字符串列表（`"time"` / `"presence"` / `"history"` / `"tasks"` / `"agents"`）。
- `WorldInput.presence` 为 `None` 表示采集关闭，不是空对象。

**验收测试**

- 情绪数值越界（`valence=1.2`、`confidence=-0.1`）报错。
- `WorldInference` 超 3 条假设报错；空假设报错。
- `PresenceSnapshot` / `WorldInput` / `WorldInference` JSON round-trip。
- `AvailableReason` 非法值报错。

**提交：** `feat(cosmos): 契约模型（presence / world）`

---

### Task 5: 契约模型 · dispatch / terminal / runtime / control

**Files:**
- Create: `agent/cosmos/contracts/dispatch.py`
- Create: `agent/cosmos/contracts/terminal.py`
- Create: `agent/cosmos/contracts/runtime.py`
- Create: `agent/cosmos/contracts/control.py`
- Modify: `agent/cosmos/contracts/__init__.py`（统一导出，`__all__` 显式列全）
- Test: `tests/cosmos/test_contracts_dispatch_terminal.py`

**接口（字段以设计 05 / 06 / 09 章为准，补充字段已标注）**

```python
# dispatch.py
class DispatchIntent(BaseModel): ...
class PlannedTask(BaseModel): ...
class DispatchPlan(BaseModel): ...
class TaskHandle(BaseModel): ...
class RejectedTask(BaseModel):
    agent_id: str; reason: str
class DispatchHandle(BaseModel): ...
class DispatchOutcome(BaseModel): ...

# terminal.py
type MessageKind = Literal["chat", "task", "query", "mixed", "progress", "result", "system"]
type LinkState = Literal["up", "unstable", "down"]
class Attachment(BaseModel):            # 文档未给字段，补齐
    kind: Literal["image", "file", "audio"]; name: str; uri: str
    mime: str | None = None; size: int | None = None
class MessageMeta(BaseModel): ...
class TerminalMessage(BaseModel): ...
class IntentResult(BaseModel): ...
class ConversationTurn(BaseModel):     # 补齐
    message_id: str; role: Literal["user", "aliya"]; text: str; at: datetime
class ConversationContext(BaseModel): ...
class TaskBrief(BaseModel): ...
class InboundEnvelope(BaseModel):
    message: TerminalMessage; intent: IntentResult
    conversation: ConversationContext; presence: ReadablePresence | None
    tasks: list[TaskBrief]; dispatch_plan: DispatchPlan | None = None
    direct_answer: bool = False         # cosmos 已直接回答查询时为 True
class ProactiveReceipt(BaseModel): ...
class CosmosEvent(BaseModel): ...

# runtime.py（文档未给模型，补齐）
class AgentRuntimeState(BaseModel):
    agent_id: str; status: AgentStatus; running: int = 0; queued: int = 0
    current_task_id: str | None = None; last_heartbeat_at: datetime | None = None
    last_error: AgentErrorInfo | None = None; error_streak: int = 0
    last_used_at: datetime | None = None
class AgentProgress(BaseModel):
    task_id: str; agent_id: str; label: str
    progress: float | None = None; step: str | None = None; message: str | None = None
    at: datetime
class AgentAbnormal(BaseModel):
    agent_id: str; reason: Literal["timeout", "stalled", "error", "internal"]
    error: AgentErrorInfo | None = None; at: datetime
class DispatchFinished(BaseModel):
    outcome: DispatchOutcome

# control.py
class CosmosCapabilities(BaseModel):
    api_version: str; contract_version: str
    event_topics: list[str]; features: list[str]; limits: dict[str, int]
```

**实现要点**

- `DispatchPlan.decision` 用 `Literal["reply_only", "single", "parallel", "clarify", "rejected"]`。
- `PlannedTask.depends_on` 是 0 基序号；`DispatchPlan` 加 `model_validator` 校验：
  序号不越界、无自依赖、无环（DFS）。
- `TerminalMessage.direction` 用 `Literal["user_to_aliya", "aliya_to_user"]`。
- `CosmosEvent.payload` 是 `dict[str, object]`；事件具体载荷由发布方序列化（`model_dump(mode="json")`）。
- `ProactiveReceipt.reason` 用 `Literal["ok", "too_frequent", "daily_limit", "quiet_hours", "user_active"] | None`。
- `TaskBrief.status` 用 `AgentStatus`；`agent_name` 是展示名，不是 id。

**验收测试**

- `DispatchPlan` 依赖环（0→1、1→0）报错；`depends_on=[5]` 越界报错。
- `TerminalMessage` 缺 `session_id` 报错；`MessageMeta.proactive=True` 默认 `False`。
- `InboundEnvelope.dispatch_plan` 可为 `None`。
- 全量 JSON round-trip：`InboundEnvelope` / `DispatchPlan` / `CosmosEvent` / `AgentRuntimeState`。

**提交：** `feat(cosmos): 契约模型（dispatch / terminal / runtime / control）`

---

### Task 6: 传输端口与进程内总线（`BusService`）

**Files:**
- Create: `agent/cosmos/transport/__init__.py`
- Create: `agent/cosmos/transport/ports.py`
- Create: `agent/cosmos/transport/memory.py`
- Create: `agent/cosmos/transport/bus_service.py`
- Create: `agent/cosmos/transport/channel.py`
- Test: `tests/cosmos/test_transport.py`

**接口**

```python
# ports.py
class MessageBusPort(Protocol):
    async def publish(self, endpoint: str, envelope: InboundEnvelope) -> None: ...
    def subscribe(self, endpoint: str) -> AsyncIterator[InboundEnvelope]: ...

class EventBusPort(Protocol):
    async def publish(self, topic: str, payload: BaseModel) -> CosmosEvent: ...
    def subscribe(self, topics: Sequence[str], *, since_sequence: int | None = None) -> AsyncIterator[CosmosEvent]: ...

class UserChannelPort(Protocol):
    async def send(self, message: TerminalMessage) -> None: ...

class WorldReasoningPort(Protocol):
    async def infer(self, payload: WorldInput) -> WorldInference: ...

# memory.py
class MemoryMessageBus: ...     # 每端点一个 asyncio.Queue(maxsize=1000)；无订阅者时丢最旧并 WARNING
class MemoryEventBus: ...       # 每主题 sequence、全局 event_id、环形缓冲 1000、慢消费告警

# bus_service.py
class BusService(Service):
    name = "bus"
    def __init__(self, config: AgentSettings) -> None: ...
    @property
    def messages(self) -> MessageBusPort: ...
    @property
    def events(self) -> EventBusPort: ...

# channel.py
class MemoryUserChannel:
    def __init__(self, limit: int = 1000) -> None: ...
    async def send(self, message: TerminalMessage) -> None: ...
    def drain(self) -> list[TerminalMessage]: ...
```

**实现要点**

- `MemoryEventBus` 的 `sequence` **按主题**递增（设计 09 章 7 节），`event_id` 全局唯一；
  `since_sequence` 从该主题环形缓冲里补发，最多 100 条，超出时抛
  `MessageDeliveryError(code="gap_too_large")`，由 `ControlService` 改走全量快照。
- 慢消费：消费者缓冲 > 1000 时丢最旧并 `log.warning("消费者过慢，已丢弃最旧事件", 主题=…)`，
  同时发布一条 `slow_consumer` 到该主题（设计 09 章 7 节）。
- `MemoryMessageBus.subscribe()` 返回 async generator；多个订阅者各自独立队列（广播语义）。
- `BusService.start()` 只做初始化，不建连接；`stop()` 关闭所有队列并唤醒等待者（幂等）。
- 不使用 `asyncio.Queue` 的 `get_nowait` 轮询；用 `await queue.get()`，关闭时放入哨兵。

**验收测试**

- 单端点发布/订阅顺序一致；两个订阅者都收到同一条。
- 事件 `sequence` 同主题递增、跨主题独立；`since_sequence` 补发缺口。
- 缓冲溢出时最旧被丢弃且有 WARNING。
- `BusService` 重复 `start` / `stop` 不报错。
- `MemoryUserChannel.drain()` 取走后清空。

**提交：** `feat(cosmos): 进程内消息总线与传输端口`

---

# M2 注册与执行

### Task 7: `AgentRegistryService`

**Files:**
- Create: `agent/cosmos/services/__init__.py`
- Create: `agent/cosmos/services/registry.py`
- Create: `agent/cosmos/registry/__init__.py`
- Create: `agent/cosmos/registry/bootstrap.py`
- Test: `tests/cosmos/test_registry.py`

**接口**

```python
class AgentRegistryService(Service):
    name = "cosmos.registry"
    def __init__(self, config: AgentSettings) -> None: ...

    def register(self, card: AgentCard, handler: AgentHandler, *, source: str = "config") -> None
    def unregister(self, agent_id: str) -> None
    def enable(self, agent_id: str) -> None
    def disable(self, agent_id: str) -> None
    def get(self, agent_id: str) -> AgentCard
    def handler(self, agent_id: str) -> AgentHandler
    def list(self) -> list[AgentCard]                 # 桌面顺序
    def enabled(self, agent_id: str) -> bool
    def match(self, text: str, top_k: int = 5) -> list[AgentMatch]
```

**实现要点**

- 注册期校验（设计 02 章 5 节）：id 格式、重复、capabilities 空或非法、`params_schema.type == "object"`、
  `timeout_seconds > 0`、handler 可调用且是协程函数；失败抛 `AgentRegistrationError`。
- `summary` 超 20 字截断 + WARNING；`desktop.group` 未在 `config.cosmos.desktop.groups` 声明时落
  `general` + WARNING；`name` 重名（id 不同）WARNING。
- `match()` 纯检索，不调模型：`capabilities` 精确命中 0.9、`summary` 关键词 0.6、
  `description` 关键词 0.4、`examples` 字符重叠 0.3；同分按 `id`。返回 `AgentMatch(agent_id, score, reason)`。
- `AgentMatch` 已在 Task 3 的 `contracts/agent.py` 定义，本任务只消费。
- `bootstrap.py`：
  ```python
  def load_agents(registry: AgentRegistryService, config: AgentCosmosSettings) -> None
  ```
  按配置 `module` + `handler` 用 `importlib` 动态加载；导入失败或不是 async 函数 → `AgentRegistrationError`。
- 运行期注册由 `config.cosmos.allow_runtime_registration` 控制；Aliya 无此入口（只在 Control 层挡）。
- 日志：注册成功 INFO（`agent_id` / `capabilities` / `来源`）；失败 ERROR；注销 WARNING。

**验收测试**

- 合法卡片注册成功，`get` / `list` / `enabled` 正确。
- 重名、非法 id、空 capabilities、非 object schema、`timeout_seconds=0`、同步 handler 各一例报错。
- `summary` 超长被截断，原文不进日志。
- `match("改一下这个函数")` 命中 `code_edit`，reason 含 `capabilities`。
- `disable` 后 `enabled=False`，`get` 仍可查（桌面置灰语义）；`unregister` 后 `get` 抛 `AgentNotFoundError`。
- `load_agents` 从配置加载成功；模块不存在时报错带模块路径。

**提交：** `feat(cosmos): 子 agent 注册表与配置加载`

---

### Task 8: `AgentRuntimeService`

**Files:**
- Create: `agent/cosmos/services/runtime.py`
- Test: `tests/cosmos/test_runtime.py`

**接口**

```python
class AgentRuntimeService(Service):
    name = "cosmos.runtime"
    dependencies = (AgentRegistryService, BusService, ClockService)

    def __init__(self, registry: AgentRegistryService, bus: BusService,
                 clock: ClockService, config: AgentSettings) -> None: ...

    async def submit(self, agent_id: str, params: dict[str, object], *, label: str,
                     session_id: str, request_id: str | None = None,
                     deadline_at: datetime | None = None) -> TaskHandle
    async def submit_many(self, tasks: Sequence[PlannedTask], *, session_id: str,
                          dispatch_id: str) -> list[TaskHandle]
    async def cancel(self, task_id: str) -> None
    async def retry(self, task_id: str) -> TaskHandle
    async def list_tasks(self, agent_id: str | None = None, limit: int = 20) -> list[TaskBrief]
    def state(self, agent_id: str) -> AgentRuntimeState
    def stats(self, agent_id: str) -> RuntimeStats          # p50_seconds / error_streak
```

**实现要点**

- 每个 agent 一个 `_AgentSlot`：`running_sem: asyncio.Semaphore(max_concurrent)`、
  `queue: asyncio.Queue`（`queue=true` 时）、`error_streak`、`durations: deque[float]`。
- 提交路径：注册表校验 → 幂等（`request_id` 命中则返回缓存 `TaskHandle`，不重跑）→
  并发未满直接启动，满了且 `queue=true` 入队，否则 `AgentBusyError`。
- 执行：构造 `AgentRequest`，`asyncio.create_task` 跑 handler，用 `_TaskContext` 实现
  `report` / `heartbeat` / `emit` / `child_scope`：
  - `report` 1s 节流（首条立即发）、`progress` 单调不减（倒退丢弃 + DEBUG）；
  - 每次上报刷新 `last_heartbeat_at`；`heartbeat()` 只刷新时间；
  - `emit` 累积到 `AgentResult.artifacts`（同一 artifact 不重复）；
  - `child_scope` 用 `log.context(子步骤=name)`。
- 超时：`execution.timeout_seconds` 到点先置 `ctx.cancelled=True` 再 `task.cancel()`，
  等 5s 宽限；未退出则 `AgentResult.failed(code="timeout", retriable=True)` 并推
  `agent.abnormal(reason="timeout")`。
- 心跳巡检：后台 1s tick，比较 `now - last_heartbeat_at > heartbeat_seconds` → `stalled`
  并推 `agent.abnormal(reason="stalled")`；恢复上报后回到 `running`。
- 幂等缓存：`request_id → (TaskHandle, AgentResult)`，保留 10 分钟。
- `submit_many`：按 `depends_on` 拓扑分层，同层并发、层间串行；失败任务有下游则下游标
  `skipped`，整批结果交 `DispatchService` 聚合。`DispatchFinished` 由 Dispatch 发，不由 Runtime 发。
- 事件：每次状态变化发 `agent.state.changed`；进度发 `agent.progress`；终态额外发
  `agent.result`；异常发 `agent.abnormal`。事件载荷用 `model_dump(mode="json")`。
- `stop()`：取消所有运行任务与巡检任务，等待它们退出，幂等。
- 日志只记 `agent_id` / `task_id` / 耗时 / 成败 / 错误类型，不记 `params` 与结果正文。

**验收测试**

- 正常执行：`submit` 返回 `TaskHandle`，收到 `agent.progress` 与 `agent.result`，状态回 `idle`。
- `max_concurrent=1` + `queue=false`：第二个提交抛 `AgentBusyError`。
- `queue=true`：第二个进队列，第一个完成后自动启动。
- 幂等：同一 `request_id` 提交两次只执行一次，返回同一 `task_id`。
- 取消：`cancel` 后 handler 收到 `cancelled=True` 与 `CancelledError`，结果 `cancelled`。
- 超时：handler `sleep` 超过 `timeout_seconds`，5s 宽限后判 `failed(timeout)`；
  测试把 `timeout_seconds` 设为 0.05 并把宽限设为可配置常量以缩短用例。
- 心跳缺失：handler 长时间不报，超过 `heartbeat_seconds` 判 `stalled` 并推 `agent.abnormal`。
- `submit_many` 依赖：任务 1 等任务 0；任务 0 失败时任务 1 为 `skipped`。
- `stop()` 后无残留任务（用 `asyncio.all_tasks()` 粗查）。

**提交：** `feat(cosmos): 任务运行时（并发 · 超时 · 取消 · 幂等 · 心跳）`

---

# M3 呈现与感知

### Task 9: `DesktopService`

**Files:**
- Create: `agent/cosmos/services/desktop.py`
- Test: `tests/cosmos/test_desktop.py`

**接口**

```python
class DesktopService(Service):
    name = "cosmos.desktop"
    dependencies = (AgentRegistryService, AgentRuntimeService, BusService, ClockService)

    async def describe(self) -> DesktopSnapshot
    async def get(self, agent_id: str) -> AgentDetail
    async def patch_layout(self, patch: DesktopLayoutPatch) -> DesktopSnapshot
    def readable(self, agent_id: str) -> ReadableState      # 懒派生
```

**实现要点**

- 快照：注册表 `list()` → 按 `pinned` / `order` / `id` 排序 → 分组 → 每条拼
  `status` / `task` / `readable` / `badge`；`revision` 从 `data/state/desktop.json` 读，任何布局变更 +1。
- `_readable()` 严格按设计 03 章 4.1 的优先级表实现，文案集中在常量表里，不散落。
  `duration_ratio` 取 `runtime.stats(agent_id).p50_seconds`，首次为 `None`，不显示快慢结论。
- 事件驱动刷新：订阅 `agent.state.changed` / `agent.progress` / `agent.result`，
  进度类按 `progress_throttle_ms` 做尾沿节流（保留最后一条），终态立即刷新。
- 全量对账：`reconcile_seconds` 定时器，只发差异；`agent.state.changed` 载荷为
  `AgentRuntimeState`，`desktop.layout.changed` 载荷为 `DesktopSnapshot`。
- 布局持久化：读 `contract.state_dir/desktop.json`；写用「临时文件 + `os.replace`」原子写；
  文件缺失或损坏 → 空布局 + WARNING，不阻断启动。
- `patch_layout` 幂等：同一补丁重复应用后 `revision` 不再增长（内容不变时不 +1）。
  不允许改 `visible`（不在 patch 模型里，天然满足）。
- 停止时取消订阅与定时器，幂等。

**验收测试**

- 空注册表：快照 `entries=[]`，`summary` 非空（「还没有可用的工具」）。
- 排序：pinned 优先，其次 order，最后 id；运行中不置顶。
- `readable` 覆盖设计表 12 条规则各一个用例（用假的 runtime 状态驱动）。
- `duration_ratio` 无基线时为 `None`，有基线且 > 2 时 summary 含「比平时慢」。
- 进度事件 5 次连发只产生 1 条节流后的 `desktop.layout.changed`（用 bus 订阅计数）。
- 布局写盘后可重新读出；损坏文件不崩。
- `patch_layout(pin=...)` 两次，`revision` 只 +1。

**提交：** `feat(cosmos): agent 桌面（快照 · 可解读状态 · 节流 · 布局持久化）`

---

### Task 10: `PresenceService`

**Files:**
- Create: `agent/cosmos/services/presence.py`
- Test: `tests/cosmos/test_presence.py`

**接口**

```python
class PresenceService(Service):
    name = "cosmos.presence"
    dependencies = (ClockService, DecisionService, LLMService, BusService)

    def __init__(self, clock: ClockService, decision: DecisionService,
                 llm: LLMService, bus: BusService, config: AgentSettings) -> None: ...

    async def touch(self, *, session_id: str | None = None) -> None
    async def apply(self, signal: PresenceInput) -> PresenceSnapshot
    async def set_visibility(self, fields: Sequence[PresenceField]) -> None
    def snapshot(self) -> PresenceSnapshot
```

**实现要点**

- 六类字段按设计 04 章 2 节建模；每类一个采集开关，关闭时 `availability.reason="disabled_by_config"`。
  通道不支持的字段（typing / device）`reason="channel_not_supported"`。
- `online` 判定纯时间差：`idle_after_seconds` / `offline_after_seconds`；`busy` 只由
  `PresenceInput.busy=True` 显式设置，不推断。
- `readable` 严格按设计 04 章 3.1 的优先级表；缺历史数据时删掉「这个点你通常在线」分支。
  文案统一用「你」，配置 `privacy.address_as` 只允许「你」，写别的值在加载期 WARNING 并回落「你」。
- 情绪：`provider=decision` 走 `DecisionService.score()`（`valence` / `energy` 两题），
  `provider=llm` 走 `chat_structured`；只在收到用户消息后更新，`evidence_count` 记窗口条数；
  `confidence < min_confidence` → `source="none"` 且 `available=false`。
  `explicit` 情绪（用户明说）优先级最高，由 `apply()` 的扩展字段或 Terminal 输入提供；
  本计划先留 `set_explicit_mood(valence, energy)` 接口，不在 Terminal 自动推断。
- 去抖：字段级 `last_push_at` + 最小间隔（online 30s、typing 3s、mood 5min、env.period 1min、
  device 30s），合并期内保留最后一次，终态必推。
- 推送 `presence.changed` 带全量 `snapshot`（设计 04 章 4.2）。
- 隐私：`retain_raw_hours` 环形缓冲（`deque(maxlen=…)` 按时间裁剪）；情绪不写日志、不落盘。
- 关闭 `presence.enabled` 时 `snapshot()` 返回 `unknown` 快照，不抛错。

**验收测试**

- 六类字段默认可用性；逐项关开关后 `available=false` 且 `reason` 正确。
- `online` 三态按时间推进（用可注入的固定时钟推进）正确。
- `readable` 覆盖设计表 11 条规则（至少 8 条，覆盖 online/away/offline/unknown/typing/awaiting）。
- `typing` 3 次抖动在 3s 内只推 1 次，且最终为 `False`。
- 情绪低置信度时 `available=false`、`source="none"`、`readable` 不含情绪描述。
- `enabled=false` 时 `snapshot().readable.summary == "用户状态未知"`，且不抛错。
- 日志中不含情绪数值（捕获 sink 断言）。

**提交：** `feat(cosmos): 用户状态线索（采集 · 派生 · 去抖 · 隐私）`

---

# M4 对话与调度

### Task 11: `ConversationService`

**Files:**
- Create: `agent/cosmos/services/conversation.py`
- Test: `tests/cosmos/test_conversation.py`

**接口**

```python
class ConversationService(Service):
    name = "cosmos.conversation"
    dependencies = (LLMService, ClockService)

    async def classify(self, text: str) -> IntentResult
    async def build_context(self, session_id: str) -> ConversationContext
    async def record_user(self, message: TerminalMessage) -> None
    async def record_aliya(self, message: TerminalMessage) -> None
    async def format(self, text: str, *, kind: MessageKind) -> list[str]
    async def redact(self, text: str) -> RedactionResult
```

**实现要点**

- 一级规则分类（不调模型）：
  - 任务动词表（改 / 写 / 加 / 删 / 跑 / 修 / 重命名 / 生成 / 更新…）+ 目标特征（路径、代码块、
    文件名）→ `task`，置信度 0.9；
  - 问号 + 状态词（进度 / 还在忙 / 到哪了 / 完成了吗）→ `query` + `query_scope`；
  - 同时命中查询与任务 → `mixed`，`task_text` 抽出任务部分；
  - 其余 → `chat`，0.8。
- 规则置信度 < `intent.rule_confidence` 且 `model_fallback=true` 时调
  `LLMService.chat_structured` 输出 `IntentResult`；模型超时/非法 → 用规则结果，无规则结论则
  `chat` + 0.3（设计 06 章 8 节）。
- 上下文：内存字典 `session_id → deque[ConversationTurn]`，上限 `conversation_turns`；
  超出部分压成 `summary`（先调 LLM，失败则截断到 `summary_chars` + WARNING）；
  `unanswered_count` 由「最后一条 user 之后没有 aliya」计数；`progress` / `result` 不入 turns。
- `format()`：按段落分段，单段超 `max_content_chars` 再按句子切；返回 `list[str]`，
  不做任何标点规范化、不合并短句、不加前缀。
- `redact()`：正则表命中 `tsk_` / `dsp_` / `agent_id` / `Traceback` / 已知工程词（用户 / 玩家 /
  agent / 模型 / 调度 / 面板）；命中只记规则名与位置，不记原文；**不因命中而改写正文**，
  由调用方决定（设计 06 章 6.3 的「脱敏失败不阻断发送」语义）。
- 日志不记消息正文。

**验收测试**

- 规则分类 12 条样例：「在吗」→ chat；「今天有点累」→ chat；「改一下 core/x.py」→ task；
  「任务跑到哪了」→ query + task_status；「改完顺便跑测试」→ mixed。
- 模型回退：假 LLM 返回 task，规则不确定时采用；假 LLM 抛超时 → 回退规则。
- 上下文：第 21 轮后 `summary` 非空、`turns` 长度不超上限；压缩失败时截断 + WARNING。
- `format()` 对 3000 字文本分成 2 段；对「嗯。」「在啊。」保持原样不合并。
- `redact()` 命中 `tsk_01H` 与 `Traceback`，返回规则名，正文不变。
- `unanswered_count`：连发 3 条未回复为 3，回复后归 0。

**提交：** `feat(cosmos): 对话服务（意图分类 · 上下文 · 分段 · 脱敏）`

---

### Task 12: `DispatchService`

**Files:**
- Create: `agent/cosmos/services/dispatch.py`
- Test: `tests/cosmos/test_dispatch.py`

**接口**

```python
class DispatchService(Service):
    name = "cosmos.dispatch"
    dependencies = (AgentRegistryService, AgentRuntimeService, ConversationService,
                    BusService, ClockService)

    async def plan(self, intent: DispatchIntent) -> DispatchPlan
    async def execute(self, plan: DispatchPlan) -> DispatchHandle
    async def dispatch(self, intent: DispatchIntent) -> DispatchHandle
    async def cancel(self, dispatch_id: str) -> None
    async def outcome(self, dispatch_id: str) -> DispatchOutcome
```

**实现要点**

- 显式入口（`source="desktop"` / `agent_id` 给定）：不语义匹配，只做存在性 / 禁用 / 参数顶层
  结构 / 并发检查；`params` 必须能通过 `params_schema` 的 `type=object` 与 `required` 检查（不实现
  完整 JSON Schema 校验，字段级校验留给子 agent）。
- 自然语言入口：`registry.match()` 粗筛 → 单候选且 `score >= high_threshold` 直接 `single`；
  多候选调 `LLMService.chat_structured(MatchResult)` 精排；模型超时 / 非法 JSON → 回退检索结果
  （唯一高分候选 → single，否则 clarify），并在 `rationale` 标注「匹配服务超时」。
- 拆解：只在第一级命中 ≥ 2 个候选时做一次结构化调用，输出 `DispatchPlan`；`params.paths`
  交集非空的两个任务改串行（不并行提交，用 `depends_on` 连接）。
- `DispatchPlan.confidence < min_confidence` → `decision="clarify"`（`suggest` 模式）；
  `mode="auto"` 且 `confidence >= auto_threshold` 且 `decision in {single, parallel}` →
  直接 `execute()`，但仍推 `agent.submitted`。
- 幂等：`sha256(session_id + agent_id + 规范化 params)` 作 key，60s 窗口命中返回已有
  `task_id`，事件标 `deduplicated=true`。
- 执行：`runtime.submit_many()`；汇总各任务结果为 `DispatchOutcome`（`ok` / `partial` /
  `failed` / `cancelled`），`summary` 只写「N 个任务完成，M 个失败」，**不合并内容结论**。
- 发 `dispatch.finished` 事件（`DispatchFinished`）；`agent.submitted` 事件载荷用
  `TaskHandle`。
- `plan()` 加 30s 结果缓存（同输入同结果）；`cancel()` 取消该批次未完成任务。

**验收测试**

- 桌面入口：目标存在 → `single`，`rationale` 不含 id 以外的工程细节；目标不存在 → `AgentNotFoundError`。
- 自然语言单候选高分 → `single` 且不调 LLM（用假 LLM 断言 0 次调用）。
- 多候选 → 假 LLM 返回 `parallel` + 2 任务，`depends_on` 正确。
- 假 LLM 超时 → 回退，`rationale` 含「超时」，不抛错。
- 低置信度 → `clarify` + `missing` 非空。
- 参数缺必填 → `clarify`。
- 幂等：同输入 2 次 `execute`，第二次返回同一 `task_id`，不重复执行。
- 并行改同一文件 → 自动串行（任务 1 `depends_on=[0]`）。
- 部分失败：1 成功 1 失败 → `outcome.status="partial"`。
- `auto` 模式高置信度直接提交；`clarify` 永不自动提交。

**提交：** `feat(cosmos): 调度服务（检索精排 · 拆解 · 并行 · 幂等）`

---

# M5 世界推理与终端

### Task 13: `WorldBridgeService`

**Files:**
- Create: `agent/cosmos/services/world.py`
- Test: `tests/cosmos/test_world.py`

**接口**

```python
class WorldBridgeService(Service):
    name = "cosmos.world"
    dependencies = (ClockService, PresenceService, AgentRuntimeService,
                    AgentRegistryService, DesktopService, BusService)

    def set_port(self, port: WorldReasoningPort) -> None
    async def refresh(self, *, force: bool = False) -> WorldInference | None
    def view(self) -> WorldInference | None
    async def set_aliya_context(self, ctx: AliyaContext | None) -> None
```

**实现要点**

- `start()`：`enabled=false` → 不注册定时器 / 订阅，直接返回；`enabled=true` 且无 port →
  按 `world.port_module` + `port_factory` 动态加载，仍无则抛 `WorldBridgeError`。
- 组装 `WorldInput`：`time` 取 clock；`link` 从 `TerminalService.link_state()` 取（为避免
  循环依赖，用 `set_link_provider(callable)` 注入，`TerminalService.start()` 时注册）；
  `presence` 取 `PresenceService.snapshot()` 的扁平化信号（采集关闭时为 `None`）；
  `tasks` / `agents` 取 Runtime 与 Registry；`history` 从 Presence 的聚合指标构造。
- 触发：定时 tick、`presence.changed`（online 变化）、`dispatch.finished`、链路恢复、
  静默阈值跨越；**用户消息不触发**。合并窗口 `min_interval_seconds`，推演中再来触发标
  `dirty`，结束后补一次。
- 超时 `infer_timeout_seconds`：`asyncio.timeout` 取消本次；失败保留旧结果并 `stale=True`；
  连续 3 次失败暂停 `failure_backoff_seconds`。
- 校验返回：非 `WorldInference`、`hypotheses` 为空、文本超长 → 丢弃并 WARNING（只记字段路径）。
- 缓存 `ttl_seconds`；`view()` 过期仍返回旧结果并标 `stale=True`（旧的参考比没有好）。
- 发 `world.inference`（`WorldInferenceEvent`），`age_seconds` / `trigger` 由 cosmos 填。
- `stop()` 取消定时器与订阅，幂等。

**验收测试**

- `enabled=false`：`view()` 恒 `None`，端口不被调用。
- `enabled=true` 无 port → `start()` 抛 `WorldBridgeError`。
- 注入假端口：收到 `WorldInput`，字段齐全；`presence` 关闭时为 `None`。
- 超时：假端口 sleep 超时 → `view()` 返回旧值且 `stale=True`，不抛错。
- 连续 3 次失败后暂停：第 4 次触发不调端口。
- 合并窗口：60s 内 5 次触发只调 1 次端口。
- 返回非法结构（hypotheses 空）→ 丢弃 + WARNING，`view()` 保持旧值。

**提交：** `feat(cosmos): 世界推理桥（组装 · 节流 · 超时 · 缓存）`

---

### Task 14: `TerminalService`

**Files:**
- Create: `agent/cosmos/services/terminal.py`
- Test: `tests/cosmos/test_terminal.py`

**接口**

```python
class TerminalService(Service):
    name = "cosmos.terminal"
    dependencies = (ConversationService, DispatchService, PresenceService,
                    BusService, ClockService)

    def set_user_channel(self, channel: UserChannelPort) -> None
    async def ingest(self, message: TerminalMessage) -> InboundEnvelope
    async def deliver(self, text: str, *, session_id: str,
                      kind: MessageKind = "chat") -> None
    async def notify(self, text: str, *, dispatch_id: str | None = None) -> ProactiveReceipt
    async def send_proactive(self, text: str) -> ProactiveReceipt
    async def history(self, session_id: str, limit: int = 20) -> list[TerminalMessage]
    async def set_link_state(self, state: LinkState) -> None
    def link_state(self) -> LinkState
```

**实现要点**

- `ingest()`：生成 `message_id` / `at`（clock）→ `PresenceService.touch()` →
  `ConversationService.classify()` → `build_context()` → `PresenceService.snapshot().readable` →
  `task` / `mixed` 时调 `DispatchService.plan()` 附 `dispatch_plan` → 组装 `InboundEnvelope` →
  `bus.messages.publish("aliya", envelope)` 并返回。
- `query` 且 `query_scope in {agent_status, task_status}`：由 `TerminalService` 直接生成模板文案
  （「还在改，3 个文件，进度 60%，跑了 1 分 12 秒。」）并 `deliver`，**不到 Aliya**；
  返回标记 `direct_answer=True`（`InboundEnvelope` 加一个可选字段，或返回 `None`——采用前者，
  在 Task 5 的模型里预留 `direct_answer: bool = False`）。
- `deliver()`：`ConversationService.format()` 分段 → 逐条生成 `TerminalMessage` →
  `ConversationService.redact()` 扫描（只记 WARNING）→ `user_channel.send()` →
  `ConversationService.record_aliya()` → `PresenceService` 复位 `awaiting_result`（若该批次已回传）。
  `link=down` 时进待发队列（`queue_limit`），恢复后按序重放。
- `notify()`：与 `deliver` 同路径，但受免打扰（`quiet_hours` / `user typing`）约束，
  被拦时返回 `ProactiveReceipt(deliver_after=…)`，不丢弃。
- `send_proactive()`：最小间隔、每日上限、免打扰、未回应降频（连续 2 条无回应后 ×2）、
  用户 5s 内发言取消待发；被拒返回结构化回执。
- 主动消息计数用 `data/state/proactive.json` 持久化（跨重启保留每日上限），原子写。
- `set_link_state()`：状态变化发 `link.state.changed`；`down` 时不发主动消息；
  恢复时若 `replay_on_recover=true` 重放双向队列。
- `history()` 只返回对话消息（不含 progress / result）。
- 日志：收发记 `message_id` / `session_id` / `kind` / 字数 / 分段数，**不记正文**。

**验收测试**

- `ingest` 纯聊天：返回信封，`dispatch_plan=None`，bus 上收到一条发往 `aliya` 的消息。
- `ingest` 任务：附 `DispatchPlan`；`query` + `task_status` 直接答复且不 publish。
- `deliver` 三条短句：channel 收到 3 条，标点原样。
- 超长文本：分段发送，`part` 信息在 `meta` 里（`MessageMeta` 加 `part_index` / `part_total`）。
- `send_proactive`：间隔内被拒 `too_frequent`；超每日上限 `daily_limit`；输入中 `user_active`。
- 用户 5s 内发言：待发主动消息被取消。
- `link=down`：`deliver` 进队列、不调 channel；恢复后按序重放且只发一次。
- `history` 不含 `progress` / `result`。

**提交：** `feat(cosmos): 通信终端（收发 · 路由 · 链路 · 主动消息节流）`

---

### Task 15: `ControlService`

**Files:**
- Create: `agent/cosmos/services/control.py`
- Test: `tests/cosmos/test_control.py`

**接口（设计 09 章 1 节全量）**

```python
class ControlService(Service):
    name = "cosmos.control"
    dependencies = (TerminalService, DesktopService, DispatchService, PresenceService,
                    WorldBridgeService, AgentRuntimeService, BusService, ClockService)

    async def handshake(self) -> CosmosCapabilities
    def inbound(self) -> AsyncIterator[InboundEnvelope]
    def subscribe(self, topics: Sequence[str], *,
                  since_sequence: int | None = None) -> AsyncIterator[CosmosEvent]
    async def describe_desktop(self) -> DesktopSnapshot
    async def get_agent(self, agent_id: str) -> AgentDetail
    async def patch_layout(self, patch: DesktopLayoutPatch) -> DesktopSnapshot
    # 调度、状态、消息：直接转发到对应服务，本层不实现业务
```

**实现要点**

- `handshake()`：`api_version` / `contract_version` 取 `config.cosmos.contract`；
  `event_topics` 是固定白名单（10 个，见设计 09 章 7 节）；`features` 按配置启用情况动态生成
  （`world` / `presence.mood` / `dispatch.auto`）；`limits` 含 `max_parallel` /
  `max_content_chars` / `history_limit`。
- 契约版本校验：`contract_version` 主版本不一致 → `ContractVersionError`；`handshake` 只返回不校验
  调用方（校验在 Aliya 侧，本层提供字段）。
- `inbound()`：`bus.messages.subscribe("aliya")`。
- `subscribe()`：先校验 topic 白名单，非法 topic 抛 `ValueError`（或 `DispatchError`）；
  订阅 `presence.changed` 时按 `set_presence_visibility` 过滤掉被隐藏字段（仍推快照，
  但 `readable` 不提该项——过滤在 `PresenceService` 完成，本层只转发）。
- 权限：Aliya 无 `set_agent_enabled` / `register` / `unregister` / 改采集开关 / 改世界配置；
  因此这些方法**不暴露**在 Control 上。`CosmosPermissionError` 由未来管理面使用。
- 所有转发方法套统一超时（设计 09 章 10 节），超时抛 `CosmosError(code="timeout", retriable=True)`。
  超时值是模块常量表，测试可 monkeypatch。
- `describe_desktop` / `get_agent` / `list_tasks` 是同步内存读取，不阻塞。

**验收测试**

- `handshake` 返回 10 个 topic，features 随 `world.enabled` / `dispatch.mode` 变化。
- `inbound()` 能收到 `TerminalService.ingest()` 发布的消息。
- `subscribe(["presence.changed"])` 能收到事件；`subscribe(["nope"])` 报错。
- `describe_desktop` / `patch_layout` / `get_agent` 转发正确（用假下游断言调用）。
- `deliver` / `notify` / `send_proactive` 转发到 Terminal。
- 契约版本主版本不一致时 `ContractVersionError`。
- 所有方法在超时后抛 `CosmosError`，`retriable=True`。

**提交：** `feat(cosmos): Aliya 控制面（协商 · 订阅 · 转发 · 权限边界）`

---

# M6 装配与验收

### Task 16: 装配入口与配置接线

**Files:**
- Create: `agent/cosmos/assembly.py`
- Create: `agent/cosmos/app.py`
- Modify: `agent/cosmos/__init__.py`（导出 `build_cosmos_manager`、`CosmosError`、契约常用类型）
- Modify: `core/main.py`（`_run` → 公开 `run(manager)`，`main()` 内部调用）
- Test: `tests/cosmos/test_assembly.py`

**接口**

```python
# assembly.py
def build_cosmos_manager(settings: Settings | None = None) -> ServiceManager

# app.py
def main() -> None
```

**实现要点**

- `build_cosmos_manager()`：`build_manager(settings)` 后 `register(BusService, AgentRegistryService,
  AgentRuntimeService, DesktopService, PresenceService, ConversationService, DispatchService,
  WorldBridgeService, TerminalService, ControlService)`。
- 启动顺序交给容器分层，不手工排序；`BusService` 必须最先注册（同层无依赖，注册顺序即启动顺序）。
- `app.py`：
  ```python
  settings = get_settings()
  _ = setup_logging(settings.log)
  log.info("cosmos 启动中", 应用=settings.app.app_name, 配置源=settings.config_source)
  try:
      asyncio.run(run(build_cosmos_manager(settings)))
  except Exception as exc:
      log.exception("cosmos 启动失败，进程退出", 错误类型=type(exc).__name__, 错误消息=str(exc))
      raise SystemExit(1) from exc
  ```
- `AgentRegistryService.start()` 调 `load_agents()`；`TerminalService.start()` 设置默认
  `MemoryUserChannel` 并把 `link_provider` 注册给 `WorldBridgeService`；`WorldBridgeService.start()`
  读取 port。
- `WorldBridgeService` 与 `TerminalService` 之间存在「链路状态」的反向读取：用
  `WorldBridgeService.set_link_provider(TerminalService.link_state)` + `TerminalService.set_link_observer(...)`
  解耦，避免构造器循环依赖。
- `data/state/` 目录在 `DesktopService.start()` 中 `mkdir(parents=True, exist_ok=True)`。

**验收测试**

- `build_cosmos_manager()` 返回的 manager 有 14 个服务（core 4 + cosmos 10）。
- `manager.start_all()` 后 14 个服务 `running=True`；`stop_all()` 后全部非 RUNNING。
- 缺 `world.port_module` 且 `world.enabled=false` 时能正常启动。
- `python -m agent.cosmos.app` 启动后健康检查包含 `cosmos.control` 等标签（用 subprocess +
  超时 5s 后 SIGINT，断言退出码 0）。
- `core.main` 行为不变：`python -m core.main` 仍只起 core 4 个服务。

**提交：** `feat(cosmos): 装配入口与进程启动`

---

### Task 17: 示例 agent 与五条端到端流程

**Files:**
- Create: `agent/cosmos/registry/echo.py`
- Modify: `data/config/cosmos.yaml`（`agent.cosmos.agents[]` 指向 `echo`）
- Test: `tests/cosmos/test_flows.py`

**接口**

```python
# echo.py
CARD = AgentCard(... id="echo", name="回声台", icon="🔁",
                 summary="原样返回收到的指令", capabilities=["echo"],
                 params_schema={"type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"]})

async def handle(request: AgentRequest, ctx: AgentContext) -> AgentResult:
    await ctx.report(progress=50, step="回显")
    return AgentResult.ok(summary=f"收到：{request.params['text']}")
```

**实现要点**

- `echo` 只是联调用的最小 agent，**不是真实 Edit agent**；真实业务 agent 属于扩展（设计 11 章 1 节）。
- 端到端测试直接构造 `build_cosmos_manager(Settings())`，用 `service.get(...)` 拿实例，
  通过 `set_user_channel()` 注入 `MemoryUserChannel`，通过 `set_port()` 注入假世界推理。
- 五条流程各一个测试，全部走真实服务，只替换端口：
  1. 聊天：`ingest` → 直接从 `bus` 取信封 → 断言 `readable` 存在、`dispatch_plan=None`；
     再 `deliver`，断言 channel 收到、标点未改。
  2. 桌面点击：`control.invoke_agent("echo", {"text": "hi"}, session_id=…)` → 等 `agent.result`
     事件 → 断言 `summary` 与 `status=ok`，桌面状态回 `idle`。
  3. 自然语言任务：`control.plan(DispatchIntent(...))` → `single`（假 LLM 或单候选直命中）→
     `execute_plan` → 等 `dispatch.finished` → `ok`。
  4. 世界推理：假 port 返回固定 `WorldInference` → 等 `world.inference` →
     `send_proactive` 被接受 → channel 收到。
  5. 状态变化：`presence.apply(PresenceInput(is_typing=True))` → 等 `presence.changed` →
     断言 `readable.summary` 含「正在输入」、`hint` 非空，且未向用户发送任何消息。

**验收测试**

- 五条流程全部通过；每条都断言「用户侧消息不出现 `task_id` / `agent` / `用户` 等工程词」。
- 连发两条短句 → channel 收到 2 条。
- `link=down` 时流程 1 的 `deliver` 进队列，`up` 后重放。
- 全量测试无真实网络调用（假 LLM / 假 port）。

**提交：** `feat(cosmos): 示例 agent 与五条端到端流程`

---

### Task 18: 文档与配置同步

**Files:**
- Modify: `agent/cosmos/README.md`（状态从「实现代码待落地」改为「已实现」，补运行命令）
- Modify: `agent/cosmos/docs/01-architecture.md`（补 `BusService` 与端口注入说明）
- Modify: `agent/cosmos/docs/08-subagent-contract.md`（补 `echo` 示例与 `AgentHandler` 类型）
- Modify: `agent/cosmos/docs/09-control-api.md`（补 `AgentRuntimeState` / `AgentProgress` 字段表）
- Modify: `README.md`（补 `agent/cosmos` 目录说明与启动命令）
- Modify: `docs/agent-development.md`（若与 cosmos 装配相关，追加一节「主 agent 装配」）
- Modify: `agent/cosmos/config/agent-settings.example.yaml`（与代码字段对齐）

**实现要点**

- 文档只写实际落地内容，不复制计划里的推测；`BusService` / `port_module` / `channel` 三处新增
  必须在架构章有一句话解释「为什么端口要由一个 Service 承载」。
- `README.md` 的「常用命令」表加一行：启动 cosmos → `uv run python -m agent.cosmos.app`。
- 所有示例代码必须能复制运行（变量名与真实签名一致）。

**验收测试**

- `uv run ruff check .` 与 `uvx basedpyright agent core tests` 全绿。
- `.venv/bin/python -m pytest` 全绿。
- 通读 `agent/cosmos/docs/01` 与实现，抽查 3 个字段名一致。
- `data/config/cosmos.yaml` 中的 `agent` 段能被 `load_settings()` 解析。

**提交：** `docs(cosmos): 同步实现状态与装配说明`

---

## 3. 总体验收

```bash
uv run ruff check . && uv run ruff format --check .
uvx basedpyright agent core tests
.venv/bin/python -m pytest --cov=agent --cov=core | tail -5
uv run python -m agent.cosmos.app     # 另开终端，Ctrl-C 能优雅退出
uv run python -m core.main            # core 入口不受影响
```

通过标准：

1. 三条判据全绿，核心链路（runtime / terminal / dispatch / presence）行覆盖 ≥ 80%。
2. 启动日志里 14 个服务的健康检查全部 `健康=true`（未配 LLM / Decision 时它们 unhealthy 是
   既有的「缺密钥不 fail fast」语义，不算失败；cosmos 9 个必须 healthy）。
3. 五条流程测试通过，且用户侧文本无工程词。
4. `SIGINT` 后进程退出码 0，日志有「全部服务已停止」。

---

## 4. 风险与回退

| 风险 | 触发点 | 应对 |
| --- | --- | --- |
| 容器注入限制比预期更严 | Task 7 起构造器报 `ServiceContractError` | 全部改注入 `AgentSettings`；端口一律 setter。不改 core |
| Runtime 超时/取消用例不稳定 | Task 8 | 超时与宽限做成模块常量，测试 monkeypatch；避免依赖真实 5s |
| 事件风暴导致桌面节流难测 | Task 9 | 用假时钟 + 手动 flush；不 sleep 等真实时间去断言 |
| Presence 文案规则多、易漏 | Task 10 | 把优先级表做成常量表 + 参数化测试，逐条对账 |
| LLM 未配导致 Dispatch 测试失败 | Task 12 | 测试全部注入假 LLM；真实模型只在手工联调时使用 |
| 世界推理端口未实现导致启动失败 | Task 13 / 16 | 默认 `enabled=false`；`enabled=true` 才 fail fast |
| 用户通道未实现 | Task 14 / 17 | 默认 `MemoryUserChannel`，真实通道是部署方扩展 |

任一回退点：每个 Task 独立提交，出问题直接回退该提交，不影响已通过的任务。

---

## 5. 实施补记

> 执行过程中在这里追加：与设计文档的偏差、新增字段、被推迟的能力、测试中被跳过的路径。
> 每条写「任务号 / 偏差 / 原因 / 后续处理」。

### 1. 与计划不一致的落地点

| 任务 | 偏差 | 原因 | 后续处理 |
| --- | --- | --- | --- |
| Task 1 | `conftest.py` 的 `bus` 夹具推迟到 Task 6 | `BusService` 在 Task 6 才存在，Task 1 引入会让 `pytest` 收集失败 | 已在 Task 6 补入，最终夹具完整 |
| Task 6 | `BusService.__init__` 额外依赖 `ClockService` | D5 要求所有时间取时钟，事件 `at` 与 `event_id` 不能自己造时间 | 已写进依赖声明，容器正常注入 |
| Task 8 | `state()` 改名 `agent_state()` | `core.service.Service` 已有实例属性 `self.state`，同名方法会被覆盖 | 契约语义不变，仅方法名变化 |
| Task 8 | 新增 `task_view()` 与 `wait()`；`submit_many()` 立即返回句柄 | 桌面需要步骤/耗时视图；Dispatch 需要异步观察整批结果，不能阻塞提交 | 两者都是只读/等待辅助，不改变提交语义 |
| Task 8 | 新增 `_Record.submitted` 防止幂等命中重复启动 | 同一 `request_id` 第二次 `submit` 会走缓存句柄，不能再次 `_start` | 已由测试覆盖 |
| Task 12 | `DispatchService` 额外依赖 `LLMService` | 计划要求多候选时调 `chat_structured`，但依赖表未列 LLM | 已加入构造器与 `dependencies` |
| Task 12 | `DispatchPlan` 新增 `session_id` 字段 | `execute(plan)` 需要会话做幂等去重与 `submit_many` 传参 | 默认空串，旧数据兼容 |
| Task 12 | 多候选用一次 `DispatchPlan` 结构化调用完成精排与拆解 | 两段调用（MatchResult + 拆解）成本翻倍，且计划测试期望直接返回计划 | 模型失败仍回退检索结果 |
| Task 14 | `TerminalService` 额外依赖 `AgentRuntimeService` | 信封需要任务摘要、查询直答需要运行态 | 依赖无环，容器正常注入 |
| Task 14 | 主动消息被拒时只返回回执，不写死信 | 设计 06 章要求「不静默丢弃」，延迟投递已覆盖免打扰/输入中 | 频次类拒绝按设计直接回执 |
| Task 10 | `PresenceSnapshot.availability` 用 `dict[str, Availability]` | pydantic 不校验 Literal 键，服务层保证键集合；类型检查更稳 | 语义不变 |
| 全局 | 未执行计划里的「每个任务一次 git 提交」 | 会话规则要求未经用户明确要求不提交 | 由用户决定提交粒度 |

### 2. 已推迟的能力

| 能力 | 现状 | 归属 |
| --- | --- | --- |
| 真实用户通道 | 默认 `MemoryUserChannel` | 部署方实现 `UserChannelPort` |
| 外部/真实 LLM 世界推理 | 内置 `RuleWorldReasoningPort` 已可用，配置默认指向它；真实语义推理仍由部署方实现 `WorldReasoningPort` 后替换 | 部署方实现并替换端口 |
| `agent/aliya` | 只依赖契约与 `ControlService`，未实现 | Aliya 侧 |
| LLM 情绪打分联调 | 代码路径已实现，测试用假 LLM/固定置信度 | 手工联调时验证 |
| 长期历史聚合（`HistoryPattern.active_hours`） | 已实现：`TerminalService` 按天聚合消息数/活跃小时/响应延迟/主动回复，并落盘 `terminal_stats.json` | 世界推理通过 `set_history_provider()` 读取 |
| 推演轨迹与离线回放 | 已实现：每次推演写 `world_trace.jsonl`，并可从轨迹读取、校准阈值/温度、训练触发分类器 | 后续用真实「主动消息是否被回复」标签替换代理标签 |

### 3. 优化批次（2026-11-08）

在一次改造中落地了前面研究结论的 1–5 项：

| 项 | 实现 |
| --- | --- |
| 触发显著性门控 | 新增 `services/world_trigger.py`：规则打分 + 动态窗口 + 去重；定时 tick 降级为 600s 对账，静默按 60s 巡检 |
| 信念状态 | `BeliefState` 进入 `WorldInput/WorldInference`，记录 `belief_delta` 与 `prediction_error` |
| 历史与结果回流 | `TerminalService.history_pattern()` 从按天统计派生；`AgentRuntimeService.recent_results()` 填充 `recent_outcomes` |
| 快慢双路与缓存 | 冷启动先发 `producer="heuristic"`，深路返回后按 `revision` 覆盖；输入指纹缓存去掉时间戳/信念/上一轮摘要 |
| 校准与证据接地 | `confidence_breakdown` + 温度缩放；`basis_signals` 必须落在 `signals_used` 内；低置信假设丢弃 |
| 打扰预算 | 主动消息增加 `hourly_limit` 与 `urgency`（≥0.8 可突破小时预算，仍受每日上限约束）；`expected_value` 进入行动窗口 |

新增测试覆盖触发策略、世界推理、终端历史/预算；全量测试与静态检查见交付说明。

### 4. 优化批次 2（2026-11-08）：校准、分类器与 EFE

| 项 | 实现 |
| --- | --- |
| 轨迹读取 | `agent/cosmos/world/trace.py`：容错读取 `world_trace.jsonl`，按窗口过滤，坏行跳过 |
| 离线校准 | `agent/cosmos/world/calibration.py`：用「下一轮 prediction_error」做代理标签，拟合温度、`min_salience`、各触发统计，并训练逻辑回归 |
| 触发分类器 | `agent/cosmos/world/classifier.py`：固定十几维特征 + JSON 权重；`world.triggers.model_path` 指向权重后接管打分，损坏自动退回规则 |
| 有限时域 EFE | `services/world_policy.py`：实用 + 认识 - 成本，再做两步前瞻（默认 γ=0.8）；产出 `action_choice` 的 `efe / lookahead / total_efe` |
| 轨迹扩展 | 每行新增 `salience`、`trigger_features`、`action_choice`，供训练与回放 |
| 文档同步 | `07-world-reasoning.md` 更新契约、触发/信念/EFE/校准；`06-terminal.md`、`09-control-api.md`、`10-flows.md` 同步小时预算与 `urgency` |

> 说明：EFE 是两步前瞻 + 手写信念转移，不是多步 POMDP 求解；分类器需要至少 20 对带 `prediction_error` 的轨迹再启用。两者都保留规则/默认路径，不阻断启动。

### 5. 对话服务优化（2026-11-08）

| 项 | 实现 |
| --- | --- |
| 脱敏闭环 | `RedactionResult` 增加 `text / spans`；`ConversationService.redact()` 返回遮蔽文本；`TerminalService.deliver()` 实际使用 |
| 分层脱敏 | 内部标识 / 密钥 / PII 使用 `mask`，工程词使用 `flag`；身份证与银行卡带校验位，降低误报 |
| 意图安全 | 否定词优先、非任务短语过滤、低置信任务 `needs_confirmation`，终端不再自动派发 |
| 意图承接 | 同一会话的短跟进继承 `pending_intent`；取消词清空 |
| 上下文预算 | `context_max_tokens`、批量摘要、pinned facts、会话 TTL/LRU |
| 分段保护 | 代码块/URL/行内代码原子化，字素边界切分，`max_parts` 限流 |
| 配置/文档 | `agent-settings.example.yaml`、`data/config/cosmos.yaml`、`06-terminal.md` 同步 |

> 说明：尚未接入 embedding 意图层与命名实体 NER；这两项依赖额外模型/标注，保留为下一阶段。

### 6. 意图分类完全移除（2026-11-25）

已删除整条链路，不只是关闭开关：

- 契约：移除 `IntentResult / IntentKind / QueryScope`、`InboundEnvelope.intent / dispatch_plan / direct_answer`、`ConversationContext.pending_intent`；
- 服务：删除 `ConversationService.classify()`、规则词表、模型回退、短跟进承接；
- 终端：删除 query 直答、`_direct_answer()`、自然语言 `DispatchPlan` 生成与 `DispatchService` 依赖；
- 配置：删除 `terminal.intent` 与 `AgentIntentSettings`；
- 错误：删除不再使用的 `TerminalRoutingError`。

现在 `ingest()` 只做状态采集、上下文组装、信封发布；所有消息交给 Aliya，
任务只能由 Aliya 或 Control API 显式发起。

### 7. 世界推理接入与清理（2026-11-25）

| 项 | 实现 |
| --- | --- |
| 真实端口 | 新增 `agent/cosmos/world/reasoning.py::RuleWorldReasoningPort`，基于 presence/tasks/agents/history 生成保守结论；`port_module` 已指向它 |
| force 语义拆分 | `refresh(force=True)` 只绕过节流/退避；新增 `reuse=True` 控制缓存与冷启动快路；事件触发与 `refresh_world()` 使用复用，显式 `get_world_view(force=True)` 保持新鲜 |
| 死配置清理 | 删除 `world.tick_seconds` 与 `world.min_interval_seconds`；窗口统一由 `triggers.min_salience / high/low_salience_window_seconds` 决定 |
| 校准回填 | `world.calibration.report_path` 指向报告时，启动期自动校验并回填 `temperature`、`min_salience` |
| 配置 | `data/config/cosmos.yaml` 与示例开启世界推理并指向内置端口 |

新增测试：规则端口 4 例、force+reuse 缓存 1 例、启动校准回填 1 例；全量验证通过。

### 8. 链路、状态与调度补齐（2026-11-25）

| 项 | 实现 |
| --- | --- |
| 链路心跳 | `TerminalService` 增加 `note_link_heartbeat()` 与后台巡检；超过 `heartbeat_seconds` 判 unstable，超过 `down_after_seconds` 判 down；链路事件带真实积压数 |
| 通道状态上报 | `TerminalService.report_presence()` 与 `ControlService.report_presence()`；真实通道可直接上报在线/输入/设备 |
| 情绪与等待状态 | `TerminalService.ingest()` 异步用最近消息更新情绪；`DispatchService.execute()` 置 awaiting，聚合结束复位 |
| 调度并发 | `AgentRuntimeService._run_batch()` 按 `dispatch.max_parallel` 分批启动；`cancel_on_supersede` 在提交前取消同会话同 agent 旧任务 |
| 调度回执 | 完成耗时写入 `record.elapsed`；`DispatchHandle` 填充 `rejected/partial`；去重窗口使用 `dispatch.dedupe_window_seconds` |
| 世界推理 | `tick` 绕过显著性门控做定时兜底；`dispatch_finished`、`world.history.window_days` 真正生效；链路/上线/Aliya 时间字段接入 |
| 校准与补偿 | 单类别分类器标记 `degenerate`；事件缺口 >100 时 `ControlService` 返回「快照 + 实时事件」 |
| 权限 | `AgentContext.read_presence()` 按 `permissions.read_presence + presence_fields + privacy.expose_to_subagents` 过滤；`ControlService.open_agent()` 只读详情 |

新增测试覆盖链路心跳、通道状态、情绪阈值、调度并发/supersede/耗时/拒绝回执、世界 tick/开关、分类器退化与事件缺口补偿。
