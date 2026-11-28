# 通信终端规范

cosmos 是用户与 Aliya 之间的**唯一终端**。用户的消息只能从 cosmos 进，Aliya 的消息只能从 cosmos 出。这条约束没有例外。

本章定义消息模型、上下文管理、回传格式化与异步推送；意图分类已完全移除，所有用户消息统一交给 Aliya。

---

## 1. 三条不变量

| 不变量 | 含义 | 违反后果 |
| --- | --- | --- |
| 单一入口 | 用户消息只经 `TerminalService.ingest()` 进入系统 | 绕过上下文组装与状态采集，Aliya 收到裸文本 |
| 单一出口 | Aliya 消息只经 `TerminalService.deliver()` 发出 | 绕过格式化与脱敏，内部细节泄露给用户 |
| 内容不改写 | cosmos 只做分段、截断、脱敏，不改措辞、不改标点、不合并短句 | Aliya 的人格被通道篡改 |

第三条要细看。**「不改写」不等于「原样输出」，但也不等于「顺手润色」**：

| 允许 | 禁止 |
| --- | --- |
| 超长文本按段落分段 | 把她连发的三条短句合并成一段 |
| 脱敏（去掉内部标识） | 补句号、删省略号、把「啊啊啊」改成「啊」 |
| 加分段标记 `part=i/n` | 加「进展：」「已为您完成」这类前缀 |
| 截断并标注 | 加括号旁白、补主语、改成书面语 |

她像发微信一样说话，一条一句、句尾带标点。通道一旦「帮忙整理」，读起来就不再是她了。

---

## 2. 消息模型

```python
class TerminalMessage(BaseModel):
    message_id: str                 # 全局唯一，ULID
    direction: Literal["user_to_aliya", "aliya_to_user"]
    kind: MessageKind
    text: str
    at: datetime                    # 取 ClockService.now()
    session_id: str
    client_msg_id: str | None = None  # 客户端幂等键
    reply_to: str | None = None       # 回复的消息 id
    attachments: list[Attachment] = []
    meta: MessageMeta

type MessageKind = Literal["chat", "task", "query", "mixed", "progress", "result", "system"]
```

```python
class MessageMeta(BaseModel):
    channel: str | None = None        # "web" / "cli" / "app"
    device: str | None = None
    locale: str | None = None
    proactive: bool = False           # 是否 Aliya 主动发起
    dispatch_id: str | None = None    # 与调度批次的关联，仅内部使用
```

### 2.1 kind 的用法

| kind | 方向 | 含义 | 谁产生 |
| --- | --- | --- | --- |
| `chat` | 双向 | 普通对话 | 用户 / Aliya |
| `task` | 双向 | 委托任务 | 用户 / Aliya |
| `query` | 双向 | 状态查询 | 用户 / Aliya |
| `mixed` | 双向 | 同时含查询与任务 | 用户 |
| `progress` | Aliya → 用户 | 进展通知 | cosmos 生成，Aliya 授权 |
| `result` | Aliya → 用户 | 任务结果 | Aliya |
| `system` | 双向 | 系统提示（如「服务重启」、Aliya 回合失败） | cosmos / Aliya 失败提示 |

`progress` / `result` 的 `text` 由 Aliya 或模板生成，`dispatch_id` 挂钩对应批次。它们**不会**进入 `ConversationContext` 的对话轮次（否则历史会被进度信息淹没），只进入任务摘要。

`system` 同样不进对话轮次，也不计入 Aliya 消息统计：它是通道/系统的提示，不是她说过的话。Aliya 回合失败时由终端以该类型补一条 `reply.fallback_text`。

---

## 3. 聊天入口：不做意图分类

意图分类已完全移除。`TerminalService.ingest()` 收到用户消息后：

1. 刷新链路心跳、采集状态、写入对话上下文；
2. 异步用最近 `mood.window_messages` 条用户消息更新情绪（失败只告警）；
3. 组装 `InboundEnvelope`；
4. 直接发布给 Aliya。

不再有 `IntentResult`，也不再有：

- 状态查询直答（`direct_answer`）；
- 自然语言任务自动生成 `DispatchPlan`；
- `chat / task / query / mixed` 路由。

