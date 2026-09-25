"""服务层对外出口。"""

from core.service.base import HealthStatus, Service, ServiceState
from core.service.clock_service import ClockService
from core.service.manager import (
    CircularDependencyError,
    MissingDependencyError,
    ServiceContractError,
    ServiceError,
    ServiceManager,
    ServiceNotRegisteredError,
    ServiceStartError,
)
from core.service.registry import build_manager, default_manager

__all__ = [
    "CircularDependencyError",
    "ClockService",
    "HealthStatus",
    "MissingDependencyError",
    "Service",
    "ServiceContractError",
    "ServiceError",
    "ServiceManager",
    "ServiceNotRegisteredError",
    "ServiceStartError",
    "ServiceState",
    "build_manager",
    "default_manager",
]
