"""Agent 装配层。

本包放的是「agent 本身」，与 `core.agent`（工具 / Skill / MCP 的零件库）不是一回事：

- `core.agent`：工具怎么声明、怎么交给模型、结果怎么回填。
- `agent.cosmos`：cosmos 主 agent 的编排实现（调度中枢 + 通信终端 + 状态感知）。
- `agent.aliya`：Aliya 的实现（对话主体 + 控制入口 + 自主行动）。

当前阶段只有文档与配置骨架，不放业务逻辑；实现落地时再按
`agent/cosmos/docs/01-architecture.md` 里的组件划分填代码。
"""

__all__: list[str] = []