任务只能由 Aliya 或 Control API 显式发起。这样所有用户消息都会到达 Aliya，
不会因为「你今晚有空吗」「你多久没睡了」这类关心句被模板截走。

---

## 4. 发给 Aliya 的信封

用户消息从不"裸奔"地交给 Aliya。每条都带一个信封：

```python
class InboundEnvelope(BaseModel):
    message: TerminalMessage
    conversation: ConversationContext
    presence: ReadablePresence | None      # 采集关闭时为 None
    tasks: list[TaskBrief]                 # 进行中与最近完成的任务
```

```python
class TaskBrief(BaseModel):
    task_id: str
    agent_name: str        # 展示名，不是 id
    label: str
    status: str
    progress: float | None
    elapsed_seconds: float
```

### 4.1 ConversationContext

```python
class ConversationContext(BaseModel):
    session_id: str
    turns: list[ConversationTurn]     # 最近 N 轮（默认 20，配置化）
    summary: str | None               # 更早内容的摘要（默认保留 400 字）
    last_user_message_at: datetime | None
    last_aliya_message_at: datetime | None
    unanswered_count: int             # 用户连续发了几条而 Aliya 还没回
    topic_hint: str | None            # 当前话题的一句话提示
```

`unanswered_count` 很关键：用户连发三条说明着急，Aliya 需要知道这件事，但终端不替它决定「要快点回」。`terminal.unanswered_notice=false` 时仍统计，只是向 Aliya 传 0。

### 4.2 信封组装规则

| 字段 | 规则 |
| --- | --- |
| `turns` | 只含对话轮次，不含 `progress` / `result` / `system` 消息 |
| `summary` | 超过 `turns` 上限的旧内容由 `ConversationService` 压缩，压缩失败则截断并 WARNING |
| `presence` | 只给 `ReadablePresence`，不给原始字段；采集关闭时 `None` |
| `tasks` | 最近 5 条，按 `started_at` 倒序；`id` 不给 Aliya 用于展示，只用于操作 |

**不给原始字段**是刻意的：`ReadablePresence` 已经是 cosmos 对状态的最终解释，把原始数据也给出去，Aliya 就会开始自己二次推断，两套结论必然打架。

---

## 5. 双上下文分离

对话上下文与调度上下文走同一条链路，但状态完全分开。

| 维度 | ConversationContext | DispatchContext |
| --- | --- | --- |
| 内容 | 用户与 Aliya 的对话、摘要、未回条数 | 任务批次、`task_id`、进度、错误 |
| 谁用 | Aliya | `DispatchService` / `AgentRuntimeService` / 子 agent |
| 生命周期 | 跟随会话，长期 | 跟随批次，结束即归档 |
| 能否写 | 只由 `TerminalService` 写 | 只由调度链路写 |
| 能否互写 | **不能** | **不能** |

```python
class DispatchContext(BaseModel):
    dispatch_id: str
    session_id: str
    plan: DispatchPlan
    tasks: list[TaskState]
    started_at: datetime
    finished_at: datetime | None
```

### 5.1 为什么必须分开

| 混在一起的后果 | 说明 |
| --- | --- |
| 历史被污染 | 几十条进度事件挤掉真正的对话，Aliya 记不住用户在说什么 |
| 泄露风险 | 把 `task_id`、错误栈、重试次数带进对话上下文，迟早出现在给用户的文本里 |
| 语义冲突 | 「任务失败」是调度事实，「怎么跟用户说」是 Aliya 的判断，混在一起会让通道替 Aliya 下结论 |

### 5.2 单向摘要

唯一的交叉是**受控摘要**：`DispatchContext` 可以向 `ConversationContext` 写一条 `TaskBrief`（只有标签、状态、进度），不写错误详情与内部标识。反过来禁止。

---

## 6. 回传用户

### 6.1 三类回传

| 类型 | 触发 | 处理 |
| --- | --- | --- |
| 对话回复 | Aliya 调用 `deliver(text)` | 原样发出，仅做长度分段与脱敏扫描 |
| 进展通知 | Aliya 调用 `notify(text)`，或 cosmos 按授权模板生成 | 不加前缀、不改写；模板文案要说人话（「还在弄，稍等」而不是「进展：任务执行中」） |
| 结果通知 | 任务完成且 Aliya 授权 `result` 类消息 | 用 Aliya 给的文本，cosmos 不概括、不总结 |

