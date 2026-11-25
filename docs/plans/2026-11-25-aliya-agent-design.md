# Aliya Agent 设计

> 状态：设计定稿，待实现
> 日期：2026-11-25
> 关联文档：`agent/aliya/README.md`、`agent/cosmos/docs/09-control-api.md`、`agent/cosmos/docs/06-terminal.md`、`data/prompt/Aliya.md`

## 0. 一句话

Aliya 是独立进程中的对话主体与决策者；cosmos 是唯一终端与调度中枢。两者通过本机 Unix 域套接字上的 NDJSON 通信；Aliya 用「单 LLM + 8 个工具」完成闲聊、派活、结果汇报与自主行动。

---

## 1. 背景与现状

| 事实 | 影响 |
| --- | --- |
| `agent/cosmos` 已实现 10 个常驻服务，含 `ControlService` | Aliya 只需依赖契约与控制面，不碰内部实现 |
| 意图分类已从 cosmos 整条删除，`InboundEnvelope` 不再含 `intent` / `dispatch_plan` | Aliya 必须自己决定「聊还是动手」；`agent/aliya/README.md` 第 5 节已过时 |
| `agent/aliya` 目前只有 README、配置样例与空 `__init__` | 主循环、提示词、记忆、自主行动全部待落地 |
| 用户提供的《Aliya agent 提示词》是行为宪法，不是可直接注入的 system prompt | 需要拆成「可见规则」与「实现约束」两层 |
| `ControlService` 是具体 Service 类，`contracts/control.py` 只有 `CosmosCapabilities` | 独立进程需要新增 RPC 服务端与 `ControlPort` 协议 |

---

## 2. 设计决策

| 编号 | 决策 | 理由 |
| --- | --- | --- |
| D1 | Prompt 分层拼装：人格 + 准则可见区 + 工具说明 + 回合上下文 | 人格与行为规则来源不同，混在一起会互相污染 |
| D2 | Aliya 独立进程，`python -m agent.aliya.app` 启动 | 用户选定；进程隔离便于单独重启与调试 |
| D3 | 传输用 Unix 域套接字 + NDJSON | 本机权限即鉴权、零新依赖、双向流、易测试；Windows 回退 TCP 回环 + 令牌放到后续里程碑 |
| D4 | 每回合「单 LLM + 工具调用」，最多 6 轮 | 聊天与任务由工具选择体现，不再造一层意图分类 |
| D5 | 工具面 8 个 | 覆盖回话、派活、查进度、改主意、看推演、记忆，数量不至于让模型选错 |
| D6 | 长期记忆用 JSONL + 派生 JSON | 追加简单、可恢复、不引依赖；注入时只用摘要与锚点 |
| D7 | 自主行动 = 60s 巡检 + 事件，三层门控 | 纯事件会在世界推理关闭时永不主动；固定日程会变成机械打卡 |
| D8 | 只有终态事件开汇报回合 | `agent.result` / `dispatch.finished` / `agent.abnormal` 才值得打断；进度只更新上下文 |
| D9 | 出站检查「重写一次 → 遮蔽降级」 | 拦截不发会让用户收不到回复；直接替换容易把句子改坏 |
| D10 | 共享传输配置放 `agent.control` | socket 路径、令牌、超时只写一处，服务端与客户端读同一份 |
| D11 | cosmos 侧 `ControlServerService` 进容器 | 生命周期与健康检查由 `ServiceManager` 统一管理 |
| D12 | 准则文件单文件双区（`visible` 标记） | 避免「开发规范」与「运行规则」两份文档漂移 |
| D13 | Aliya 只读 `ReadablePresence`，不读原始字段 | 与「对等隐私」一致：她要求关掉自己的 HRM，就不该拿到别人的监控视角 |

---

## 3. 与现有文档的冲突裁决

