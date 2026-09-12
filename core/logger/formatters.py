"""日志格式化（纯函数，无副作用）。

两种输出形态：
- 树形（默认）：时间 [级别] 颜文字 消息，结构化字段以 ├─ / └─ 缩进成树。
- JSON：每行一个 JSON 对象，字段平铺，便于日志采集。

参考样式：
    2026-08-27 23:09:52 [I] (^_^)/ 用户回合已入队
        ├─ 参与者: qq:6329133635628374381
        └─ 已取消旧计划: 0

本模块只依赖 types 与 faces，不导入 setup，因此不构成导入环。
"""

from __future__ import annotations

import json
import traceback
from datetime import datetime
from typing import cast

import core.logger.faces as faces_module
from core.logger.types import Record

#: 级别首字母标记，用于 [I] / [E] 这类紧凑写法
LEVEL_TAGS: dict[str, str] = {
    "TRACE": "T",
    "DEBUG": "D",
    "INFO": "I",
    "SUCCESS": "S",
    "WARNING": "W",
    "ERROR": "E",
    "CRITICAL": "C",
}

#: 各级别对应的 ANSI 颜色（控制台使用）
LEVEL_COLORS: dict[str, str] = {
    "TRACE": "\x1b[37m",
    "DEBUG": "\x1b[36m",
    "INFO": "\x1b[32m",
    "SUCCESS": "\x1b[32m",
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[31m",
    "CRITICAL": "\x1b[1;31m",
}

RESET = "\x1b[0m"
FIELD_INDENT = "    "


def _pick_face(face: object, level_name: str) -> str:
    """选出颜文字：显式指定优先，否则按级别取默认值。"""
    if isinstance(face, str) and face:
        return face
    return faces_module.DEFAULT_BY_LEVEL.get(level_name, "")


def format_tree(record: Record, *, stack: str | None = None) -> str:
    """树形格式化，供控制台与文件 sink 使用。

    返回不带结尾换行的字符串，换行由 loguru 负责，避免多行内容被拼接。

    stack 由调用方预先算好并传入，而不再从 record 里取：多个 sink 共享同一
    record，谁先渲染谁就会清空 record["exception"]，后渲染的 sink 会丢失堆栈。
    """
    extra = _extra_of(record)
    level_name = _level_name_of(record)
    face = _pick_face(extra.get("face"), level_name)
    fields = _fields_of(extra)
    timestamp = _time_of(record).strftime("%Y-%m-%d %H:%M:%S")

    head = f"{timestamp} [{LEVEL_TAGS.get(level_name, 'I')}]"
    if face:
        head = f"{head} {face}"
    lines = [f"{head} {_message_of(record)}"]

    items = list(fields.items())
    for index, (key, value) in enumerate(items):
        branch = "└─" if index == len(items) - 1 else "├─"
        lines.append(f"{FIELD_INDENT}{branch} {key}: {render_value(value)}")

    lines.extend(_stack_lines(stack))
    return "\n".join(lines)


def format_exception(record: Record) -> str | None:
    """把 record 里的异常渲染成堆栈文本，无异常时返回 None。

    单独渲染而非交给 loguru：loguru 会把堆栈拼在 message 之后，与字段树混在
    一起，多行结构失去可读性。
    """
    exception = record.get("exception")
    if not exception:
        return None
    stack = traceback.format_exception(exception.type, exception.value, exception.traceback)
    return "".join(stack).rstrip("\n")


def _stack_lines(stack: str | None) -> list[str]:
    """把堆栈文本转成树形块的行。"""
    if not stack:
        return []
    lines = [f"{FIELD_INDENT}└─ 堆栈"]
    lines.extend(f"{FIELD_INDENT}   {raw}" for raw in stack.split("\n"))
    return lines


def format_json(record: Record, *, stack: str | None = None) -> str:
    """JSON 格式化，字段平铺到顶层；堆栈作为独立键存放。"""
    extra = _extra_of(record)
    level_name = _level_name_of(record)

    payload: dict[str, object] = {
        "time": _time_of(record).strftime("%Y-%m-%d %H:%M:%S"),
        "level": level_name,
        "message": _message_of(record),
        "face": _pick_face(extra.get("face"), level_name),
    }
    payload.update(_fields_of(extra))

    if stack:
        payload["exception"] = stack

    return json.dumps(payload, ensure_ascii=False, default=str)


def render_value(value: object) -> str:
    """把字段值渲染为单行文本。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False, default=str)


def colorize(text: str, level_name: str) -> str:
    """按级别给整行着色，仅控制台使用。只包裹第一行，避免字段块被染色。"""
    color = LEVEL_COLORS.get(level_name, "")
    if not color:
        return text
    head, separator, tail = text.partition("\n")
    colored = f"{color}{head}{RESET}"
    return f"{colored}{separator}{tail}" if separator else colored


def _extra_of(record: Record) -> dict[str, object]:
    """取出 record 的 extra。Record 是 TypedDict，extra 始终存在。"""
    extra = record["extra"]
    return dict(extra)


def _fields_of(extra: dict[str, object]) -> dict[str, object]:
    """从 extra 里取出结构化字段。

    loguru 的 extra 声明为 Dict[Any, Any]，这里规整为 str 键的字典，
    保证后续渲染取值有确定的键类型。
    """
    fields = extra.get("fields")
    if not isinstance(fields, dict):
        return {}
    typed = cast("dict[object, object]", fields)
    return {str(key): value for key, value in typed.items()}


def _level_name_of(record: Record) -> str:
    """取出级别名。"""
    return record["level"].name


def _time_of(record: Record) -> datetime:
    """取出日志时间。"""
    return record["time"]


def _message_of(record: Record) -> str:
    """取出日志消息。"""
    return record["message"]


__all__ = [
    "FIELD_INDENT",
    "LEVEL_COLORS",
    "LEVEL_TAGS",
    "colorize",
    "format_exception",
    "format_json",
    "format_tree",
    "render_value",
]