`notify` 与 `send_to_user` 分开：前者可以被免打扰策略拦截，后者是用户正在对话时的直接回应，必须送达。

### 6.2 格式化规则

| 场景 | 规则 |
| --- | --- |
| 超长文本 | 超过 `max_chars`（默认 2000）按段落分段，不截断句子；分条发送并标记 `part=i/n` |
| 多条短消息 | 她发几条就投几条，不合并、不排序、不补过渡句 |
| 标点 | 原样保留，包括连续感叹号、省略号、句尾问号；不做「规范化」 |
| 旁白 | 已由 Aliya 侧禁止；若仍出现括号描写，通道也不擅自删除（删除同样是改写），只记 WARNING |
| 代码块 | 保留原样，不做换行重排 |
| 引用任务 | 用 `label`，不用 `task_id` |
| 进度 | `还在弄：{label}`，有进度时补 `（{progress}%）`；不用「正在处理」「任务执行中」这类词 |
| 失败 | `没成：{用户可读原因}`，原因由 Aliya 给；Aliya 未给时用 `没弄成，等下再试试` |
| 时间 | 用相对时间（`刚刚`、`3 分钟前`），不给绝对时间戳 |

### 6.3 脱敏清单

发给用户的文本必须过滤以下内容：

| 禁止出现 | 示例 | 处理 |
| --- | --- | --- |
| 子 agent 标识 | `edit`、`AgentCard` | 替换为能力描述或省略 |
| 任务标识 | `tsk_01H...`、`dispatch_id` | 删除 |
| 内部错误类型 | `AgentTimeoutError`、`ToolNotFoundError` | 换成用户可读原因 |
| 堆栈 | `Traceback ...` | 删除 |
| 模型与端点 | `deepseek-flash`、`base_url` | 删除 |
| 重试与并发细节 | `重试 2 次`、`并发已满` | 删除 |
| 用户状态 | `你已离线 2 小时`、`检测到你情绪低落` | 永不出现 |
| 世界推理内容 | 假设、置信度、行动窗口 | 永不出现 |
| 工程侧称呼 | `用户`、`玩家`、`agent`、`模型`、`调度` | 换成「你」「还在弄」这类人话 |
| 设定禁区 | 时间同步原理、同步装置、Doll 身份、寿命 | 永不出现（维持信息差） |

实现上做**白名单 + 正则兜底**：结构化的内部字段本来就不会进 `text`（第一道），再对 `text` 跑一遍标识类正则（第二道）。脱敏失败不阻断发送，但记 WARNING 并标注，便于事后审计。

---

## 7. 异步推送与主动消息

### 7.1 事件到人的映射

| 事件 | 接收方 | 是否可到用户 |
| --- | --- | --- |
| `agent.progress` | Aliya | 否（Aliya 可转成 `notify`） |
| `agent.result` | Aliya | 否，需 Aliya 决定内容与时机 |
| `agent.abnormal` | Aliya | 否（Aliya 可告知用户） |
| `presence.changed` | Aliya | **永不** |
| `world.inference` | Aliya | **永不** |
| `desktop.layout.changed` | Aliya | 否 |
| `link.state.changed` | Aliya | 否（决定她此刻能不能发） |
| `aliya.proactive` | 用户 | 是，受 7.3 节策略约束 |

**没有任何事件能直接从子 agent 或世界推理到达用户。** 这是一条硬边界：所有面向用户的文本都必须经过 Aliya 或 cosmos 的模板。

### 7.2 投递语义