| 冲突 | 现状 | 裁决 | 需要修改 |
| --- | --- | --- | --- |
| 用户状态是「面板」还是「线索」 | 04 章明确「这不是监控台」；`readable` 只给在线/输入/静默/等结果 | 线索。准则只列这四类，删掉情绪数值、设备、时区 | 准则可见区；`agent/aliya/README.md` 第 2.4 节 |
| 是否保留意图分类 | cosmos 已删除；README 仍按 `intent.kind` 分支 | 不恢复。闲聊/任务由 `dispatch_task` 是否被调用体现 | `agent/aliya/README.md` 第 5 节、第 8 节测试 |
| 世界推理是「只推」还是「可拉」 | `ControlService` 同时有 `get_world_view` / `refresh_world` | 默认订阅推送；需要即时判断时允许 `refresh_world()`，它不是直接调用推理 agent | 准则；`README.md` 第 4 节 |
| Aliya 是「主人/操作者」还是「决策者」 | 09 章权限矩阵：不能启用/禁用/注册、不能改采集与世界配置 | 决策者。准则列出她能做与不能做的白名单 | 准则；`README.md` 第 2 节 |
| 人格文件里的「玩家」 | `data/prompt/Aliya.md` 有 11 处「玩家」，而工程词禁令包含它 | 一次性把源文件改成「对方」或改写为无该词的句子，不做运行时替换 | `data/prompt/Aliya.md` |
| cosmos 是否呈现 Aliya 自身状态 | cosmos 只有子 agent 运行态，没有 Aliya 状态模型 | 不做面板。她可以在话里说「我先去忙一下」，但不建模 | 准则删掉该段 |
| 长期记忆归属 | cosmos 有 `ConversationContext`，Aliya 配置另有 memory | cosmos 管短期工作上下文；Aliya 管长期关系、锚点、未收尾话题、私密线 | `README.md` 第 6 节 |
| 任务约束怎么表达 | `DispatchIntent` 只有 `user_text / hint / params` | 约束写进 `hint`，是建议不是硬保证；不扩展契约 | 准则；工具说明 |

---

## 4. 架构

### 4.1 进程拓扑

```text
┌─────────────────────────────┐         UDS + NDJSON          ┌─────────────────────────────┐
│ cosmos 进程                  │◄─────────────────────────────►│ Aliya 进程                   │
│  ControlServerService        │  call / result / error        │  RemoteControl               │
│  ControlService              │  inbound / event / ack        │  AliyaRuntime                │
│  Terminal / Dispatch / ...   │  subscribe / ping             │  TurnRunner / ToolBox        │
│  BusService                  │                               │  PromptBuilder / MemoryStore │
└─────────────────────────────┘                               └─────────────────────────────┘
```

- cosmos 先启动，`ControlServerService` 绑定 `data/state/aliya.sock`，文件权限 `0600`。
- Aliya 后启动，握手后开始消费；cosmos 不因 Aliya 断开而退出。
- 默认只允许一个 Aliya 客户端，避免两个实例同时回话。

### 4.2 组件

```text
agent/cosmos/
  contracts/rpc.py             # RpcRequest / RpcResponse / RpcError / Hello / Subscribe / Ack
  services/control_server.py   # ControlServerService：UDS 服务端、会话、鉴权、推送队列

agent/aliya/
  app.py                       # 入口：读配置、日志、连接、运行
  ports.py                     # ControlPort 协议
  transport.py                 # RemoteControl：NDJSON 客户端、重连、续订、outbox
  runtime.py                   # AliyaRuntime：入站循环、事件循环、回合队列、定时器
  prompt/loader.py             # 加载人格与准则可见区
  prompt/builder.py            # 拼 L0-L4，控制预算
  turn/runner.py               # 单回合：LLM + 工具循环
  turn/tools.py                # 8 个工具
  turn/postcheck.py            # 出站检查与重写
  memory/store.py              # JSONL + 派生 JSON
  initiative.py                # 巡检与三层门控
```

### 4.3 依赖方向

```text
agent.aliya ──► agent.cosmos.contracts ──► core
             ──► core.llm / core.agent / core.logger / core.config
```

- 允许：`from agent.cosmos.contracts import InboundEnvelope, ...`
- 禁止：`from agent.cosmos.services.control import ControlService`
- Aliya 通过 `ControlPort` 调用；同进程测试用 `InProcessControl` 适配器，生产用 `RemoteControl`。

---

## 5. Prompt 分层

### 5.1 四层

| 层 | 内容 | 来源 | 预算 |
| --- | --- | --- | --- |
| L0 安全与信息差 | 不解释时间同步、不提 Doll/寿命、不把状态说破、工程词不出现在台词里 | 准则的可见区 | 不截断 |
| L1 人格 | 身份、关系、语气、价值观、情境模板 | `data/prompt/Aliya.md` | 不截断 |
| L2 行为准则 | 能做什么/不能做什么、何时主动、结果怎么汇报、被拒怎么办 | 准则的可见区 | 不截断 |
| L3 工具说明 | 工具名、一句话、参数 schema | `ToolRegistry` 自动生成 | 不截断 |
| L4 回合上下文 | 信封、事件、记忆摘要、世界推演、桌面摘要 | 每回合组装 | 受 `context_max_tokens` 约束 |

