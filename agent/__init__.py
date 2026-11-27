"""Agent 装配层。

本包放的是「agent 本身」，与 `core.agent`（工具 / Skill / MCP 的零件库）不是一回事：

- `core.agent`：工具怎么声明、怎么交给模型、结果怎么回填。
- `agent.cosmos`：cosmos 主 agent 的编排实现（调度中枢 + 通信终端 + 状态感知）。
- `agent.aliya`：Aliya 的实现（对话主体 + 控制入口 + 自主行动）。

组件划分见 `docs/wiki/architecture/overview.md`，
各服务的规范见 `docs/wiki/agents/cosmos/overview.md`。
"""

__all__: list[str] = []
