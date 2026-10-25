# cosmos agent 开发文档

- 文档版本：0.1
- 状态：目标设计，代码待实现
- 面向读者：实现、接入与维护 cosmos 及子 agent 的工程师
- 设计参考：《彼方的她-Aliya》中的 cosmos 系统

cosmos 是 aliya-cosmos 的主 agent，承担 Supervisor / Orchestrator 角色。Aliya 通过 cosmos 发起请求、查看状态、干预执行；cosmos 负责意图识别、任务拆解、路由调度与桌面展示；子 agent 负责具体业务执行。cosmos 自身不修改文件、不执行 shell、不直接访问外部业务系统。

## 1. 设计来源映射

| 游戏设定 | 本项目映射 |
| --- | --- |
| 玩家接管的太空船终端 | Aliya 控制的 cosmos 调度层 |
| 飞船设备与舱门 | 具备能力边界的子 agent |
| 氧气、心率等仪表 | agent 卡片的进度、心跳、资源、成功率 |
| 设备操作权限 | 能力声明、权限范围与审批 |
| 实时通话与呼叫 | 命令接口、事件流与异步通知 |
| 聊天窗口 | agent 桌面与自然语言命令入口 |

游戏里的 cosmos 是唯一交互终端和设备监控中枢。本项目保留 cosmos 作为调度层，Aliya 通过它操控子 agent，端用户不直接接触 cosmos。

## 2. 角色与边界

| 角色 | 定位 | 负责 | 不负责 |
| --- | --- | --- | --- |
| Aliya | 控制入口 | 下达指令、操作桌面、审批高风险操作、接收结果与告警 | 直接调用子 agent、维护调度状态 |
| cosmos | 调度中枢兼展示层 | 意图识别、任务拆解、路由、并发调度、状态聚合、桌面与通知 | 编辑文件、执行命令、业务数据处理 |
| 子 agent | 执行单元 | 在声明的能力边界内执行任务、上报进度、产出工件 | 面向 Aliya、跨 agent 调度、修改其他 agent 状态 |

三层关系固定为 `Aliya -> cosmos -> 子 agent`。子 agent 的回调只回到 cosmos，由 cosmos 统一呈现给 Aliya。

## 3. 设计原则

1. 单一入口：Aliya 只与 cosmos 交互，所有调度与状态在 cosmos 收口。
2. 调度与执行分离：cosmos 决定谁来做、何时做、以什么顺序做；agent 只负责做。
3. 桌面即控制面：agent 列表、状态、调用、排序、启停都在同一套桌面模型上完成。
4. 状态由事件驱动：进度、心跳、告警、完成都通过事件流推送，查询作为兜底。
5. 契约先行：注册、任务、事件、控制接口都有稳定 schema 与版本号。
6. 可解释路由：每次自动匹配都保留候选、分数与原因，Aliya 可查看与纠正。
7. 渐进扩展：新增 agent 只需实现契约并注册，不改 cosmos 核心代码。

## 4. 文档地图

| 文档 | 内容 |
| --- | --- |
| [01-architecture.md](01-architecture.md) | 三层架构、组件、通信方式、任务生命周期、部署形态 |
| [02-registration.md](02-registration.md) | agent 清单格式、注册流程、探测、校验、存储 |
| [03-desktop.md](03-desktop.md) | 桌面卡片结构、状态模型、监控面板、排序分组、刷新与交互 |
| [04-dispatch.md](04-dispatch.md) | 意图识别、路由决策、任务拆解、并发调度、两条调用路径统一 |
| [05-contracts.md](05-contracts.md) | TaskEnvelope / TaskResult / TaskEvent 契约、回调、错误、取消与输入 |
| [06-control-api.md](06-control-api.md) | Aliya 控制面 API、桌面操作、干预、事件流、通知 |
| [07-example-edit-agent.md](07-example-edit-agent.md) | Edit agent 的桌面点击与自然语言两条完整链路 |
| [08-extension.md](08-extension.md) | 新增子 agent 的步骤、测试、灰度与注意事项 |
| [schemas/](schemas/) | 机器可读 JSON Schema：清单、任务信封、任务事件、任务结果 |

推荐阅读顺序：先读本文与架构说明，再按实现顺序读注册、契约、调度、桌面、控制接口，最后看示例与扩展。

## 5. 术语

| 术语 | 含义 |
| --- | --- |
| agent | 独立注册、可被发现和调度的执行单元；本文统一小写 |
| 能力 capability | agent 声明的稳定能力标识，如 `code.edit` |
| 任务 task | cosmos 委派给某个 agent 的一次执行，拥有唯一 `task_id` |
| 调用 invocation | Aliya 发起的一次请求，可能产生一个或多个任务 |
| 桌面 desktop | cosmos 向 Aliya 展示的 agent 卡片集合与控制面板 |
| 卡片 card | 桌面上的单个 agent 展示单元 |
| 事件 event | agent 或 cosmos 产生的状态变化记录 |
| 心跳 heartbeat | agent 周期性上报的存活与运行摘要 |
| 审批 approval | 高风险操作执行前对 Aliya 的确认 |
| 路由 route | cosmos 将请求匹配到 agent 的决策过程 |