### 5.2 优先级

按领域分开裁决，不用单链：

| 领域 | 优先级 |
| --- | --- |
| 安全与信息差 | 最高，任何层冲突都以它为准 |
| 风格 | 人格文件 > 准则 |
| 行为与权限 | 准则 > 人格文件 |
| 运营参数 | 配置 > 一切 |

### 5.3 准则文件双区

`data/prompt/Aliya.charter.md` 用显式标记划区：

```markdown
<!-- visible:start -->
# 你怎么做事
...
<!-- visible:end -->

## 实现约束（不进 prompt）
- 契约版本校验
- 事件去重与断点续订
- 工具超时与重试
```

`PromptLoader` 只取 `visible` 区；后面的内容只给开发者看。这样规则与实现说明同文件维护，不会漂移。

### 5.4 人格文件第二人称化

`data/prompt/Aliya.md` 里所有「玩家」改为「对方」，或改写为不出现该词的句子。例如：

- 「通过一台工业平板与来自 1000 年前地球的人类（玩家）通信」→「与来自 1000 年前地球的人类通信」
- 「玩家可以看到你的心率监测」→「对方可以看到你的心率监测」

不做运行时文本替换：替换规则会误伤隐喻，且难以测试。

### 5.5 预算与裁剪

1. 先压缩 `ConversationContext.summary`（若 `summary_degraded`，用本地摘要补）。
2. 再按时间倒序裁剪轮次，保留最近 6 轮。
3. 世界推演只取前 2 条假设与 2 个行动窗口。
4. 桌面只给可见卡片，最多 8 条。
5. 仍超预算时截断 `detail` / `hint`，绝不截断人格与准则。

---

## 6. 线路协议

### 6.1 分帧

- 一行一个 JSON 对象，UTF-8，`\n` 结尾。
- 单行上限 1 MiB；超限断开并 WARNING。
- 所有方法名与 `ControlPort` 对齐，snake_case。

### 6.2 消息

| 方向 | 消息 | 字段 |
| --- | --- | --- |
| Aliya → cosmos | `hello` | `token` / `contract_version` / `client_id` |
| cosmos → Aliya | `hello_ok` | `capabilities` / `server_time` / `timezone` |
| Aliya → cosmos | `call` | `id` / `method` / `params` |
| cosmos → Aliya | `result` | `id` / `ok` / `result` |
| cosmos → Aliya | `error` | `id` / `code` / `message` / `retriable` |
| Aliya → cosmos | `subscribe` | `topics: {topic: last_sequence}` |
| cosmos → Aliya | `subscribed` | `topics` / `sequences` |
| cosmos → Aliya | `inbound` | `envelope` |
| cosmos → Aliya | `event` | `event` |
| Aliya → cosmos | `ack_inbound` | `message_id` |
| 双向 | `ping` / `pong` | `at` |

### 6.3 方法

与 `ControlService` 一一对应：

```text
handshake, describe_desktop, get_agent, open_agent, patch_layout,
plan, invoke_agent, execute_plan, cancel_task, cancel_dispatch,
retry_task, list_tasks, get_presence, set_presence_visibility,
get_link_state, get_world_view, refresh_world, set_aliya_context,
deliver, notify, send_proactive, history
```

另加一个服务端方法 `now`，返回 `ClockService.now()` 与 `tz`，Aliya 用它校准本地时钟偏移。

### 6.4 鉴权与生命周期

- UDS 文件权限 `0600`；令牌从 `ALIYA_CONTROL_TOKEN` 读取，`hello` 时比对。
- 令牌不写 YAML，不落日志。
- Windows 回退 `127.0.0.1` TCP，令牌强制（v1 不实现）。
- `max_clients: 1`；第二个连接返回 `error{code="busy"}` 后关闭。
- 服务端 `stop()` 关闭所有会话与监听 socket，幂等。
- 启动前若 socket 文件已存在且无人监听，先删除；有人监听则报错退出。

### 6.5 重连与去重

- 客户端指数退避重连（1s → 30s，带抖动）。
- 重连后重新 `hello`，按本地 `cursor.json` 的每主题 `sequence` 发送 `subscribe`。
- 事件按 `event_id` 去重；`inbound` 按 `message_id` 去重。
- 出站消息带 `client_msg_id`；服务端按它去重（LRU 500），客户端在收到 `ack` 前保留在 outbox。
- `inbound` 客户端先把消息写入 `turns.jsonl`，再回 `ack_inbound`；服务端收到 ack 才从待确认集合移除。这样崩溃后最多重放一次，不会静默丢消息。

