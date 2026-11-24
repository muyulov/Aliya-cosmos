"""cosmos 常驻服务（`core.service.Service` 子类）。"""

from agent.cosmos.services.control import ControlService
from agent.cosmos.services.conversation import ConversationService
from agent.cosmos.services.desktop import DesktopService
from agent.cosmos.services.dispatch import DispatchService
from agent.cosmos.services.presence import PresenceService
from agent.cosmos.services.registry import AgentRegistryService
from agent.cosmos.services.runtime import AgentRuntimeService
from agent.cosmos.services.terminal import TerminalService
from agent.cosmos.services.world import WorldBridgeService

__all__ = [
    "AgentRegistryService",
    "AgentRuntimeService",
    "ControlService",
    "ConversationService",
    "DesktopService",
    "DispatchService",
    "PresenceService",
    "TerminalService",
    "WorldBridgeService",
]