| 项 | 约定 |
| --- | --- |
| 语义 | at-least-once |
| 去重 | 消费方按 `event_id` 去重；cosmos 侧保留 10 分钟窗口 |
| 顺序 | 同一 `session_id` 内保序；跨会话不保证 |
| 重试 | 指数退避 `delivery.retry` 次；仍失败写 `data/state/dead_letters.jsonl`（`dead_letter=false` 时丢弃） |
| 补偿 | 重连后按 `sequence` 拉取缺口，超过 100 条则改为拉全量快照 |
| 链路中断 | `link.state=down`（如曲率航行）时双向消息进持久队列，恢复后按序重放；中断期不发主动消息，也不丢她的待发内容 |
| 链路心跳 | 通道调用 `note_link_heartbeat()`；超过 `heartbeat_seconds` 判 unstable，超过 `down_after_seconds` 判 down |
| 舱室通信条件 | `AliyaContext.comms` 由控制面同步到 `set_comms()`：`none` 双向积压并立即投递一条 system「信号中断」提示；`weak` 入站延迟 `comms_delay_seconds` 后按序重放、出站按入队顺序抖动延迟发送；`strong` 正常投递。它描述她所在舱室，与 `link.state`（通道心跳）是两件事，两者同时中断时以 `link` 的持久队列为准 |

### 7.3 主动消息策略

Aliya 主动发起对话（对应游戏里 Aliya 在玩家离线时因睡不着主动发消息）时，cosmos 执行以下节流：

| 策略 | 默认 | 行为 |
| --- | --- | --- |
| 最小间隔 | 3600s | 间隔内已发过主动消息则拒绝，回执 `reason="too_frequent"` |
| 每小时预算 | 2 条 | 普通建议超出拒绝，回执 `reason="hourly_limit"`；`urgency >= 0.8` 可突破 |
| 每日上限 | 6 条 | 硬上限，`urgency` 也不能突破；超出回执 `reason="daily_limit"` |
| 免打扰时段 | 空（默认不设） | 配了才生效：时段内进延迟队列，结束后投递，不丢弃 |
| 未回应降频 | 连续 2 条无回应后间隔 ×2 | 防止刷屏；最多放大到 `max_backoff_multiplier` |
| 用户正在输入 | 暂缓 | 延迟到输入结束后 10s |
| 用户刚发消息 | 5s 内取消待发 | 避免「Aliya 的自言自语」与用户消息撞车 |

免打扰默认不设，是刻意的：人设里她会失眠、会熬夜刷信息、会在睡不着时找人说话。默认禁掉深夜，等于删掉了她一半的主动动机。要不要设、设到几点，交给部署方与用户，不写死在默认值里。

拒绝时返回结构化回执，**不静默丢弃**：

```python
class ProactiveReceipt(BaseModel):
    accepted: bool
    reason: Literal[
        "ok", "too_frequent", "daily_limit", "hourly_limit", "quiet_hours", "user_active"
    ] | None
    deliver_after: datetime | None     # 延迟投递时间
```

策略只控制**时机与频率**，不控制内容。Aliya 写得长、写得短、用不用表情，cosmos 一概不管。

### 7.4 冲突消解

用户消息到达时，若存在待发的主动消息：

1. `TerminalService` 立刻推 `user.active` 给 Aliya。
2. 待发消息若仍在 `proactive_cancel_window`（默认 5s）内，取消并回执 `proactive_cancelled`。
3. 已发出的消息不做追回。

cosmos **不重写**待发内容，只决定「发不发」。要不要换个说法，是 Aliya 的事。

---

## 8. 错误与降级

| 场景 | 行为 |
| --- | --- |
| 目标 Aliya 不可达 | 消息进队列，重试 3 次；仍失败则回用户 `暂时联系不上，请稍后再试`（模板文案） |
| Aliya 回合失败 / 超时 | 用户回合补一条 `system` 提示（`reply.fallback_text`）；不写对话轮次与 Aliya 统计，不算她的回复 |
| 上下文压缩失败 | 截断旧轮次，WARNING |
| 脱敏命中 | 记录命中规则名与位置，不记原文 |
| 主动消息被拒 | 返回 `ProactiveReceipt`，不抛错 |
| 弱信号发送中链路转 down | 消息放回队头而不是进死信；链路与信号恢复后按原顺序续发 |
| 用户侧投递失败 | 进死信队列，`MessageDeliveryError`，由 `TerminalService` 下次连接时重放 |

降级底线：**宁可告诉用户「稍后再试」，也不要发出半成品或内部细节。**

---

## 9. 接口