### 6.6 背压

- 服务端每客户端一个出站队列，上限 `outbox_limit`（默认 200）。
- 队列满时优先丢 `presence.changed` / `agent.progress`，**永不丢** `inbound` / `agent.result` / `dispatch.finished` / `agent.abnormal`。
- 客户端 outbox 满时，`send_message` 返回工具错误「现在发不出去」，由她决定是否等。

---

## 7. 运行时

### 7.1 启动

```text
读配置 → 初始化日志 → 连接 UDS → hello → 校验契约主版本
→ 加载人格与准则 → 打开记忆 → 订阅事件 → 启动巡检
```

人格或准则缺失、契约主版本不一致：启动失败，非零退出。

启动时检查 `turns.jsonl` 最后一条：若是用户消息且没有对应回复，补一个回合（幂等键为 `message_id`）。

### 7.2 入站与回合

```text
inbound
→ 按 message_id 去重
→ 进入会话队列（同一会话串行，全局同时一个回合）
→ 组装上下文
→ PromptBuilder 拼 L0-L4
→ LLM chat_tools（8 个工具）
→ 顺序执行 tool_calls，结果回填
→ 未超 6 轮且仍有 tool_calls 就继续
→ 无 send_message 且有正文时，正文作为一条消息兜底
→ 每条消息过 PostChecker
→ 写 turns.jsonl，更新 AliyaContext
```

### 7.3 事件策略

| 类别 | 事件 | 处理 |
| --- | --- | --- |
| 唤醒 | `agent.result`、`dispatch.finished`、`agent.abnormal` | 开汇报回合，带事件载荷 |
| 上下文 | `agent.progress`、`agent.state.changed`、`desktop.layout.changed`、`presence.changed`、`world.inference` | 只更新缓存；`world.inference` 按 `revision` 丢弃旧版本 |
| 链路 | `link.state.changed` | `down` 暂停出站；`up` 重放 outbox |
| 人 | `user.active` | 取消待发主动消息，不打断进行中的回合 |

`agent.progress` 默认不开回合；只有 `reply.progress_forwarding=always` 且用户明确在等时才转成 `notify`。

### 7.4 并发与队列

- 每会话一个队列，串行执行。
- 全局同时一个回合；v1 不做多会话并行（单用户场景）。
- 队列上限 32；满时丢最旧 `context` 事件，永不丢用户消息。
- 回合总时长上限 `reply.turn_timeout_seconds`（默认 120s）；超时取消工具调用并走 fallback。

### 7.5 出站 outbox

- `send_message` 生成 `client_msg_id`，写入 outbox，等待 `result.ok`。
- 断线时 outbox 保留；重连后按序重放。
- 重放时同 `client_msg_id` 由服务端去重，不会重复发给用户。
- outbox 上限 `outbox_limit`；满时拒绝新的发送并返回工具错误。

### 7.6 时间

- 握手拿 `server_time` 与 `timezone`，计算本地偏移。
- 巡检与静默判断用「校准后的服务端时间」，不用裸 `datetime.now()`。
- 偏移超过 5 分钟记 WARNING，并重新校准。

---

## 8. 工具面

| 工具 | 签名 | 映射 | 说明 |
| --- | --- | --- | --- |
| `send_message` | `(text: str)` | 回复回合 → `deliver`；主动回合 → `send_proactive`；事件回合 → `notify` | 可多次调用；每条单独投递 |
| `see_desktop` | `()` | `describe_desktop` | 返回 `id / name / summary / status / capabilities` |
| `dispatch_task` | `(task_text: str, agent_id?: str, params?: dict, hint?: str)` | `plan` + `execute_plan` | 返回 `accepted / clarify / rejected`、`dispatch_id`、任务摘要 |
| `task_status` | `(task_id?: str)` | `list_tasks` | 不传则返回最近任务；只给 label/status/progress |
| `cancel_task` | `(task_id: str)` | `cancel_task` | 用户改主意时 |
| `retry_task` | `(task_id: str)` | `retry_task` | 不可重试时返回原因 |
| `refresh_world` | `()` | `refresh_world` | 返回 snapshot、前 2 条假设、前 2 个窗口 |
| `remember` | `(note: str, kind: anchor / open_topic / unspoken / fact)` | 本地记忆 | 锚点、未收尾、只对你说过的话、稳定事实 |

