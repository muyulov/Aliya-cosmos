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


def test_TTY_着色不污染日志文件(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """控制台着色不得写进文件：三 sink 共享 record，串用标记位就会串色。"""
    import core.logger.sinks as sinks_module

    monkeypatch.setattr(sinks_module, "_is_tty", lambda: True)
    cfg = _cfg(tmp_path)
    setup_logging(cfg)
    logger.info("着色检查")
    logger.error("着色错误检查")
    logger.remove()

    for name in (cfg.file_name, cfg.error_file_name):
        raw = (Path(cfg.dir) / name).read_bytes()
        assert b"\x1b[" not in raw, f"{name} 不应包含 ANSI 转义序列"


def test_两个文件_sink_都能拿到堆栈(tmp_path: Path) -> None:
    """堆栈只提取一次并缓存，主日志与错误日志都要有，且各只有一份。"""
    cfg = _cfg(tmp_path)
    setup_logging(cfg)
    try:
        raise KeyError("boom")
    except KeyError:
        logger.exception("带堆栈的错误")
    logger.remove()

    for name in (cfg.file_name, cfg.error_file_name):
        text = (Path(cfg.dir) / name).read_text(encoding="utf-8")
        assert text.count("Traceback (most recent call last)") == 1
        assert "KeyError: 'boom'" in text


def test_桥接路径堆栈也只出现一次(tmp_path: Path) -> None:
    """标准库 logging 桥接过来的异常，同样不应重复追加堆栈。"""
    cfg = _cfg(tmp_path, level="DEBUG")
    setup_logging(cfg)
    try:
        raise ValueError("来自标准库")
    except ValueError:
        logging.getLogger("asyncio").exception("桥接异常")
    logger.remove()

    text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    assert text.count("Traceback (most recent call last)") == 1
    assert "ValueError: 来自标准库" in text


def test_第三方库日志经桥接后仍统一格式(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, level="DEBUG")
    setup_logging(cfg)
    logging.getLogger("uvicorn.access").info("第三方库日志")
    logger.remove()

    text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    assert "[I]" in text
    assert "第三方库日志" in text


def _build_app(tmp_path: Path):
    """构造带日志配置的应用，供接口级测试使用。"""
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
    return create_app(settings=settings, manager=mgr)


async def _request(app, path: str, request_id: str):
    """发一个请求，返回响应。"""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, headers={"X-Request-ID": request_id})


async def test_请求日志自动携带_request_id(tmp_path: Path) -> None:
    """中间件不手写 request_id，字段由上下文自动注入。"""
    app = _build_app(tmp_path)
    resp = await _request(app, "/api/v1/health", "header-id")
    logger.remove()

    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == "header-id"
    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    # 请求完成那条日志的字段块里应带上来自请求头的 request_id
    block = text[text.index("请求完成") :]
    assert "request_id: header-id" in block


async def test_未捕获异常日志也带_request_id(tmp_path: Path) -> None:
    """500 处理器在中间件之外执行，必须显式从 request.state 取 id。"""
    app = _build_app(tmp_path)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("炸了")

    resp = await _request(app, "/boom", "rid-500")
    logger.remove()

    assert resp.status_code == 500
    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    block = text[text.index("未捕获异常") :]
    assert "rid-500" in block


def test_bind_附加基础字段(tmp_path: Path) -> None:
    from core.logger import log

    setup_logging(_cfg(tmp_path))
    log.bind(服务="clock").info("绑定了字段")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "服务: clock" in text


def test_prefix_渲染消息前缀(tmp_path: Path) -> None:
    from core.logger import log

    setup_logging(_cfg(tmp_path))
    log.prefix("clock").info("时钟服务已启动")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "[clock] 时钟服务已启动" in text


def test_bind_与_prefix_可链式叠加(tmp_path: Path) -> None:
    from core.logger import log

    setup_logging(_cfg(tmp_path))
    log.bind(服务="clock").prefix("clock").info("链式")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "[clock] 链式" in text
    assert "服务: clock" in text


def test_调用点字段覆盖_bind_字段(tmp_path: Path) -> None:
    """bind 的字段优先级最低，调用点显式传值应胜出。"""
    from core.logger import log

    setup_logging(_cfg(tmp_path))
    log.bind(服务="clock").info("覆盖", 服务="item")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "服务: item" in text
    assert "服务: clock" not in text


def test_prefix_作用于_exception(tmp_path: Path) -> None:
    from core.logger import log

    setup_logging(_cfg(tmp_path))
    try:
        raise KeyError("boom")
    except KeyError:
        log.prefix("clock").exception("带堆栈")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "[clock] 带堆栈" in text
