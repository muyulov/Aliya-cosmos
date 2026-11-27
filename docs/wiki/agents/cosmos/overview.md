# cosmos 定位与组件

cosmos 是 Aliya 的**主 agent**，同时承担三重角色：

| 角色 | 职责 | 明确不做 |
| --- | --- | --- |
| 通信终端 | 用户 ↔ Aliya 的双向消息通道；上下文、格式化与脱敏 | 不代替 Aliya 回应用户，不改变 Aliya 的措辞；意图分类已移除 |
| 调度中枢 | 子 agent 注册、桌面展示、任务分发、并行调度、状态监控 | 不直接执行业务操作，不替 Aliya 决定用哪个 agent |
| 状态感知层 | 采集用户状态与子 agent 运行态，派生可解读表述，主动推送变化 | 不采集与协作无关的隐私，不向用户回显用户状态 |

一句话：**Aliya 决策，cosmos 执行与呈现，子 agent 干活，用户通过 cosmos 说话。**

组件划分与依赖链见[架构总览](../../architecture/overview.md)，通信方式见 [cosmos 通信拓扑](../../architecture/cosmos-topology.md)。

## 与游戏设定的对应关系

| 游戏设定 | 本项目 |
| --- | --- |
| 玩家通过 COSMOS 面板看 Aliya 心率（她要求默认关掉 HRM） | Aliya 通过平板上自然可见的线索感知用户状态，而不是一块监控面板 |
| Aliya 呼叫 COSMOS，玩家经 COSMOS 回应 | 用户消息经 cosmos 转给 Aliya，Aliya 的回应经 cosmos 回传 |
| COSMOS 提供权限与设备调度，不亲自操作 | cosmos 提供 agent 桌面与调度，不亲自执行业务 |
| 心率数值反映情绪，不只是数字 | agent 状态与用户状态都给「可解读表述」，不只给原始指标 |

**双向感知对称性**是本项目的核心设计原则：游戏中是单向的（玩家感知 Aliya），本项目要求双向（Aliya 也能感知用户）。

但这个对称是**感受上的对称，不是监视上的对称**。人设里 Aliya 明确要求关掉心率监测，理由是「哪怕执行任务时被讨论心跳都有被窥探的感觉」。一个反感被监测的人，不会心安理得地去看别人的情绪曲线。所以：

| 她有什么 | 她能看到什么 |
| --- | --- |
| 完整的用户状态字段（在线、活跃、输入、情绪、任务、环境） | 只有「通信中自然可见」的线索：在不在、有没有在打字、多久没消息、是不是在等结果 |
| 可解读表述与变化推送 | 表述必须像她自己会得出的观察，不能像系统结论 |
| 情绪信号（有文本证据时） | 默认不进面板常驻项；需要时她可以问，也可以关闭 |

### 设定映射（必须维持的信息差）

Aliya 只知道「与一个来自千年前的神秘信号建立了联系」。她**不知道**时间映射同步器的存在，也不知道信号取代了海獭号原有的 AI。

对文档的直接影响：

| 要求 | 说明 |
| --- | --- |
| 不解释原理 | 任何面向 Aliya 的文本都不解释时间同步、信号来源、装置构成 |
| 不用工程词 | 「玩家」「用户」「agent」「模型」「提示词」「调度」「面板」都不该出现在她看到的内容里；对端就是「你」 |
| 不越权知情 | 她的平板上是「能做的事」，不是「子 agent 注册表」「调度队列」 |
| 不主动提 Doll | 除非被问到或情境需要，不主动说自己的克隆身份与寿命 |