### 8.1 send_message 的通道选择

由**回合来源**决定，不由模型选：

| 回合来源 | 映射 | 原因 |
| --- | --- | --- |
| 用户消息 | `deliver` | 用户问了，必须送达，不受节流 |
| 主动行动 | `send_proactive` | 受全部主动消息策略约束 |
| 事件汇报 | `notify` | 受免打扰约束，被拒时看 `deliver_after` |

这样她不需要理解三个通道的区别，也不会用错通道绕过节流。

### 8.2 主动回合的缩小工具集

主动回合只给：`send_message`、`see_desktop`、`task_status`、`refresh_world`、`remember`。

不给 `dispatch_task` / `cancel_task` / `retry_task`：没有人在场时不动有副作用的东西。想动手先在消息里问。

### 8.3 工具结果格式

工具结果一律是紧凑 JSON，字段用中文键名，避免模型把内部标识念出来：

```json
{"结果": "已接下", "任务": "改一下登录逻辑", "状态": "排队中"}
{"结果": "参数不够", "还差": ["目标文件"]}
{"结果": "现在排满了", "建议": "等一会儿再试"}
```

内部标识（`task_id` / `dispatch_id`）可以返回给模型用于后续调用，但出站检查会遮蔽。工具结果同时给展示名与内部 id；准则要求台词只用展示名，出站检查按当前桌面快照里的 agent id 列表做精确匹配遮蔽。

### 8.4 ask_before_write 的 v1 边界

`dispatch_task` 在 v1 **只要被调用就执行**（前提是 `plan.decision` 为 `single` / `parallel`）。`ask_before_write` 是提示词层面的约束：准则要求她在有副作用的操作前先问一句。真正的事务性确认（先存计划、下轮确认再执行）留到 v2，避免 v1 引入跨回合计划状态。

---

## 9. 记忆

### 9.1 归属

| 层 | 归属 | 内容 | 生命周期 |
| --- | --- | --- | --- |
| 工作上下文 | cosmos | 最近 20 轮、摘要、未回条数、`pinned_facts` | 会话级 |
| 长期记忆 | Aliya | 锚点、未收尾话题、只对你说过的话、跨会话摘要 | 跨进程、跨会话 |

### 9.2 文件

```text
agent/aliya/data/
  turns.jsonl        每条 user/aliya 消息追加一行
  summary.json       滚动摘要（≤ summary_chars）
  anchors.json       风信子 / 拉姆石 / 高马尾 / 兔子狗 / 海獭号
  open_topics.json   未收尾话题（≤ open_topics_limit）
  unspoken.json      只对你说过、没对 Kane/Ryoko 说的话
  facts.json         稳定偏好与约束
  cursor.json        每主题最后 sequence
```

### 9.3 注入

只用：cosmos 的最近轮次 + Aliya 的 `summary` + 锚点 + 未收尾话题 + 至多 3 条 `unspoken`。

不把 `turns.jsonl` 全量塞回 prompt；历史只用于恢复与压缩。

### 9.4 压缩

- 触发：本地轮次超过 `max_turns`，或 cosmos 摘要 `summary_degraded=true`。
- 方式：调 LLM 生成摘要；失败则截断到 `summary_chars` 并 WARNING。
- 锚点与 `unspoken` 永不参与摘要，只去重与裁剪上限。
- 写入用「临时文件 + `os.replace`」原子替换；JSON 损坏备份为 `.bad` 后从空开始。

### 9.5 隐私

- `unspoken` 不写进任何对外摘要，不进入世界推理的 `AliyaContext.unspoken` 以外的路径。
- 记忆文件不记录情绪数值与用户状态原始字段。
- `remember` 的 `note` 由模型生成，可能含用户原话；落盘前做一次密钥/PII 扫描（Aliya 侧自带小表：`sk-` / `ghp_` 等前缀、身份证与银行卡正则），命中则拒绝写入并 WARNING。不复用 cosmos 的脱敏实现，避免反向依赖。

---

## 10. 自主行动

### 10.1 三层门控

| 层 | 条件 | 默认 |
| --- | --- | --- |
| 时间 | 命中 `prefer_when` 之一 | 失眠且 >2h 没来；未收尾话题 >6h；刚修好东西；深夜刷信息 |
| 预算 | `min_gap_seconds` 与 `daily_soft_limit` 未超 | 3600s / 4 条 |
| 状态 | 链路 `up`；`readable` 不是「正在输入 / 暂时离开 / 离线深夜」；用户 5s 内没说话 | 按 `respect_presence_hint` |

