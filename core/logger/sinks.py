"""日志 sink 装配。

三个 sink 共用同一套渲染逻辑，仅在输出目标、级别门槛与着色上有差异：
- 控制台 stderr：级别跟随配置，仅在 TTY 下着色。
- 主日志文件：级别跟随配置，按天轮转。
- 错误日志文件：级别固定 ERROR，按天轮转，便于运维只翻错误。

为什么用 filter 而不是 format 函数：loguru 的 format 传函数时，返回值里的换行
会被它自己追加的换行逻辑吃掉，多行日志会挤成一行；而 filter 能在输出前改写
record，配合 format="{message}" 可原样保留换行。
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from core.config import LogSettings
from core.logger.formatters import colorize, format_json, format_tree
from core.logger.types import FilterFunction, Record

#: 错误日志文件的级别门槛
ERROR_LEVEL = "ERROR"


def _is_tty() -> bool:
    """当前 stderr 是否连接到终端。"""
    stream = sys.stderr
    return hasattr(stream, "isatty") and bool(stream.isatty())


def build_filter(cfg: LogSettings, *, color: bool) -> FilterFunction:
    """构造 per-sink filter：把 record 预渲染为完整文本。

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
        # 堆栈已由 format_tree/format_json 渲染，清空以免 loguru 重复追加
        record["exception"] = None
        return True

    return _filter


def add_console_sink(cfg: LogSettings) -> None:
    """控制台 sink。"""
    _ = logger.add(
        sys.stderr,
        level=cfg.level.upper(),
        format="{message}",
        colorize=False,
        backtrace=False,
        diagnose=False,
        filter=build_filter(cfg, color=_is_tty()),
    )


def add_file_sinks(cfg: LogSettings) -> tuple[Path, Path]:
    """装配主日志与错误日志两个文件 sink，返回两者的路径。"""
    log_dir = Path(cfg.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    app_log = log_dir / cfg.file_name
    error_log = log_dir / cfg.error_file_name

    common: dict[str, object] = {
        "format": "{message}",
        "rotation": cfg.rotation,
        "retention": cfg.retention,
        "compression": cfg.compression,
        "encoding": "utf-8",
        "backtrace": False,
        "diagnose": False,
    }

    _ = logger.add(
        app_log,
        level=cfg.level.upper(),
        filter=build_filter(cfg, color=False),
        **common,  # type: ignore[arg-type]
    )
    _ = logger.add(
        error_log,
        level=ERROR_LEVEL,
        filter=build_filter(cfg, color=False),
        **common,  # type: ignore[arg-type]
    )
    return app_log, error_log


__all__ = [
    "ERROR_LEVEL",
    "add_console_sink",
    "add_file_sinks",
    "build_filter",
]
