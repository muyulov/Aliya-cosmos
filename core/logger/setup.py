"""日志装配与门面。

职责：
- 暴露 log 对象：调用 log.info("消息", face=..., 任意字段=值) 时字段自动成树，
  并把 context.py 里的上下文自动并入字段。
- 编排 sink 装配（委托给 sinks.py）。
- 桥接标准库 logging（uvicorn、sqlalchemy 等）到 loguru，统一格式。

导入关系：setup 依赖 sinks / context / config，反向不成立，不存在导入环。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType, ModuleType
from typing import override

from loguru import logger

import core.logger.faces as faces_module
from core.config import LogSettings, get_settings
from core.logger import context as context_module
from core.logger.sinks import add_console_sink, add_file_sinks

_INTERCEPTED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
    "sqlalchemy",
    "asyncio",
    "httpx",
    "httpcore",
)


class Log:
    """日志门面：把 kwargs 收集为结构化字段并注入 loguru。

    类型注解约定：face 为颜文字字符串，fields 为任意键值的结构化字段，
    因此这里必须宽松（object），这也是 loguru 自身的签名风格。
    """

    #: 颜文字常量表，便于 log.face.CHEER 这样取用
    face: ModuleType = faces_module

    def _emit(self, level: str, message: str, face: str | None = None, **fields: object) -> None:
        resolved = face or faces_module.DEFAULT_BY_LEVEL.get(level.upper(), "")
        merged = {**context_module.current(), **fields}
        logger.bind(face=resolved, fields=merged).log(level.upper(), message)

    def debug(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("DEBUG", message, face, **fields)

    def info(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("INFO", message, face, **fields)

    def success(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("SUCCESS", message, face, **fields)

    def warning(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("WARNING", message, face, **fields)

    def error(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("ERROR", message, face, **fields)

    def critical(self, message: str, face: str | None = None, **fields: object) -> None:
        self._emit("CRITICAL", message, face, **fields)

    def exception(self, message: str, face: str | None = None, **fields: object) -> None:
        """记录异常并附带 traceback。"""
        resolved = face or faces_module.DEFAULT_BY_LEVEL.get("ERROR", "")
        merged = {**context_module.current(), **fields}
        logger.bind(face=resolved, fields=merged).exception(message)

    @contextmanager
    def context(self, **kv: object) -> Iterator[None]:
        """临时附加业务维度到日志上下文，退出时自动还原。"""
        token = context_module.bind(**kv)
        try:
            yield
        finally:
            context_module.reset(token)


#: 全局日志对象
log = Log()


class InterceptHandler(logging.Handler):
    """把标准库 logging 记录转发给 loguru。"""

    @override
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level_name = logger.level(record.levelname).name
        except ValueError:
            level_name = str(record.levelno)

        frame: FrameType | None = logging.currentframe()
        depth = 1
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level_name, record.getMessage())


def setup_logging(settings: LogSettings | None = None) -> None:
    """装配日志。可重复调用，会先移除已有 sink。"""
    cfg = settings or get_settings().log

    # 返回的 handler id 在运行期不需要，显式赋给 _ 表示有意忽略
    _ = logger.remove()

    add_console_sink(cfg)
    app_log, error_log = add_file_sinks(cfg)

    _intercept_stdlib()

    log.info(
        "日志已就绪",
        face=faces_module.START,
        级别=cfg.level.upper(),
        主日志=str(app_log),
        错误日志=str(error_log),
    )


def _intercept_stdlib() -> None:
    """接管标准库与第三方库日志。"""
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in _INTERCEPTED_LOGGERS:
        target = logging.getLogger(name)
        target.handlers = [InterceptHandler()]
        target.propagate = False


__all__ = ["InterceptHandler", "Log", "log", "setup_logging"]