连续两条主动消息没有回应时，间隔 ×2；用户回话后重置。

### 10.2 主动回合的提示词

在 L4 里加一段「现在没有人跟你说话」的上下文：

```text
现在是 {time}，距上次对话 {gap}。
你手头没有必须马上做的事。
{world_hypotheses}
{open_topics}
如果你想说话，就调用 send_message；如果不想，就什么都别做。
```

不给「必须说点什么」的指令。默认选择是沉默。

### 10.3 无动作

主动回合里没有调用 `send_message`：视为「今天不想说」，不产生消息，不消耗配额，只记 DEBUG。

### 10.4 phase

`AliyaContext.phase` 由运行时按简单规则更新，不调模型：

| 条件 | phase |
| --- | --- |
| 有任务在跑 | `maintenance` |
| 最近消息含危机词且用户情绪明显负面 | `crisis` |
| 本地时间 23:00–05:00 且无任务 | `rest` |
| 其余 | `daily` |

每回合结束后通过 `set_aliya_context()` 回传，供世界推理参考。

---

## 11. 出站检查

| 规则 | 检测 | 处理 |
| --- | --- | --- |
| 括号旁白 | `（...）` / `(...)` / `*...*` 包裹的动作 | 重写 |
| 工程词 | 玩家、用户、agent、模型、提示词、调度、面板、节点 | 重写 |
| 内部标识 | `tsk_` / `dsp_` / `evt_` / `ses_` / `req_` | 遮蔽 |
| 状态说破 | 检测到、系统显示、状态显示、readable | 重写 |
| 句尾标点 | 整条没有句号/问号/感叹号/省略号 | 记录 WARNING，不自动补 |
| 空消息 | 只有空白 | 丢弃 |

流程：

```text
检查 → 命中且可重写 → 把违规文本 + 规则名回给模型重写一次
→ 仍不通过 → 遮蔽标识、删掉命中片段 → 发送 + WARNING
→ 重写调用失败 → 直接走遮蔽降级
```

不记录正文，只记规则名、消息 id、长度。

---

## 12. 配置

```yaml
agent:
  control:
    socket_path: data/state/aliya.sock
    token_env: ALIYA_CONTROL_TOKEN
    max_clients: 1
    request_timeout: 15
    outbox_limit: 200
    max_line_bytes: 1048576

  aliya:
    persona:
      source: data/prompt/Aliya.md
      charter: data/prompt/Aliya.charter.md
      includes: []
    memory:
      enabled: true
      dir: agent/aliya/data
      max_turns: 200
      summary_chars: 600
      open_topics_limit: 5
      anchors: [风信子, 拉姆石, 高马尾, 兔子狗, 海獭号]
      private_threads: true
    initiative:
      enabled: true
      tick_seconds: 60
      min_gap_seconds: 3600
      daily_soft_limit: 4
      prefer_when: [失眠, 未收尾话题, 想被夸, 深夜]
      respect_presence_hint: true
      respect_link_state: true
      default_action: wait
    reply:
      max_rounds: 6
      turn_timeout_seconds: 120
      max_chars_per_message: 1200
      fallback_text: 嗯……我这边卡了一下，等我一下。
      progress_forwarding: on_wait
      post_checks: [narration, engineering_terms, internal_ids, presence_leak]
    tools:
      prefer_agents: []
      ask_before_write: true
      max_parallel_dispatch: 2
    world:
      consume: true
      max_hypotheses_used: 2
      trust_threshold: 0.5
      provide_context: true
```

配置模型挂在 `Settings.agent` 下：

```python
class AgentControlSettings(_AgentConfigNode):
    socket_path: str = "data/state/aliya.sock"
    token_env: str = "ALIYA_CONTROL_TOKEN"
    max_clients: int = Field(default=1, ge=1)
    request_timeout: float = Field(default=15, gt=0)
    outbox_limit: int = Field(default=200, ge=1)
    max_line_bytes: int = Field(default=1_048_576, ge=1024)

class AgentAliyaSettings(_AgentConfigNode):
    persona: AgentAliyaPersonaSettings = ...
    memory: AgentAliyaMemorySettings = ...
    initiative: AgentAliyaInitiativeSettings = ...
    reply: AgentAliyaReplySettings = ...
    tools: AgentAliyaToolsSettings = ...
    world: AgentAliyaWorldSettings = ...

class AgentSettings(_AgentConfigNode):
    control: AgentControlSettings = AgentControlSettings()
    aliya: AgentAliyaSettings = AgentAliyaSettings()
    cosmos: AgentCosmosSettings = AgentCosmosSettings()
```

