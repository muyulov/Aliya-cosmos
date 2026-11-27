# 代码边界

## 1. 依赖方向

```text
agent.aliya  ──►  agent.cosmos.contracts / ControlPort  ──►  core.*
```

`core` 内部同样单向：

```text
service → (config, logger)
embedding / llm / decision → (config, logger, service)
agent → (config, llm, logger)
agent.cosmos → core
```

反向不成立。`service` 层不引用任何框架，可被 CLI、定时任务、测试直接复用。

唯一的例外是 `core/service/registry.py`：它要 import 具体服务类才能完成注册，因此这个模块反向依赖 `embedding` / `llm` / `decision`；它也因此不挂在 `core/service/__init__.py` 上（会成环），装配入口直接从 `core.service.registry` 取。

## 2. 与 `agent/aliya` 的边界

`agent/aliya` 是独立进程，通过 UDS 控制面连接 cosmos。接口分工：

| 由 cosmos 提供 | 由 Aliya 自己实现 |
| --- | --- |
| `TerminalService.ingest()` 接收用户消息 | 怎么理解、怎么回应 |
| `ConversationContext`（历史摘要、用户状态摘要） | 怎么使用这些上下文 |
| `ControlService` / `ControlPort` 全部方法（桌面、调度、订阅） | 何时调用、调用哪个 |
| `WorldInference`（状态摘要、假设、行动窗口） | 采纳还是忽略 |
| `TerminalService.send_to_user()` / `notify()` | 说什么、什么时候说 |

代码层面的硬约束：

| 约束 | 理由 |
| --- | --- |
| Aliya 的生产路径不 import `agent.cosmos.services.*`，只依赖 `agent.cosmos.contracts` 与 `ControlPort`；`InProcessControl` 仅用于测试 | 换实现不破坏 Aliya |
| Aliya 不直接持有子 agent handler 引用 | 否则绕过调度与状态监控 |
| Aliya 不直接写 `ConversationContext` 的存储，只通过 `TerminalService` 提交 | 避免两份历史打架 |
| cosmos 不 import `agent.aliya.*` | 依赖方向必须是 Aliya → cosmos → core |

生产环境 Aliya 通过 `ControlServerService` 的 UDS/NDJSON 端点调用 `ControlService`；测试可用 `InProcessControl` 直接委托。反向依赖只允许一种：cosmos 通过 `MessageBus` 端点名称（`"aliya"`）投递消息，这是运行期解耦，不是代码依赖。

## 3. 错误分层

| 类别 | 处理方式 | 例子 |
| --- | --- | --- |
| 输入问题 | 结构化回填，让调用方自我纠正 | 参数不合法、agent_id 不存在于当前桌面、消息为空 |
| 运行期失败 | 记录 + 事件推送 + 降级 | 子 agent 超时、世界推理超时、推送失败 |
| 配置/编程错误 | fail fast，启动期抛错 | 卡片重名、依赖缺失、桌面引用不存在的 agent |

错误树统一继承 `CosmosError`，调用方可一次 `except CosmosError` 兜住：

| 类型 | 触发条件 |
| --- | --- |
| `AgentRegistrationError` | 卡片字段非法、重名、能力声明冲突 |
| `AgentNotFoundError` | 引用了未注册或已禁用的 agent |
| `AgentInvokeError` | 提交任务失败（handler 不可用、参数不符合 schema） |
| `AgentBusyError` | 超过并发上限或该 agent 不允许并发 |
| `DispatchError` | 无法生成调度计划（无候选、置信度过低且未指定目标） |
| `PresenceError` | 状态字段非法、采集开关冲突 |
| `PresencePrivacyError` | 请求了未授权字段 |
| `WorldBridgeError` | 输入组装失败、推理端口未配置 |
| `MessageDeliveryError` | 投递失败且重试耗尽 |

配置类错误继承 `core.service.ServiceError` 的语义（启动失败、进程退出）；运行期错误不杀进程，只影响单条链路。

## 4. 明确不做的事

| 不做 | 交给谁 |
| --- | --- |
| 理解用户意图的语义细节、组织回应内容 | Aliya |
| 执行编辑、读写文件等业务操作 | 子 agent |
| 修改或解释世界推理结论 | 不改写，原样转发 |
| 采集用户在设备上的隐私数据（剪贴板、屏幕、位置） | 不采集 |
| 向用户回显用户状态（"检测到你情绪低落"） | 永不 |
| 记忆长期人格设定与关系进展 | Aliya |
| 承担子 agent 内部的 ReAct 循环 | 子 agent，用 `core.agent` 自行组装 |
| 向 Aliya 解释时间同步原理、装置构成、信号来源 | 永不（人设要求维持信息差） |
| 让 Aliya 看到「玩家 / 用户 / agent / 模型 / 调度」这类工程词 | 永不，用她的语言 |
| 改写她的语气：合并短句、补标点、加旁白 | 永不，见[通信终端](../agents/cosmos/terminal.md#6-回传用户) |
