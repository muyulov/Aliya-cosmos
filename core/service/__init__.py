"""服务层对外出口。"""

from core.service.base import UNSET, HealthStatus, Service, ServiceState, Unset
from core.service.clock_service import ClockService
from core.service.manager import (
    CircularDependencyError,
    HookTimeoutError,
    MissingDependencyError,
    ServiceContractError,
    ServiceError,
    ServiceManager,
    ServiceNotRegisteredError,
    ServiceStartError,
)

#: 装配入口（build_manager / default_manager）在 core.service.registry：
#: registry 要 import 具体服务类（如 LLMService），若再由本文件导出它，
#: `service.__init__ → registry → llm.service → service.base` 就成环了。

__all__ = [
    "UNSET",
    "CircularDependencyError",
    "ClockService",
    "HealthStatus",
    "HookTimeoutError",
    "MissingDependencyError",
    "Service",
    "ServiceContractError",
    "ServiceError",
    "ServiceManager",
    "ServiceNotRegisteredError",
    "ServiceStartError",
    "ServiceState",
    "Unset",
]
