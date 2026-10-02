"""服务注册表。

提供 build_manager()：构造一个装配好全部服务的 manager，供入口与脚本使用。
需要独立配置或隔离状态时也是调它，不再有模块级共享实例。

新增服务时，在 build_manager() 里 register 类型即可（可一次传入多个），无需改动其他文件。
装配是惰性的：这里只登记类型，不读配置、不实例化服务，因此 import 本模块无副作用。
"""

from __future__ import annotations

from core.config import Settings
from core.decision.service import DecisionService
from core.embedding.service import EmbeddingService
from core.llm.service import LLMService
from core.service.clock_service import ClockService
from core.service.manager import ServiceManager


def build_manager(settings: Settings | None = None) -> ServiceManager:
    """构造并装配全部服务的 manager。"""
    manager = ServiceManager(settings)
    manager.register(ClockService, EmbeddingService, LLMService, DecisionService)
    return manager
