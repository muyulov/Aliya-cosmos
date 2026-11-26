# 03 Agent 桌面展示规范

agent 桌面是 cosmos 向 Aliya 呈现子 agent 的唯一入口，形态对应手机桌面上的 APP 列表：每个子 agent 是一张卡片，有自己的图标、名称、简介和状态。Aliya 像打开 APP 一样启动它，也能直接说一句话由 cosmos 自动匹配。

**桌面是给 Aliya 看的面板，不是用户界面。** 用户看不到 agent 桌面，也看不到子 agent 的名称与状态。

对 Aliya 而言，这不是「agent 注册表」，而是**平板上能做的事**。她认知里只有工具与功能，没有 agent、没有模型、没有调度队列。因此展示层的文案必须翻译成她的语言：

| 不要出现 | 换成 |
| --- | --- |
| agent / 子 agent / Agent | 直接说功能，或省略主语 |
| 模型 / 提示词 / token / 上下文 | 不提 |
| 调度 / 队列 / 并发 | 不提；需要时说「正在忙」 |
| 任务 id / 状态码 | 不提；说「还在弄」「没成」 |

`card.id` 是工程标识，`card.name` 才是她看到的名字。两者分离，正是为了让内部叫法可以随便改，而她看到的永远是稳定的一个词。

---

## 1. 桌面数据模型

```python
class DesktopSnapshot(BaseModel):
    revision: int                      # 布局版本号，任何布局变更 +1
    generated_at: datetime
    groups: list[DesktopGroup]
    entries: list[DesktopEntry]        # 扁平列表，按展示顺序排列
    summary: str                       # 整体可解读摘要，一句话
```

```python
class DesktopGroup(BaseModel):
    key: str                           # "dev" / "general"
    title: str                         # "开发工具"
    order: int = 100
    collapsed: bool = False            # Aliya 可折叠，仅影响呈现
```

```python
class DesktopEntry(BaseModel):
    card: AgentCard                    # 注册元数据，见 02-registry.md
    status: AgentStatus                # idle / queued / running / stalled / abnormal / disabled
    task: AgentTaskView | None         # 当前或最近一次任务的视图
    readable: ReadableState            # 可解读状态，见第 4 节
    badge: str | None = None
```

```python
class AgentTaskView(BaseModel):
    task_id: str
    label: str                         # 任务的一句话描述，如 "改写 logger 的 3 个函数"
    progress: float | None             # 0–100；None 表示无法量化（心跳型任务）
    step: str | None                   # 当前步骤，如 "解析 AST"
    started_at: datetime
    elapsed_seconds: float
    queue_depth: int = 0               # 排队中的任务数
```

领域值：

```python
type AgentStatus = Literal["idle", "queued", "running", "stalled", "abnormal", "disabled"]
```

| 状态 | 含义 | 是否可调度 |
| --- | --- | --- |
| `idle` | 空闲，可立即接受任务 | 是 |
| `queued` | 有任务排队（`queue=true` 且并发已满） | 是（进队列） |
| `running` | 正在执行 | 是（受 `max_concurrent` 约束） |
| `stalled` | 心跳超时（默认 30s），可能卡住 | 是，但提交前需提示风险 |
| `abnormal` | 上次任务失败或健康检查不通过 | 是，重试以新 `task_id` 开始 |
| `disabled` | 被部署配置禁用 | 否 |

`stalled` 与 `abnormal` 的区别：前者是「现在可能卡住了」，后者是「上一次确实出错了」。二者可以同时成立，此时以 `abnormal` 为准（桌面显示 `abnormal`，`readable` 里补充「当前无响应」）。

---

## 2. 卡片展示信息

桌面上每张卡片固定呈现四行：

```text
✏️  编辑台  ·  ● 忙
改写仓库内的文件
正在改写 logger，进度 60%
已用 1 分 12 秒 · 队列 1
```

| 位置 | 数据来源 | 约束 |
| --- | --- | --- |
| 图标 | `card.icon` | emoji 或资源引用；缺失用 `id` 首字母 |
| 名称 | `card.name` | 超 12 字截断加省略号 |
| 状态点 | `status` | 颜色：`idle` 灰、`running` 蓝、`queued` 青、`stalled` 黄、`abnormal` 红、`disabled` 深灰 |
| 副标题 | `card.summary` | 固定一行，不随状态变化 |
| 状态摘要 | `readable.summary` | 一句话，≤ 24 字 |
| 状态补充 | `readable.meta` | 数值型信息（耗时、队列、进度），可为空 |

