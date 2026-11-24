"""cosmos 装配入口。

`core` 不 import `agent`，因此装配放在这里：先拿 core 的 4 个服务，再按
「同层无依赖、注册顺序即启动顺序」注册 cosmos 的 10 个服务。

跨服务的反向读取（链路状态）用 setter 注入，避免构造器循环依赖：

- `WorldBridgeService.set_link_provider(TerminalService.link_state)`
- `WorldBridgeService.set_link_since_provider(TerminalService.link_since_minutes)`
- `WorldBridgeService.set_last_aliya_provider(TerminalService.last_aliya_at)`
"""

from __future__ import annotations

from agent.cosmos.services import (
    AgentRegistryService,
    AgentRuntimeService,
    ControlService,
    ConversationService,
    DesktopService,
    DispatchService,
    PresenceService,
    TerminalService,
    WorldBridgeService,
)
from agent.cosmos.transport import BusService
from core.config import Settings, get_settings
from core.service import ServiceManager
from core.service.registry import build_manager


def build_cosmos_manager(settings: Settings | None = None) -> ServiceManager:
    """构造并装配 core + cosmos 全部服务（共 14 个）。"""
    resolved = settings if settings is not None else get_settings()
    manager = build_manager(resolved)
    manager.register(
        BusService,
        AgentRegistryService,
        AgentRuntimeService,
        DesktopService,
        PresenceService,
        ConversationService,
        DispatchService,
        WorldBridgeService,
        TerminalService,
        ControlService,
    )
    _wire(manager)
    return manager


def _wire(manager: ServiceManager) -> None:
    """跨服务注入：构造期拿实例，但不动 core 的容器。"""
    world = manager.get(WorldBridgeService)
    terminal = manager.get(TerminalService)
    manager.get(AgentRuntimeService).set_presence(manager.get(PresenceService))
    world.set_link_provider(terminal.link_state)
    world.set_link_since_provider(terminal.link_since_minutes)
    world.set_last_aliya_provider(terminal.last_aliya_at)
    world.set_queue_provider(terminal.queue_depth)
    world.set_history_provider(
        lambda: terminal.history_pattern(window_days=world.history_window_days())
    )


__all__ = ["build_cosmos_manager"]