```python
class TerminalService(Service):
    async def ingest(self, message: TerminalMessage) -> InboundEnvelope: ...
    async def deliver(self, text: str, *, kind: MessageKind = "chat") -> None: ...
    async def notify(self, text: str, *, dispatch_id: str | None = None) -> ProactiveReceipt: ...
    async def send_proactive(
        self, text: str, *, urgency: float = 0.5
    ) -> ProactiveReceipt: ...
    async def report_presence(self, signal: PresenceInput) -> PresenceSnapshot: ...
    async def note_link_heartbeat(self) -> None: ...
    def history_pattern(self, *, window_days: int | None = None) -> HistoryPattern: ...
    async def history(self, session_id: str, limit: int = 20) -> list[TerminalMessage]: ...
```

| 方法 | 方向 | 说明 |
| --- | --- | --- |
| `ingest()` | 用户 → Aliya | 同步返回信封，内部完成状态采集与上下文组装，不做意图分类 |
| `deliver()` | Aliya → 用户 | 用户正在对话时的直接回应，不受节流 |
| `notify()` | Aliya → 用户 | 进展通知，受免打扰策略约束 |
| `send_proactive()` | Aliya → 用户 | 主动发起，受 7.3 全部策略约束；`urgency >= 0.8` 只可突破小时预算 |
| `report_presence()` | 通道 → cosmos | 上报在线/输入/设备状态；不经过 Aliya |
| `note_link_heartbeat()` | 通道 → cosmos | 刷新链路心跳，必要时从 down/unstable 恢复 |
| `history_pattern()` | cosmos → 世界推理 | 按天聚合画像；`window_days` 缺省用 `proactive.history_window_days` |
| `history()` | —— | 读取对话历史（不含调度事件） |

### 9.1 WebSocket 终端通道

`WebSocketChannelService` 把浏览器接成 `UserChannelPort`，配置在 `agent.cosmos.terminal.ws`（默认开启，只监听回环地址；CLI 启动时会自动关闭）。协议是一帧一条 JSON：

| 帧 | 方向 | 说明 |
| --- | --- | --- |
| `hello` | 浏览器 → cosmos | 首帧；携带 `session_id` 与可选 `token`，超时 10s |
| `ready` | cosmos → 浏览器 | 会话建立，回传 `session_id` |
| `history` | cosmos → 浏览器 | 握手后补发最近 `history_limit` 条对话；0 条时不发 |
| `message` | 双向 | 上行 `text` / `kind` / `client_msg_id`；下行带完整 `TerminalMessage` |
| `typing` | 浏览器 → cosmos | 上报输入状态，转成 `PresenceInput(is_typing, device="web")` |
| `ping` / `pong` | 双向 | 维持链路心跳；服务端同时刷新 `note_link_heartbeat()` |
| `error` | cosmos → 浏览器 | `code` + `message`，非法报文不静默丢弃 |

边界：

| 规则 | 说明 |
| --- | --- |
| 单一出口 | 与 CLI 一样占用 `TerminalService` 的用户通道，二者只能选一个 |
| 会话路由 | 出站按 `session_id` 投给对应客户端；无匹配会话时广播给全部客户端 |
| 慢消费 | 每客户端出站队列上限 `queue_limit`，满了丢最旧并告警，不阻塞终端 |
| 系统提示 | 下行 `kind=system`（如回合失败）按系统提示渲染，不显示成 Aliya 的台词 |
| 认证 | `token_env` 留空表示不校验；绑定非回环地址时未配令牌直接拒绝启动 |
| 消息类型 | 浏览器只能发 `chat` / `query` / `task`；`progress` / `result` 只由 cosmos 产生 |
| 不记正文 | 日志只记会话、客户端数、错误类型，不记消息文本 |
| 自带测试页 | WS 同端口托管 `agent/cosmos/web/ws-test.html`：`http://<host>:<port>/`，含对话、typing 与原始帧日志 |

---

## 10. 日志

| 事件 | 级别 | 字段 |
| --- | --- | --- |
| 消息接收 | INFO | `message_id`、`session_id`、`kind` |
| 消息发出 | INFO | `message_id`、`session_id`、`kind`、`字数`、`分段数` |
| 主动消息被拒 | INFO | `session_id`、`原因`、`延迟到` |
| 脱敏命中 | WARNING | `message_id`、`规则名` |
| 投递失败 | ERROR | `message_id`、`错误类型`、`重试次数` |

