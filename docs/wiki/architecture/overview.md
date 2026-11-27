# 架构总览

本章定义 cosmos 的角色边界与组件划分，并说明它与 Aliya、子 agent 的分工。通信方式与消息流向见 [cosmos 通信拓扑](cosmos-topology.md)，代码级约束见[代码边界](boundaries.md)。

## 三层职责

| 参与方 | 是什么 | 负责 | 不负责 |
| --- | --- | --- | --- |
| 用户（玩家） | 对话的另一端 | 发起聊天、下达任务、查询状态 | —— |
| cosmos | 主 agent（Supervisor / Orchestrator） | 通信终端、调度中枢、状态感知 | 不执行业务、不代替 Aliya 说话、不做最终决策 |
| Aliya | 上层控制者与对话主体 | 理解用户、决定是否调度、组织回应、自主行动 | 不直接接触子 agent 运行时、不直接向用户投递 |
| 子 agent | 执行单元（如 Edit agent） | 执行具体任务、上报进度、返回结果 | 不接触用户、不访问对话上下文、不调度其他 agent |
| 世界推理 agent | Aliya 的后台参谋 | 推演世界状态、给出可能性与行动窗口 | 不接收用户消息、不触发调度、不占桌面 |

一句话：**Aliya 想，cosmos 转，子 agent 做。**

## 为什么 cosmos 与 Aliya 分开

| 关注点 | cosmos | Aliya |
| --- | --- | --- |
| 与用户的关系 | 通道，不承担人格 | 对话主体，承担人格与情绪 |
| 与子 agent 的关系 | 注册、调度、监控 | 决策「用不用、用哪个、什么时候用」 |
| 状态数据 | 采集、聚合、呈现 | 消费，用于判断与自主行动 |
| 世界推理 | 提供输入、转发输出 | 唯一决策方，可采纳或忽略 |

分开的直接收益：Aliya 换人格、换推理策略甚至换模型，都不影响调度链路；cosmos 换传输方式（进程内 → IPC/WebSocket）也不影响 Aliya 的接口。

## 组件与依赖

| 组件 | 关键职责 | 依赖 |
| --- | --- | --- |
| `AgentRegistryService` | `AgentCard` 注册、查询、能力检索 | `AgentCosmosSettings` |
| `AgentRuntimeService` | 任务生命周期、进度、取消、超时、幂等去重 | `AgentRegistryService` |
| `DesktopService` | 布局、分组、排序、可见性、桌面快照 | `AgentRegistryService` |
| `PresenceService` | 用户状态采集、聚合、派生表述、变化事件 | `ClockService` |
| `ConversationService` | 上下文摘要、分段、脱敏 | `LLMService` |
| `DispatchService` | 统一调度管线、匹配、并行、降级 | 上面四个 |
| `WorldBridgeService` | 输入组装、结果转发、缓存 | `ClockService`、`PresenceService`、`AgentRuntimeService`、`AgentRegistryService`、`BusService`、`LLMService` |
| `TerminalService` | 双向消息、推送、主动消息节流 | `ConversationService`、`PresenceService` |
| `WebSocketChannelService` | 浏览器用户通道：WebSocket JSON 收发、输入状态上报、按会话推送 | `TerminalService`、`AgentSettings` |
| `ControlService` | Aliya 控制面 | 以上全部 |
| `ControlServerService` | 控制面 UDS/NDJSON 端点：握手、RPC、入站与事件推送 | `ControlService`、`ClockService`、`AgentSettings` |

依赖成链：`ControlServerService → ControlService → TerminalService → DispatchService → RuntimeService → RegistryService`。启动顺序由 `ServiceManager` 按类型自动分层，不要手工排序。

`AgentRegistryService` 不依赖任何 LLM 服务：注册与查询是纯数据操作，测试时可脱离模型单独跑。

各服务的详细规范见：

- [注册机制](../agents/cosmos/registry.md)
- [任务分发](../agents/cosmos/dispatch.md)
- [通信终端](../agents/cosmos/terminal.md)
- [用户状态线索](../agents/cosmos/presence.md)
- [世界推理接入](../agents/cosmos/world-reasoning.md)
- [控制接口](../agents/cosmos/control-api.md)
