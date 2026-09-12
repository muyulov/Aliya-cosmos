"""日志装配。

职责：
- 按配置装配控制台与文件两个 sink。
- 桥接标准库 logging（uvicorn、sqlalchemy 等）到 loguru，统一格式。
- 暴露 log 对象：调用 log.info("消息", face=..., 任意字段=值) 时字段自动成树。

导入关系：setuptools 依赖 config 与 formatters，formatters 只依赖 faces/types，
不存在导入环。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import FrameType, ModuleType
from typing import override

from loguru import logger

import core.logger.faces as faces_module
from core.config import LogSettings, get_settings
from core.logger.formatters import colorize, format_json, format_tree
from core.logger.types import FilterFunction, Record

_INTERCEPTED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
    "sqlalchemy",
    "asyncio",
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
        logger.bind(face=resolved, fields=fields).log(level.upper(), message)

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
        logger.bind(face=resolved, fields=fields).exception(message)

    def bind_request(self, request_id: str) -> None:
        """把 request_id 绑定到默认上下文，后续日志自动携带。"""
        _ = logger.configure(extra={"request_id": request_id})


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


def _is_tty() -> bool:
    """当前 stderr 是否连接到终端。"""
    stream = sys.stderr
    return hasattr(stream, "isatty") and bool(stream.isatty())


def _render_filter(cfg: LogSettings, *, color: bool) -> FilterFunction:
    """构造 per-sink filter：把 record 预渲染为完整文本。

    为什么用 filter 而不是 format 函数：loguru 的 format 传函数时，返回值里的换行
    会被它自己追加的换行逻辑吃掉，多行日志会挤成一行；而 filter 能在输出前改写
    record，配合 format="{message}" 可原样保留换行。

    filter 可能被 loguru 多次调用，因此改写必须幂等——用 extra 里的标记位判重，
    否则消息会被重复拼接。Record 是 TypedDict，直接按键读写即可。
    """

    def _filter(record: Record) -> bool:
        extra = record["extra"]
        if extra.get("_rendered"):
            return True

        text = format_json(record) if cfg.json_output else format_tree(record)
        if color and not cfg.json_output:
            text = colorize(text, record["level"].name)
        record["message"] = text
        extra["_rendered"] = True
        return True

    return _filter


def setup_logging(settings: LogSettings | None = None) -> None:
    """装配日志。可重复调用，会先移除已有 sink。"""
    cfg = settings or get_settings().log

    # 返回的 handler id 在运行期不需要，显式赋给 _ 表示有意忽略
    _ = logger.remove()

    # loguru 会在每条消息末尾自动追加换行，这里只原样透出已渲染好的 message
    _ = logger.add(
        sys.stderr,
        level=cfg.level.upper(),
        format="{message}",
        colorize=False,
        backtrace=False,
        filter=_render_filter(cfg, color=_is_tty()),
    )

    log_dir = Path(cfg.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    _ = logger.add(
        log_dir / cfg.file_name,
        level=cfg.level.upper(),
        format="{message}",
        rotation=cfg.rotation,
        retention=cfg.retention,
        compression=cfg.compression,
        encoding="utf-8",
        backtrace=False,
        filter=_render_filter(cfg, color=False),
    )

    _intercept_stdlib()

    _ = logger.configure(extra={"request_id": "-"})

    log.info(
        "日志已就绪",
        face=faces_module.START,
        级别=cfg.level.upper(),
        输出=str(log_dir / cfg.file_name),
    )


def _intercept_stdlib() -> None:
    """接管标准库与第三方库日志。"""
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in _INTERCEPTED_LOGGERS:
        target = logging.getLogger(name)
        target.handlers = [InterceptHandler()]
        target.propagate = False


__all__ = ["InterceptHandler", "Log", "log", "setup_logging"]