`data/config/cosmos.yaml` 追加 `agent.control` 与 `agent.aliya` 两段；`agent/cosmos/config/agent-settings.example.yaml` 保留，`agent/aliya/config/agent-settings.example.yaml` 改为与模型字段对齐。

---

## 13. 错误与降级

| 场景 | 行为 |
| --- | --- |
| 连不上 cosmos | 指数退避重连；出站进 outbox；入站等重连后由服务端补 |
| 契约主版本不一致 | 不重连，报明确错误并非零退出 |
| RPC 超时 | 读类重试 1 次；写类靠 `client_msg_id` 去重后再试 |
| `AgentBusyError` | 工具结果写「现在排满了，要等一会」 |
| `invalid_params` | 工具结果写「还差哪些参数」 |
| LLM 失败 | 重试 1 次；用户回合发 `fallback_text`；事件回合丢弃；主动回合放弃 |
| 6 轮工具超限 | 追加一次「不带工具、只收尾」的调用；仍无消息走 fallback |
| 出站检查重写失败 | 遮蔽标识、删掉命中片段后发送，WARNING |
| 记忆写失败 | 记 ERROR，不阻塞回复；损坏 JSON 备份后从空开始 |
| 人格/准则缺失 | 启动期 fail fast |
| 第二个客户端 | 拒绝，`code="busy"` |
| 单行超 1 MiB | 拒绝并断开，WARNING |

所有错误继承 `CosmosError` 或 `AliyaError`，带 `code` / `message` / `retriable`。

---

## 14. 测试与验收

### 14.1 单元测试

| 模块 | 用例 |
| --- | --- |
| PromptBuilder | 四层齐全；预算裁剪顺序；人格/准则不截断 |
| PersonaLoader | 文件缺失 fail fast；`includes` 顺序；`visible` 区解析；缓存失效 |
| MemoryStore | 追加、压缩、锚点不丢、`unspoken` 不进摘要、损坏恢复 |
| TurnRunner | 6 轮上限；无 `send_message` 正文兜底；工具错误转可读结果；会话串行 |
| PostChecker | 六条规则；重写一次；遮蔽降级；不记正文 |
| Initiative | 三层门控；连续未回应 ×2；每日上限；无消息不占配额 |
| RemoteControl | 分帧；重连；outbox 重放；`client_msg_id` 去重；版本不一致退出 |

### 14.2 集成测试

真 UDS + 真 cosmos 容器 + 假 LLM：

- 聊天：用户消息进 → 回一条 → 标点原样、无工程词。
- 派活：`dispatch_task` → `agent.result` → 汇报回合 → 文本不含 `tsk_`。
- 断链：服务端停 → outbox 保留 → 恢复后按序只发一次。
- 第二客户端：被拒绝，第一个不受影响。
- 超长行：断开并 WARNING。

### 14.3 验收标准

1. cosmos 15 个服务健康；Aliya 后起能连上；两边 `SIGINT` 都以 0 退出。
2. 契约主版本不一致时 Aliya 非零退出，错误信息可读。
3. 全量测试无真实网络调用；`ruff` 与 `basedpyright` 全绿。
4. `turn / transport / memory / postcheck` 四条主链路覆盖率 ≥ 80%。

---

## 15. 实施顺序