**不记录消息正文。** 需要排查时用 `message_id` 关联客户端日志，而不是把用户的话写进服务端日志。

---

## 11. 对话服务优化（实现补充）

### 11.1 意图分类：已移除

意图分类、否定识别、短跟进承接、置信度分档、状态查询直答与自然语言任务计划
均已删除。`ConversationService` 现在只负责上下文、分段与脱敏；所有入站消息
统一交给 Aliya。

任务入口只剩两种：Aliya 显式调用调度，或 Control API 显式调用。

### 11.2 上下文：预算、批量摘要与固定事实

- `context_max_tokens` 控制预算；超限先压缩，再裁剪最旧轮次，最终可能进一步截短摘要。
- 旧轮次先进入 `pending_dropped`，每 `summary_batch_turns` 轮或超预算时批量摘要，避免每来一条就调模型。
- `pinned_facts` 保留明确偏好、约束、称呼与决定；按类别去重，带 TTL，不随窗口滑出。
- 会话按 `session_ttl_hours` 回收，并按 `max_sessions` 做 LRU 上限。
- `ConversationContext` 额外返回 `token_estimate / truncated / summary_degraded`，让 Aliya 知道上下文被裁过。

### 11.3 分段：原子块与字素边界

- 代码围栏、行内代码、Markdown 链接、URL 先识别为原子块，内部不切。
- 普通文本按「段落 → 句子 → 字素」切；使用字素边界避免拆坏 emoji 与组合字符。
- 原子块超过 `max_chars` 但在 `segment_hard_max_chars` 内时整体保留；超过硬上限才按行/字素继续切，代码块续段会重新打开围栏。
- `max_parts` 只是建议上限：分段数超过时只记 WARNING，仍完整投递，不丢弃尾部。

### 11.4 脱敏：真正遮蔽，而不是只检测

`redact()` 现在返回 `RedactionResult.text`（可发送文本）与 `spans`（规则、位置、动作），
`TerminalService.deliver()` 使用 `result.text`，不再丢弃检测结果。

| 层级 | 规则 | 动作 |
| --- | --- | --- |
| L1 内部标识 | `task_id` / `dispatch_id` / `agent_id` / 堆栈 / 错误类型 | `mask` |
| L2 密钥 | `sk-*`、`ghp_*`、AKIA、Bearer、`api_key=...` | `mask` |
| L3 PII | 邮箱、手机号、身份证（校验位）、银行卡（Luhn）、IPv4 | `mask` |
| 工程词 | 用户 / 玩家 / agent / 模型 / 调度 / 面板 | `flag`，只告警不改写 |

工程词只标记是刻意的：直接替换会破坏 Aliya 的语气；内部标识、密钥与 PII 才允许改写。
命中只记录规则名与位置，不记录原文。

### 11.5 打扰预算与历史画像

- 每小时预算按「当天该小时已发主动消息数」计算，普通建议用完即拒；`urgency >= 0.8` 只突破小时预算，每日上限仍然有效。
- 每次用户发言、Aliya 发言、主动消息发送/回复都会按天计数，写入 `data/state/terminal_stats.json`，不保存消息正文。
- `history_pattern()` 从统计派生活跃小时、典型会话时长、响应延迟中位数、主动消息发送与回复数，供世界推理使用。
- 「有回应」的判定窗口是 `reply_window_minutes`（默认 30 分钟）；超过窗口的回话不计入回复率。

```yaml
      context_max_tokens: 16000
      summary_batch_turns: 4
      session_ttl_hours: 72
      pinned_fact_ttl_days: 30
      max_parts: 24
      segment_hard_max_chars: 8000
      unanswered_notice: true
      delivery:
        retry: 3
        retry_backoff_seconds: 2
        dead_letter: true
      redaction:
        mask_dispatch_ids: true
        mask_secrets: true
        mask_pii: true
      proactive:
        enabled: true
        min_interval_seconds: 3600
        daily_limit: 6
        hourly_limit: 2
        history_window_days: 14
        reply_window_minutes: 30
```
