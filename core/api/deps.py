"""依赖注入提供者。

路由通过 Depends 声明所需依赖，避免在模块级持有全局状态，
测试时可整体替换。

说明：Starlette 的 app.state 是动态属性容器，类型检查器无法推断其成员，
因此这里用显式类型注解 + 局部变量承接，避免 Any 扩散。
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request
from starlette.applications import Starlette
from starlette.datastructures import State

from core.config import Settings, get_settings
from core.service import ItemService, ServiceManager
from core.service.registry import default_manager


def _state_of(request: Request) -> State:
    """取出 request.app.state。

    starlette 把 app 声明为 Any，直接取属性会让 Any 一路扩散，
    这里用 cast 收敛为 State（动态属性容器）。
    """
    app = cast("Starlette", request.app)
    return app.state


def settings_dep(request: Request) -> Settings:
    """提供当前应用的配置。

    优先取 app.state.settings（工厂注入的实例），回退到全局单例，
    这样测试注入的配置能贯穿整条请求链路。
    """
    settings = cast("Settings | None", getattr(_state_of(request), "settings", None))
    return settings if settings is not None else get_settings()


def manager_dep(request: Request) -> ServiceManager:
    """提供当前应用的 service manager。

    走 app.state 以保证每个应用实例独立；脱离 Web 场景时回退到全局实例。
    """
    manager = cast("ServiceManager | None", getattr(_state_of(request), "services", None))
    return manager if manager is not None else default_manager


def item_service_dep(manager: Annotated[ServiceManager, Depends(manager_dep)]) -> ItemService:
    """提供条目服务实例。"""
    return manager.get(ItemService)


def request_id_dep(request: Request) -> str:
    """提供当前请求的 request_id，用于回写响应信封。"""
    return cast("str", getattr(request.state, "request_id", "-"))


SettingsDep = Annotated[Settings, Depends(settings_dep)]
ManagerDep = Annotated[ServiceManager, Depends(manager_dep)]
ItemServiceDep = Annotated[ItemService, Depends(item_service_dep)]
RequestIdDep = Annotated[str, Depends(request_id_dep)]
