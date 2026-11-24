"""子 agent 适配器与内置 agent。

`bootstrap` 负责配置加载；具体 agent（如 `echo`）各自一个模块，导出卡片与 handler。
"""

from agent.cosmos.registry.bootstrap import load_agents, to_card

__all__ = ["load_agents", "to_card"]