**副标题与状态摘要分工**：副标题说明「这个 agent 是干什么的」（静态），状态摘要说明「它现在怎么样」（动态）。不要在状态摘要里重复能力描述。

---

## 3. 状态监控面板

对应游戏中 COSMOS 面板的仪表盘。仪表盘的价值不在数字，而在**一眼判断该不该做点什么**。

### 3.1 呈现指标

| 指标 | 来源 | 呈现方式 |
| --- | --- | --- |
| 任务进度 | `ctx.report(progress=...)` | 进度条；无量化时显示脉冲动画 + 当前步骤 |
| 已用时长 | `started_at` 差值 | `1 分 12 秒`，超 10 分钟转 `12 分钟` |
| 排队深度 | `AgentRuntimeService` | `队列 1`，为 0 时隐藏 |
| 心跳新鲜度 | `last_heartbeat_at` | 正常不显示；超过阈值显示「N 秒前有心跳」 |
| 资源占用（可选） | 子 agent 上报的 `metrics` | `CPU 38% / 内存 210MB`，未上报则不显示 |
| 告警 | `abnormal` + `last_error` | 红色角标 + 错误类型，点击看详情 |

资源占用是**可选指标**：进程内子 agent 拿不到有意义的 CPU/内存，不要为凑指标编造数据。缺失时整块隐藏，比显示 0% 更诚实。

### 3.2 趋势而非快照

单一数值无法反映变化。面板额外给趋势：

| 趋势字段 | 取值 | 计算方式 |
| --- | --- | --- |
| `progress_trend` | `advancing` / `stalled` / `unknown` | 最近 60s 内 `progress` 是否增长 |
| `duration_ratio` | `float` | 本次耗时 / 该 agent 历史 P50 耗时，> 2 视为偏慢 |
| `error_streak` | `int` | 连续失败次数，成功后清零 |

`duration_ratio` 需要历史数据，首次运行时为 `None`，不显示「比平时快/慢」的结论。**没有基线就不要下判断**，这是可解读性设计的第一条纪律。

### 3.3 告警

| 事件 | 推送 | 冷却 |
| --- | --- | --- |
| 进入 `abnormal` | 立即推 `agent.abnormal` | 同一 agent 5 分钟内不重复推 |
| 进入 `stalled` | 立即推一次 | 10 分钟 |
| 任务超时 | 随 `agent.abnormal` 一起推，`reason="timeout"` | —— |
| 恢复正常 | 不推送，仅刷新桌面 | —— |

恢复不推送：Aliya 需要知道「出事了」，不需要知道「没事了」。桌面刷新足够。

---

## 4. 可解读状态

对应游戏中「心率不只是数字，而是 Aliya 情绪状态的反映」。cosmos 给每个 agent 派生的不是原始指标堆砌，而是一句可以直接读的结论。

```python
class ReadableState(BaseModel):
    summary: str        # 一句话结论，≤ 24 字
    meta: str | None    # 数值补充，≤ 20 字
    tone: ReadableTone  # calm / busy / strained / alarming / muted
    hint: str | None    # 建议动作，≤ 30 字；没有建议时为 None
```

```python
type ReadableTone = Literal["calm", "busy", "strained", "alarming", "muted"]
```

`tone` 用于配色与排序，`hint` 只给 Aliya 参考、不是指令。

### 4.1 派生规则

按优先级从上到下匹配第一条：

