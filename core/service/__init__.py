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
from core.service.registry import build_manager, default_manager

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
    "build_manager",
    "default_manager",
]
