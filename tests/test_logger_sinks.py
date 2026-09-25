"""sink 装配测试。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, cast

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
    # 覆盖项是动态拼进来的，**base 无法静态校验，这里显式抑制
    return LogSettings(**base)  # pyright: ignore[reportArgumentType]


class _Core(Protocol):
    """loguru 私有核心对象的最小形状，仅取用其中已注册的 sink 表。"""

    handlers: dict[int, object]


def _sink_count() -> int:
    """已注册 sink 的数量。

    loguru 未公开该信息，只能读私有属性 _core.handlers；该属性不在类型标注里，
    故对属性访问放宽一次，再用 _Core 承接出显式形状，避免未知类型一路扩散。
    """
    core = cast("_Core", logger._core)  # pyright: ignore[reportAttributeAccessIssue]
    return len(core.handlers)


def test_装配三个_sink(tmp_path: Path) -> None:
    setup_logging(_cfg(tmp_path))
    assert _sink_count() == 3


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
    logging.getLogger("sqlalchemy.engine").info("第三方库日志")
    logger.remove()

    text = (Path(cfg.dir) / cfg.file_name).read_text(encoding="utf-8")
    assert "[I]" in text
    assert "第三方库日志" in text


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
    log.bind(服务="clock").info("覆盖", 服务="db")
    logger.remove()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "服务: db" in text
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