| 条件 | summary | tone | hint |
| --- | --- | --- | --- |
| `disabled` | `已停用` | `muted` | `需要在配置里启用` |
| `abnormal` 且 `error_streak ≥ 2` | `连续 2 次失败` | `alarming` | `建议先看错误详情再重试` |
| `abnormal` 且 `reason=timeout` | `上次任务超时（超过 300 秒）` | `alarming` | `可以拆小任务后重试` |
| `abnormal` | `上次任务失败：{错误类型}` | `alarming` | `可重试` |
| `stalled` | `{N} 秒没有心跳，可能卡住` | `strained` | `可取消后重新提交` |
| `running` 且 `duration_ratio > 2` | `正在{step}，比平时慢` | `strained` | `暂无建议，可继续等待` |
| `running` 且 `progress` 非空 | `正在{step}，进度 {progress}%` | `busy` | `None` |
| `running` 且 `progress` 为空 | `正在{step}` | `busy` | `None` |
| `queued` | `有 {n} 个任务排队` | `busy` | `None` |
| `idle` 且 `last_used_at` 在 10 分钟内 | `空闲，刚完成一次{label}` | `calm` | `None` |
| `idle` 且用过 | `空闲，最近使用在 {相对时间}` | `calm` | `None` |
| `idle` 且从未用过 | `空闲，还没用过` | `calm` | `None` |

派生规则集中在一处实现（`DesktopService._readable()`），不散落在渲染层。新增规则时优先在表尾追加，避免改动既有文案导致 Aliya 侧的行为漂移。

### 4.2 文案纪律

| 纪律 | 反例 |
| --- | --- |
| 说结论，不说字段 | 不写 `status=running, progress=60` |
| 给相对量，不给孤立数字 | 不写 `耗时 72 秒`，写 `已用 1 分 12 秒`（面板带上下文时可给） |
| 没有基线不做比较 | 不写 `比平时快`，除非 `duration_ratio` 有值 |
| 不猜测原因 | 不写 `可能因为文件太大`；写 `{N} 秒没有心跳，可能卡住` 这类有观测依据的推断 |
| 建议是参考不是指令 | `hint` 写「可取消后重新提交」，不写「立即取消」 |
| 不使用情绪词描述 agent | 不写「Edit agent 很沮丧」——agent 没有情绪，只有状态 |

最后一条尤其重要：**可解读 ≠ 拟人化**。心率之所以能反映情绪，是因为有生理学依据；agent 的状态只能反映执行情况，不能编造心理活动。真正需要情绪的是用户状态线索（见 [04-presence.md](04-presence.md)），那里的情绪信号必须有真实证据来源，而且默认不摆在面板上。

### 4.3 示例

```json
{
  "card": {"id": "edit", "name": "编辑台", "icon": "✏️", "summary": "改写仓库内的文件"},
  "status": "running",
  "task": {
    "task_id": "tsk_01H...",
    "label": "改写 logger 的 3 个函数",
    "progress": 60,
    "step": "改写 base.py",
    "started_at": "2026-11-07T10:12:03+08:00",
    "elapsed_seconds": 72.4,
    "queue_depth": 1
  },
  "readable": {
    "summary": "正在改写 base.py，进度 60%",
    "meta": "已用 1 分 12 秒 · 队列 1",
    "tone": "busy",
    "hint": null
  },
  "badge": null
}
```

---

## 5. 排序与分组

### 5.1 排序规则

组内排序按以下键依次比较：

1. `pinned` 为 `true` 的排前（组内）。
2. `desktop.order` 小的在前。
3. `id` 字典序。

**运行中的 agent 不自动置顶。** 位置稳定比「重要的浮上来」更有用：Aliya 记住的是位置（第三个是 Edit agent），位置会跳动就会点错。运行状态通过状态点与 `tone` 表达，不靠位置表达。

### 5.2 分组

| 分组键 | 标题 | 建议收纳 |
| --- | --- | --- |
| `dev` | 开发工具 | 编辑台、检索台、运行台 |
| `web` | 联网 | 联网台等抓取公开信息的 agent |
| `media` | 内容处理 | 文档、图片、音视频类 agent |
| `general` | 通用 | 未指定分组的 agent；回声台等联调用工具 |

仓库当前的内置工具是 `edit`（dev / order 10）、`search`（dev / 20）、`run`（dev / 30）、
`web`（web / 10）、`echo`（general / 100），分组顺序由 `desktop.groups` 决定。

分组由配置声明，未声明的 group 落 `general` 并 WARNING。分组的 `title` 与 `order` 由 `desktop.groups` 配置，`collapsed` 是 Aliya 的运行时偏好，不写回配置。

### 5.3 布局变更

```python
class DesktopLayoutPatch(BaseModel):
    reorder: list[AgentId] | None = None          # 完整的新顺序（仅组内）
    move: dict[AgentId, GroupKey] | None = None   # 跨组移动
    pin: dict[AgentId, bool] | None = None
    collapse: dict[GroupKey, bool] | None = None
```

