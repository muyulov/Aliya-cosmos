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
from core.logger.formatters import colorize, format_exception, format_json, format_tree
from core.logger.types import FilterFunction, Record

#: 错误日志文件的级别门槛
ERROR_LEVEL = "ERROR"


def _is_tty() -> bool:
    """当前 stderr 是否连接到终端。"""
    stream = sys.stderr
    return hasattr(stream, "isatty") and bool(stream.isatty())


def build_filter(cfg: LogSettings, *, color: bool) -> FilterFunction:
    """构造 per-sink filter：把 record 渲染为完整文本。

    必须了解的 loguru 行为：所有 sink 的 filter 共享**同一个 record 对象**，
    并按 sink 注册顺序串行执行。也就是说前一个 sink 对 record["message"] 的
    改写，后一个 sink 会原样看到。这带来两个坑：

    1. 若用"只渲染一次"的标记位，先执行的控制台 sink 会着色并写回 message，
       后续文件 sink 直接复用带 ANSI 码的文本，把转义序列写进日志文件。
    2. 若谁先渲染谁就清空 record["exception"]，后渲染的 sink 会丢失堆栈。

    因此这里采取两个对策：**每个 sink 无条件重新渲染自己的 message**（不设
    渲染标记，不依赖执行顺序），以及**堆栈文本只提取一次并缓存到 extra**
    供所有 sink 复用。

    为什么用 filter 而不是 format 函数：loguru 的 format 传函数时，返回值里的
    换行会被它自己追加的换行逻辑吃掉，多行日志会挤成一行；而 filter 能在输出前
    改写 record，配合 format="{message}" 可原样保留换行。
    """

    #: 堆栈文本的缓存键：跨 sink 共享，只算一次
    stack_key = "_stack_text"
    #: 原始消息的缓存键：message 会被各 sink 改写，必须留一份原文
    raw_key = "_raw_message"

    def _filter(record: Record) -> bool:
        extra = record["extra"]

        # 首次进入时留存原始消息与堆栈，供所有 sink 复用
        if raw_key not in extra:
            extra[raw_key] = record["message"]
            extra[stack_key] = format_exception(record)
            # 堆栈已自行渲染，清空以免 loguru 再追加一份原始堆栈
            record["exception"] = None

        raw = extra.get(raw_key)
        stack = extra.get(stack_key)
        record["message"] = raw if isinstance(raw, str) else ""

        rendered = (
            format_json(record, stack=stack if isinstance(stack, str) else None)
            if cfg.json_output
            else format_tree(record, stack=stack if isinstance(stack, str) else None)
        )
        if color and not cfg.json_output:
            rendered = colorize(rendered, record["level"].name)
        record["message"] = rendered
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


def _add_file_sink(target: Path, level: str, cfg: LogSettings) -> None:
    """装配单个文件 sink。

    两个文件 sink 只有目标路径与级别不同，格式、轮转、编码与关闭规则完全一致，
    因此收在这里，避免同一张参数表抄两遍。

    路径显式转成 str：loguru 的类型标注把"文件路径"那条重载声明为 str，
    传 Path 会被类型检查器判为不匹配（运行期两者都能用）。
    """
    _ = logger.add(
        str(target),
        level=level,
        format="{message}",
        rotation=cfg.rotation,
        retention=cfg.retention,
        compression=cfg.compression,
        encoding="utf-8",
        backtrace=False,
        diagnose=False,
        filter=build_filter(cfg, color=False),
    )


def add_file_sinks(cfg: LogSettings) -> tuple[Path, Path]:
    """装配主日志与错误日志两个文件 sink，返回两者的路径。"""
    log_dir = Path(cfg.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    app_log = log_dir / cfg.file_name
    error_log = log_dir / cfg.error_file_name

    _add_file_sink(app_log, cfg.level.upper(), cfg)
    _add_file_sink(error_log, ERROR_LEVEL, cfg)
    return app_log, error_log


__all__ = [
    "ERROR_LEVEL",
    "add_console_sink",
    "add_file_sinks",
    "build_filter",
]
