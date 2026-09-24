"""应用工厂。

装配顺序：日志 → 服务容器 → 中间件 → 路由 → 异常处理器。
工厂形式便于测试直接构造实例，也便于将来挂多个入口。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.api.errors import register_exception_handlers
from core.api.middleware import RequestContextMiddleware
from core.api.v1.router import api_router
from core.config import Settings, get_settings
from core.logger import faces, log, setup_logging
from core.service import ServiceManager
from core.service.registry import build_manager


def create_app(
    settings: Settings | None = None,
    manager: ServiceManager | None = None,
) -> FastAPI:
    """构造 FastAPI 应用。"""
    cfg = settings or get_settings()
    setup_logging(cfg.log)

    services = manager or build_manager(cfg)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        log.info(
            "应用启动中",
            face=faces.START,
            应用=cfg.app.app_name,
            环境=cfg.app.env,
            调试=str(cfg.app.debug).lower(),
        )
        async with services.lifespan():
            yield
        log.info("应用已关闭", face=faces.BYE, 应用=cfg.app.app_name)

    app = FastAPI(
        title=cfg.app.app_name,
        debug=cfg.app.debug,
        version="0.1.0",
        lifespan=lifespan,
    )

    # 服务容器挂在 app.state，deps 从这里取，保证多实例互不干扰
    app.state.services = services
    app.state.settings = cfg

    app.add_middleware(RequestContextMiddleware)
    app.include_router(api_router, prefix="/api/v1")
    register_exception_handlers(app)

    return app