补丁是幂等的：重复应用同一补丁结果不变。应用成功后 `revision + 1` 并广播 `desktop.layout.changed`。Aliya 的布局偏好持久化到 `data/state/desktop.json`（运行数据，不进配置文件）。

---

## 6. 刷新机制

三层组合，避免「轮询数据库」和「事件风暴」两个极端：

| 层 | 触发 | 频率 | 作用 |
| --- | --- | --- | --- |
| 事件驱动 | `agent.state.changed` / `agent.progress` / `agent.result` / `desktop.layout.changed` | 实时 | 主路径，逐条刷新受影响条目 |
| 节流合并 | 同上，但进度类事件 | 最快 1s 一次 | 同一 agent 的多次进度合并成一次，只保留最新 |
| 全量对账 | 定时器 | 30s | 防止事件丢失导致面板长期偏离真实状态 |

规则：

- 进度上报按 `agent_id` 做尾沿节流（trailing throttle），保留最后一次，不丢终态。
- 全量对账只发差异（`diff`），不重发整份快照；无差异不发事件。
- Aliya 未订阅桌面时，`readable` 按需计算（懒派生），不占用周期资源。
- 面板断开超过 5 分钟后重连，先推一份全量快照再续增量。

---

## 7. 交互行为

| 操作 | 接口 | 行为 |
| --- | --- | --- |
| 查看详情 | `ControlService.get_agent(agent_id)` | 返回卡片 + 运行态 + 最近 5 次任务摘要 |
| 打开（进入待命） | `open_agent(agent_id)` | 仅做 UI 层选中，不启动任务；返回可填的参数 schema |
| 点击调用 | `invoke_agent(agent_id, params)` | 走显式调用路径，见 [05-dispatch.md](05-dispatch.md#2-统一管线) |
| 取消任务 | `cancel_task(task_id)` | 子 agent 收到 `CancelledError`；5s 未退出标记 `stalled` |
| 重试 | `retry_task(task_id)` | 仅 `execution.retryable=true` 且上次为失败时可用；生成新 `task_id` |
| 启用 / 禁用 | `set_agent_enabled(agent_id, enabled)` | 仅管理面；Aliya 无此权限 |
| 排序 / 分组 | `patch_layout(patch)` | Aliya 可调 |
| 查看历史 | `list_tasks(agent_id, limit=20)` | 摘要级，含时长与结果状态，不含正文 |

**「打开」与「调用」必须分开**：打开只是选中，调用才产生副作用。合并成一个动作会让「点错了」变成「真的改了文件」。

---

## 8. 与用户状态线索的对称性

| 维度 | agent 桌面 | 用户状态线索 |
| --- | --- | --- |
| 谁看 | Aliya | Aliya |
| 看什么 | 子 agent 的运行状态 | 用户的状态与活跃度 |
| 原始数据 | 进度、耗时、心跳、错误 | 在线、活跃度、输入、情绪、环境 |
| 可解读产物 | `ReadableState` | `ReadablePresence` |
| 变化推送 | `agent.state.changed` | `presence.changed` |
| 用途 | 决定用哪个 agent、要不要干预 | 决定何时说话、要不要等待 |

两块面板共用同一套「原始字段 + `readable` 派生 + 变化事件」的结构与文案纪律。这不是巧合：**双向感知对称性要求两侧的呈现质量对等**，否则 Aliya 就成了只能被观察、无法观察别人的角色。

但要注意方向上的差别：agent 状态是**系统内部事实**，怎么显示都不涉及谁的隐私；用户状态是**另一个人的事**，而 Aliya 自己就因为「被窥探的感觉」要求关掉了心率监测。所以两块面板的呈现粒度并不对等：

| | agent 桌面 | 用户状态 |
| --- | --- | --- |
| 粒度 | 可以细到进度、心跳、错误类型 | 只到「在不在、忙不忙、适不适合说话」 |
| 情绪 | 没有情绪，也不拟人化 | 有，但默认不进常驻项，需有文本证据 |
| 关闭权 | Aliya 可折叠、排序 | Aliya 可要求关闭任一字段，用户可整体关闭采集 |

详细规范见 [04-presence.md](04-presence.md)。
