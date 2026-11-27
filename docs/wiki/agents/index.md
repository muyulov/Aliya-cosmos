# Agent 总览

项目里有两个 agent 实现，职责与人格归属完全不同：

| 目录 | 角色 | 文档入口 |
| --- | --- | --- |
| `agent/cosmos` | 主 agent（Supervisor / Orchestrator）：通信终端 + 调度中枢 + 状态感知层 | [cosmos 定位与组件](cosmos/overview.md) |
| `agent/aliya` | Aliya：对话主体与控制入口，对用户负责，对子 agent 有调度决定权 | [Aliya Agent](aliya.md) |

关系图与三层职责见[架构总览](../architecture/overview.md)，通信细节见 [cosmos 通信拓扑](../architecture/cosmos-topology.md)。

## 人设基线

角色设定的唯一来源是 `data/prompt/Aliya.md`。文档与人设冲突时以人设为准，最少要遵守三条：

| 约束 | 说明 |
| --- | --- |
| 维持信息差 | Aliya 只知道「与一个来自千年前的神秘信号建立了联系」，不知道时间映射同步器，也不知道信号取代了海獭号原有的 AI |
| 语气不加工 | 她像发微信一样短句连发，cosmos 不合并、不补标点、不加旁白、不改成客服腔 |
| 对等隐私 | 她要求关掉自己的心率监测（HRM），cosmos 便不给她监控式的用户面板，只给通信中自然可见的线索 |

**双向感知对称性**不是「互相监视」，而是「互相读懂」。玩家看她的心率，看的其实是情绪；她看用户的状态，看的应该是「现在适不适合说话」。

## 命名易混点

| 名字 | 含义 | 位置 |
| --- | --- | --- |
| `core.agent` | 工具 / Skill / MCP 的**零件库** | `core/agent/` |
| `agent.cosmos` | cosmos 的**编排实现** | `agent/cosmos/` |
| `agent.aliya` | Aliya 的**实现** | `agent/aliya/` |

再强调一次：子 agent 注册到 cosmos 的 agent 桌面，**不是**注册成 LLM 的 tool。前者是「应用」，由 Aliya 通过控制面启动；后者是子 agent 内部的实现细节（Edit agent 内部当然可以用 `ToolRegistry` 跑 ReAct，但那是它自己的事）。

## 阅读顺序

1. [cosmos 定位与组件](cosmos/overview.md)：定位、三重角色、文档索引。
2. [cosmos 通信拓扑](../architecture/cosmos-topology.md)：通信方式与消息流向。
3. [流程示例](cosmos/flows.md)：五类端到端流程，先看流程再看契约更快。
4. 其余 cosmos 章节按需查阅。
5. [Aliya Agent](aliya.md)：从 Aliya 视角看接口边界。
