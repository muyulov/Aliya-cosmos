"""sink 装配测试。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from loguru import logger

from core.config import LogSettings
from core.logger import setup_logging


@pytest.fixture(autouse=True)
def _restore_logger():
    """每个用例前后重置 loguru，避免 sink 泄漏到其他测试。"""
    yield
    _ = logger.remove()
    logging.getLogger().handlers = []


def _cfg(tmp_path: Path, **overrides: object) -> LogSettings:
    base: dict[str, object] = {
        "level": "INFO",
        "dir": str(tmp_path / "logs"),
        "retention": "1 day",
    }
    base.update(overrides)
    return LogSettings(**base)  # type: ignore[arg-type]


def test_装配三个_sink(tmp_path: Path) -> None:
    setup_logging(_cfg(tmp_path))
    assert len(logger._core.handlers) == 3


def test_普通信息只进主日志(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    setup_logging(cfg)
    logger.info("普通信息")
    logger.remove()

    app_log = Path(cfg.dir) / cfg.file_name
    error_log = Path(cfg.dir) / cfg.error_file_name
    assert "普通信息" in app_log.read_text(encoding="utf-8")
    assert not error_log.exists() or "普通信息" not in error_log.read_text(encoding="utf-8")


def test_错误同时进主日志与错误日志(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    setup_logging(cfg)
    logger.error("出错了")
    logger.remove()

    app_text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    error_text = (Path(cfg.dir) / cfg.error_file_name).read_text(encoding="utf-8")
    assert "出错了" in app_text
    assert "出错了" in error_text


def test_默认按天轮转() -> None:
    assert LogSettings().rotation == "00:00"
    assert LogSettings().error_file_name == "error.log"


def test_门面自动携带上下文字段(tmp_path: Path) -> None:
    """log.info 时，上下文里的键自动成为树形字段。"""
    from core.logger import context, log

    setup_logging(_cfg(tmp_path))
    with context.request_scope("rid-1"):
        log.info("带上下文")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "带上下文" in text
    assert "rid-1" in text
    assert "request_id" in text


def test_调用点字段覆盖上下文(tmp_path: Path) -> None:
    """kwargs 显式传值与上下文同名时，以调用点为准。"""
    from core.logger import context, log

    setup_logging(_cfg(tmp_path))
    with context.request_scope("ctx-id"):
        log.info("覆盖", request_id="explicit-id")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "explicit-id" in text
    assert "ctx-id" not in text


def test_log_context_上下文管理器(tmp_path: Path) -> None:
    from core.logger import log

    setup_logging(_cfg(tmp_path))
    with log.context(会话="s-9"):
        log.info("会话内")
    log.info("会话外")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    inside, _, outside = text.partition("会话外")
    assert "s-9" in inside
    assert "s-9" not in outside


def test_不再暴露_bind_request() -> None:
    from core.logger import log

    assert not hasattr(log, "bind_request")


def test_中间件不再显式传_request_id() -> None:
    """回归防护：middleware 源码里不应再出现 request_id=request_id 这种手写字段。"""
    import inspect

    from core.api import middleware

    source = inspect.getsource(middleware)
    assert "request_id=request_id" not in source
    assert "bind_request" not in source


async def test_请求日志自动携带_request_id(tmp_path: Path) -> None:
    """中间件不再手写 request_id，字段由上下文自动注入。"""
    from httpx import ASGITransport, AsyncClient

    from core.api import create_app
    from core.config import AppSettings, Settings, get_settings
    from core.service import ServiceManager
    from core.service.item_service import ItemService

    get_settings.cache_clear()
    settings = Settings(
        app=AppSettings(app_name="t", env="test", debug=False),
        log=_cfg(tmp_path),
    )
    mgr = ServiceManager()
    _ = mgr.register(ItemService())
    app = create_app(settings=settings, manager=mgr)

    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/health", headers={"X-Request-ID": "header-id"})
    logger.remove()

    assert resp.status_code == 200
    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "header-id" in text
    assert "请求完成" in text
