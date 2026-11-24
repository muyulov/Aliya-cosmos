"""cosmos 主 agent（Supervisor / Orchestrator）。

对外只暴露装配入口与错误树；装配入口用模块级 `__getattr__` 延迟导入，
避免 `import agent.cosmos.contracts` 时把整棵服务依赖树带起来。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.cosmos.errors import CosmosError

if TYPE_CHECKING:
    from agent.cosmos.assembly import build_cosmos_manager

__all__ = ["CosmosError", "build_cosmos_manager"]


def __getattr__(name: str) -> object:
    """按需导出装配入口。"""
    if name == "build_cosmos_manager":
        from agent.cosmos.assembly import build_cosmos_manager

        return build_cosmos_manager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
