"""测试公共夹具。

要点：
- 每个测试用独立的应用实例与独立的 ServiceManager，避免状态串味。
- 用 httpx 的 ASGITransport 在进程内发请求，无需真实端口。
- 通过覆盖 get_settings 依赖隔离环境变量。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.api import create_app
from core.config import AppSettings, LogSettings, Settings, get_settings
from core.service import ServiceManager
from core.service.item_service import ItemService


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    """测试用配置：日志落到临时目录，避免污染工作区。"""
    get_settings.cache_clear()
    return Settings(
        app=AppSettings(app_name="aliya-cosmos-test", env="test", debug=False),
        log=LogSettings(
            level="DEBUG",
            dir=str(tmp_path / "logs"),
            rotation="1 MB",
            retention="1 day",
        ),
    )


@pytest.fixture
def manager() -> ServiceManager:
    """只装配示例服务的 manager。"""
    mgr = ServiceManager()
    _ = mgr.register(ItemService())
    return mgr


@pytest.fixture
def app(test_settings: Settings, manager: ServiceManager) -> FastAPI:
    return create_app(settings=test_settings, manager=manager)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """带生命周期的异步客户端：请求前后自动启停服务。"""
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