## 6. 实现状态

| 模块 | 状态 | 位置 |
| --- | --- | --- |
| 工具 / Skill / MCP 层 | 已实现 | `core/agent` |
| LLM / 判断 / 配置 / 日志 / 服务层 | 已实现 | `core/llm`、`core/decision`、`core/config`、`core/logger`、`core/service` |
| cosmos 契约与注册表 | 规划 | `core/cosmos/contracts.py`、`core/cosmos/registry.py` |
| 路由与调度 | 规划 | `core/cosmos/router.py`、`core/cosmos/dispatcher.py` |
| 桌面与通知 | 规划 | `core/cosmos/desktop.py`、`core/cosmos/notifications.py` |
| 控制面 API | 规划 | `core/cosmos/api.py` |
| agent 清单与桌面配置 | 规划 | `data/cosmos/agents/`、`data/cosmos/desktop.yaml` |

本文描述目标设计。落地时先实现进程内调度与事件总线，再按需拆分 HTTP agent 服务。

建议落地顺序：

1. 契约与 schema：清单、任务信封、任务结果、任务事件。
2. 注册表与探测：启动扫描、校验、`health` / `describe` 握手。
3. 事件总线与任务存储：去重、排序、快照、断点续传。
4. 桌面与通知：卡片派生、布局、SSE 扇出、异步通知。
5. 路由与调度：候选打分、计划拆解、并发控制、取消传播。
6. 控制面：命令、调用、桌面操作、任务干预。
7. Edit agent 联调：桌面点击与自然语言两条路径跑通。
8. 运维收口：审计、限流、离线检测、灰度与下线。

## 7. 与 core/agent 的关系

`core/agent` 是工具层，解决「函数如何变成模型可调用的工具」。cosmos 是编排层，解决「哪个 agent 在什么约束下执行任务，以及如何把状态呈现给 Aliya」。

| 维度 | core/agent 工具层 | cosmos 编排层 |
| --- | --- | --- |
| 单元 | tool / skill / MCP server | agent |
| 生命周期 | 调用方用 `async with` 掌握 | 注册、探测、启停、离线检测 |
| 输入输出 | JSON 参数进、文本回填 | TaskEnvelope 进、TaskResult 与事件流回 |
| 调度 | 无 | 路由、并发、优先级、取消、审批 |
| 展示 | 无 | 桌面卡片、仪表盘、通知 |

子 agent 可以在内部使用 `core/agent` 的 `ToolRegistry`、`SkillLibrary` 与 `MCPManager`，也可以完全独立实现。cosmos 不假设 agent 的内部结构，只依赖对外契约。

## 8. 规划中的代码结构

```text
core/cosmos/
  contracts.py        Pydantic 模型：清单、任务、事件、错误
  registry.py         AgentRegistry：注册、校验、探测、启停
  router.py           IntentRouter：候选生成、打分、澄清
  dispatcher.py       任务拆解、依赖编排、结果聚合
  scheduler.py        并发控制、优先级队列、取消传播
  events.py           事件总线、序列号、SSE 扇出、断点续传
  desktop.py          卡片派生、布局、排序、分组、快照
  notifications.py    通知生成、去重、优先级、静默时段
  api.py              控制面 HTTP / SSE 接口
data/cosmos/
  agents/<agent_id>.yaml    agent 清单（可提交，密钥用占位符）
  desktop.yaml              分组、排序、可见性等桌面配置
```

依赖方向建议为 `cosmos -> (agent, llm, decision, config, logger, service)`。`agent` 层不需要反向依赖 cosmos；agent 通过契约与回调接入。

## 9. 文档约定

| 项 | 约定 |
| --- | --- |
| 字段命名 | JSON 与 YAML 使用 `snake_case` |
| 时间 | RFC 3339，UTC，毫秒精度，如 `2026-10-25T02:30:00.000Z` |
| 时长 | 毫秒整数，字段名以 `_ms` 结尾 |
| 进度 | `0.0` 到 `1.0` 的浮点数 |
| ID | `agent_id` 小写字母开头，允许短横线与下划线；`task_id` 使用 ULID 或 UUIDv7；`request_id` 由调用方生成 |
| 版本 | `schema_version` 为主版本整数或 `major.minor`；agent 版本使用语义化版本 |
| 扩展 | 新增字段保持向后兼容；读取方忽略未知字段 |
| 语言 | 文档与注释中文；代码标识符与事件类型英文 |
| 错误码 | 大写下划线，如 `AGENT_DISABLED`、`EDIT_CONFLICT` |

示例中的时间、ID、分数均为演示值。