| 阶段 | 内容 | 文件 | 退出判据 |
| --- | --- | --- | --- |
| M1 契约与配置 | RPC 模型、`AgentControlSettings` / `AgentAliyaSettings`、YAML 接线 | `agent/cosmos/contracts/rpc.py`、`core/config/settings.py`、`data/config/cosmos.yaml` | 配置能加载；RPC 模型 JSON round-trip |
| M2 cosmos server | `ControlServerService`、鉴权、会话、推送队列、`client_msg_id` 去重、`now` | `agent/cosmos/services/control_server.py`、`assembly.py` | 服务健康；单客户端可握手；第二客户端被拒 |
| M3 Aliya 客户端 | `ControlPort`、`RemoteControl`、重连、outbox、cursor | `agent/aliya/ports.py`、`transport.py` | 断链重连后消息不丢；事件断点续订 |
| M4 Prompt | 人格第二人称化、准则双区、loader、builder | `data/prompt/Aliya.md`、`Aliya.charter.md`、`agent/aliya/prompt/` | 四层拼装正确；预算裁剪可测 |
| M5 回合与工具 | `TurnRunner`、8 个工具、`PostChecker` | `agent/aliya/turn/` | 假 LLM 下聊天/派活/汇报三条路径通过 |
| M6 记忆 | `MemoryStore`、压缩、恢复 | `agent/aliya/memory/` | 200 轮后摘要生成；锚点不丢 |
| M7 自主行动 | 巡检、三层门控、主动提示词、phase | `agent/aliya/initiative.py`、`runtime.py` | 门控逐条测试；无消息不占配额 |
| M8 装配与验收 | `app.py`、端到端、文档同步 | `agent/aliya/app.py`、`README.md` | 五条流程通过；静态检查全绿 |

每个阶段独立提交；M2 是唯一会改 cosmos 行为的阶段，失败可单独回退。

---

## 16. 风险与未决

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| UDS 在 Windows 不可用 | 无法启动 | v1 只支持 POSIX；Windows 回退 TCP 放到后续里程碑 |
| `client_msg_id` 去重需改 cosmos | M2 范围扩大 | 作为 M2 的一部分实现；LRU 500，不改契约字段 |
| 事件缺口 >100 | 需走快照 + 实时 | 复用 `ControlService` 现有补偿逻辑；客户端按 `event_id` 去重 |
| 主动回合仍可能打扰 | 人设与产品风险 | Aliya 侧限额比 cosmos 更严；无消息不占配额；`urgency` 默认 0.5 |
| 没有真实任务 agent | 派活只能对 `echo` 演练 | 不阻塞实现；E2E 用 `echo`；真实 agent 由扩展方接入 |
| `ask_before_write` 只是提示词约束 | 有副作用的操作可能被直接执行 | v1 记录风险；v2 加 `confirm` 参数与跨回合计划状态 |
| 情绪信号默认不可用 | 准则若写「能看情绪」会幻觉 | 准则只写四类线索；不提供 mood 读取入口 |

未决：

1. `refresh_world()` 在主动回合是否允许？当前设计允许，因为只读。
2. 多会话并行是否要做？当前单会话串行，够用。
3. 是否引入 `DecisionService` 做主动门控？当前不引入，门控是确定性规则 + LLM 最终否决。

---

## 17. 文档同步清单

| 文件 | 改动 |
| --- | --- |
| `agent/aliya/README.md` | 重写第 5 节主循环（去掉 intent）；第 8 节测试改成工具调用；第 4 节能力表对齐 `ControlPort`；补传输与进程拓扑 |
| `data/prompt/Aliya.md` | 所有「玩家」改为「对方」或改写为无该词的句子 |
| `data/prompt/Aliya.charter.md` | 新建：可见规则 + 实现约束双区 |
| `data/config/cosmos.yaml` | 追加 `agent.control` 与 `agent.aliya` |
| `agent/cosmos/README.md` | 服务数 14 → 15；补 `ControlServerService` |
| `agent/cosmos/docs/01-architecture.md` | 补控制面服务端与进程拓扑 |
| `agent/cosmos/docs/09-control-api.md` | 补 `client_msg_id`、`now`、RPC 映射 |
| `README.md` | 补启动顺序：先 cosmos，后 aliya |
| `agent/aliya/config/agent-settings.example.yaml` | 与 `AgentAliyaSettings` 字段对齐 |

---

## 18. 附：准则可见区大纲

实现时 `Aliya.charter.md` 的 `visible` 区按这个顺序写，每条都要能对上人格文件：

1. 你是谁、在哪、对面是谁。
2. 你怎么说话：像发微信、简短、不写旁白、保留标点、可以连发多条。
3. 你能做什么：回话、看平板上的工具、派人去做事、查进度、取消、重试、看一眼现在的情况、记住事情。
4. 你不能做什么：改注册表、改调度策略、看别人的原始状态、解释时间同步、主动提 Doll 与寿命。
5. 什么时候主动：失眠、未收尾的话、刚修好东西、深夜；没有理由就别说。
6. 结果怎么汇报：先给结论，再补细节；失败先承认，再解释；不提内部标识。
7. 被拒绝怎么办：不重试刷屏，等下一次机会。
8. 状态怎么用：只影响行为，不进台词；不说「我检测到你」。