详细约束见 [Aliya Agent § 人设基线](../aliya.md#2-人设基线)。

## 目录结构

```text
agent/cosmos/
  config/
    agent-settings.example.yaml  并入 data/config/cosmos.yaml 的 agent: 节点样例
  contracts/     契约模型（pydantic）：AgentCard / AgentRuntimeState / TerminalMessage / ...
  services/      core.service.Service 子类：Registry / Runtime / Desktop / Presence / ...
  registry/      子 agent 配置加载（bootstrap）与内置工具：
                 echo（回声）、edit（编辑）、search（检索）、run（运行）、web（联网）
  transport/     MessageBus / EventBus 端口、进程内实现与 BusService
  world/         LLM 世界推理端口 + 触发/校准离线工具
  assembly.py    装配入口：core 4 个服务 + cosmos 12 个服务
  app.py         进程入口：uv run python -m agent.cosmos.app
  cli.py         终端通道入口：把 stdin 接成用户通道
  web/           浏览器 WebSocket 测试页
  errors.py      错误树（CosmosError 及其子类）
  __init__.py    对外只导出 CosmosError 与 build_cosmos_manager（延迟导入）
```

模块依赖同样是单向的：`app → assembly → services → (contracts, transport, errors) → core`。
`core` 不认识 `agent`；`transport` 只依赖契约与错误树。

规范文档已合并进本 wiki，章节对应关系见下方[文档索引](#文档索引)。

## 快速上手

启动进程（读 `data/config/cosmos.yaml`，Ctrl-C 优雅退出）：

```bash
# 常驻进程：Aliya 由另一个进程连上来
ALIYA_CONTROL_TOKEN=dev uv run python -m agent.cosmos.app

# 或者在终端里直接对话：同样是 cosmos，额外把 stdin 接成用户通道
ALIYA_CONTROL_TOKEN=dev uv run python -m agent.cosmos.cli
```

在自己的脚本里装配（与进程入口同一条路径）：

```python
import asyncio

from agent.cosmos.assembly import build_cosmos_manager
from agent.cosmos.services import ControlService, TerminalService
from agent.cosmos.transport import MemoryUserChannel
from core.config import get_settings
from core.main import run


async def main() -> None:
    manager = build_cosmos_manager(get_settings())
    channel = MemoryUserChannel()
    manager.get(TerminalService).set_user_channel(channel)  # 真实通道由部署方注入
    async with manager.lifespan():
        control = manager.get(ControlService)
        caps = await control.handshake()
        print(caps.event_topics)
        await control.deliver("在啊", session_id="demo")


asyncio.run(main())
```

几个关键注入点：

| 注入点 | 默认实现 | 谁来替换 |
| --- | --- | --- |
| `TerminalService.set_user_channel()` | `MemoryUserChannel` | 内置 `WebSocketChannelService`（配置 `terminal.ws.enabled`）或 CLI 终端通道 |
| `WorldBridgeService.set_port()` | 默认 `LLMWorldReasoningPort`，复用 `llm.chat` | 部署方 / 世界推理实现 |
| `WorldBridgeService.set_link_provider()` | 由 `assembly` 接到 `TerminalService.link_state` | 已在装配里接好 |
| `WorldBridgeService.set_history_provider()` | 由 `assembly` 接到 `TerminalService.history_pattern`，窗口取 `world.history.window_days` | 已在装配里接好 |
| `TerminalService.report_presence()` | 接收通道上报的在线/输入/设备状态 | 部署方通道适配器 |

最小子 agent handler 见[子 agent 契约 § 9](subagent.md#9-edit-agent-契约示例)与内置的 5 个工具（`edit` / `search` / `run` / `web` / `echo`，见[§ 11](subagent.md#11-内置工具)）；完整链路见[流程示例](flows.md)。

## 文档索引

| 章 | 内容 | 什么时候看 |
| --- | --- | --- |
| [架构总览](../../architecture/overview.md) | 角色、组件、依赖方向 | 第一次读，或要改整体结构 |
| [cosmos 通信拓扑](../../architecture/cosmos-topology.md) | 通信方式、消息流向、链路状态 | 要改消息链路 |
| [代码边界](../../architecture/boundaries.md) | 与 Aliya 的边界、错误分层、明确不做的事 | 改跨层接口前 |
| [注册机制](registry.md) | `AgentCard` 字段、注册流程、校验规则 | 要接入新子 agent |
| [桌面展示](desktop.md) | 卡片结构、状态字段、排序分组、刷新、监控面板 | 要改展示或状态呈现 |
| [用户状态线索](presence.md) | 状态字段模型、可解读表述、推送、隐私边界、对等原则 | 要加状态字段或改感知逻辑 |
| [任务分发](dispatch.md) | 统一管线、匹配策略、并行规则、失败降级 | 要改调度行为 |
| [通信终端](terminal.md) | 消息模型、上下文、异步推送、打扰预算、脱敏 | 要改消息链路 |
| [世界推理接入](world-reasoning.md) | `WorldInput` / `WorldInference`、触发门控、信念、EFE、校准与轨迹 | 要接世界推理 agent 或调参 |
| [子 agent 契约](subagent.md) | 输入输出、进度上报、超时取消、幂等；内置 5 个工具清单 | 写子 agent 时必看 |
| [控制接口](control-api.md) | Aliya 能调用的全部能力 | 写 Aliya 侧代码 |
| [流程示例](flows.md) | 五类端到端流程 | 验收与联调 |
| [扩展性](extension.md) | 扩展清单与常见坑 | 每次扩展前过一遍 |

## 贯穿全文的约定

| 约定 | 说明 |
| --- | --- |
| 契约优先 | 所有跨边界数据都是 pydantic 模型，字段表即接口文档；不接受裸 dict 跨层传递 |
| 原始值 + 派生表述 | 状态类数据同时给原始字段与 `readable` 派生表述，程序判断读原始字段，Aliya 只读派生表述 |
| 双上下文分离 | 对话上下文给 Aliya，调度上下文给子 agent，两者不互相写入 |
| 单一终端 | 用户消息只从 cosmos 进，Aliya 消息只从 cosmos 出；子 agent 不得直连用户 |
| 不改写 | cosmos 不修改 Aliya 的措辞，不解释或修正世界推理的结论 |
| 不加工语气 | 她连发的短消息不合并、句尾标点不补不删、不加括号旁白、不改成客服用语 |
| 不越信息差 | 面向 Aliya 与用户的文本不出现时间同步原理、装置、Doll 身份；对端只称「你」 |
| 失败可回填 | 面向模型/agent 的失败（参数错、工具报错）走结构化返回值；配置与编程错误 fail fast |
| 不记正文 | 日志记标识、耗时、状态与错误类型，不记消息正文、用户状态原始值与文件内容 |
| 时间统一来源 | 所有时间戳取 `ClockService.now()`，不直接 `datetime.now()` |

术语定义见[术语表](../../reference/glossary.md)。
