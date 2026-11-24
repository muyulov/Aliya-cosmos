"""Aliya agent。

定位：对话主体 + 控制入口。对用户负责，对子 agent 有最终调度决定权。

依赖方向只能是 `agent.aliya → agent.cosmos.contracts / ControlService → core.*`；
本目录不得 import `agent.cosmos` 的内部实现，也不得直接持有子 agent 的引用。

当前为占位骨架：契约与流程见 `agent/cosmos/docs/`，实现尚未落地。
"""

__all__: list[str] = []
