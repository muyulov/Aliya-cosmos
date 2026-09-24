"""服务注册表。

提供两件事：
- build_manager()：构造一个装配好全部服务的 manager，供应用与脚本使用。
- default_manager：全局默认实例，供 CLI 与测试等无 Web 场景使用。

新增服务时，在 build_manager() 里 register 类型即可，无需改动其他文件。
装配是惰性的：这里只登记类型，不读配置、不实例化服务，因此模块级构造
default_manager 没有 import 副作用。
"""

from __future__ import annotations

from core.config import Settings
from core.service.clock_service import ClockService
from core.service.item_service import ItemService
from core.service.manager import ServiceManager


def build_manager(settings: Settings | None = None) -> ServiceManager:
    """构造并装配全部服务的 manager。"""
    manager = ServiceManager(settings)
    _ = manager.register(ClockService)
    _ = manager.register(ItemService)
    return manager


#: 全局默认 manager。Web 场景请使用 app.state.services，避免多实例互相干扰。
default_manager = build_manager()
