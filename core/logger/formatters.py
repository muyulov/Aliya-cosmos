"""日志格式化（纯函数，无副作用）。

三种输出形态（由 LogSettings.layout / json_output 选择）：
- 单行（默认）：字段以 `键=值` 内联到同一行，便于 grep 与按行采集。
- 树形：时间 [级别] 颜文字 消息，结构化字段以 ├─ / └─ 缩进成树。
- JSON：每行一个 JSON 对象，字段平铺，便于日志采集。

参考样式：
    2026-08-27 23:09:52 [I] (^_^)/ 用户回合已入队
        ├─ 参与者: qq:6329133635628374381
        └─ 已取消旧计划: 0

单行样式：
    2026-08-27 23:09:52 [I] (^_^)/ 用户回合已入队 | 参与者=qq:6329133635628374381 | 已取消旧计划=0

着色由各 format_* 的 color 参数控制，关掉时输出与不带颜色时逐字节一致，
因此文件 sink 复用同一套渲染逻辑而不必担心 ANSI 转义写进日志文件。

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

#: 时间戳与字段名用暗灰：彩色只为突出级别，不喧宾夺主
ANSI_GRAY = "\x1b[90m"
ANSI_RESET = "\x1b[0m"
FIELD_INDENT = "    "

#: 单行布局里字段之间的分隔符
LINE_SEPARATOR = " | "

#: JSON 输出的保留键：由日志框架写入，业务字段同名时一律让位
RESERVED_KEYS: frozenset[str] = frozenset({"time", "level", "message", "face", "exception"})


def _pick_face(face: object, level_name: str) -> str:
    """选出颜文字：显式指定优先，否则按级别取默认值。"""
    if isinstance(face, str) and face:
        return face
    return faces_module.DEFAULT_BY_LEVEL.get(level_name, "")


def format_tree(record: Record, *, stack: str | None = None, color: bool = False) -> str:
    """树形格式化，供控制台与文件 sink 使用。

    返回不带结尾换行的字符串，换行由 loguru 负责，避免多行内容被拼接。

    stack 由调用方预先算好并传入，而不再从 record 里取：多个 sink 共享同一
    record，谁先渲染谁就会清空 record["exception"]，后渲染的 sink 会丢失堆栈。
    """
    extra = _extra_of(record)
    level_name = _level_name_of(record)
    face = _pick_face(extra.get("face"), level_name)
    fields = _fields_of(extra)

    lines = [_head(record, level_name, face, color=color)]

    items = list(fields.items())
    for index, (key, value) in enumerate(items):
        # 有堆栈时末项也用 ├─：把 └─ 留给堆栈块，否则同一层会出现两个末项符号
        last = index == len(items) - 1 and not stack
        branch = "└─" if last else "├─"
        label = _paint(f"{FIELD_INDENT}{branch} {key}:", ANSI_GRAY, color=color)
        lines.append(f"{label} {render_value(value)}")

    lines.extend(_stack_lines(stack))
    return "\n".join(lines)


def format_line(record: Record, *, stack: str | None = None, color: bool = False) -> str:
    """单行格式化：结构化字段以 `键=值` 内联，一条记录占一行。

    消息与字段值里内嵌的换行会转义成字面 `\\n`，兑现「一条记录一行」；堆栈例外——
    它单独缩进成块，把 traceback 压成一行会彻底失去可读性，而按行采集的读取方
    本来也只关心消息与字段。
    """
    extra = _extra_of(record)
    level_name = _level_name_of(record)
    face = _pick_face(extra.get("face"), level_name)

    parts = [_head(record, level_name, face, color=color, one_line=True)]
    parts.extend(
        f"{_paint(key, ANSI_GRAY, color=color)}={_escape_newlines(render_value(value))}"
        for key, value in _fields_of(extra).items()
    )

    lines = [LINE_SEPARATOR.join(parts)]
    lines.extend(_stack_lines(stack))
    return "\n".join(lines)


def _head(
    record: Record, level_name: str, face: str, *, color: bool, one_line: bool = False
) -> str:
    """渲染首段：时间 [级别] 颜文字 消息。两种布局共用。

    one_line=True 时把消息里内嵌的换行一并转义，见 format_line 的约定。
    """
    timestamp = _time_of(record).strftime("%Y-%m-%d %H:%M:%S")
    level_color = LEVEL_COLORS.get(level_name, "")
    message = _message_of(record)
    parts = [
        _paint(timestamp, ANSI_GRAY, color=color),
        _paint(f"[{LEVEL_TAGS.get(level_name, 'I')}]", level_color, color=color),
    ]
    if face:
        parts.append(_paint(face, level_color, color=color))
    parts.append(
        _paint(_escape_newlines(message) if one_line else message, level_color, color=color)
    )
    return " ".join(parts)


def _escape_newlines(text: str) -> str:
    """把内嵌换行转成字面 `\\n`。"""
    return text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _paint(text: str, code: str, *, color: bool) -> str:
    """按需给片段上色。color=False、无色码或空文本时原样返回。"""
    if not color or not code or not text:
        return text
    return f"{code}{text}{ANSI_RESET}"


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
    """JSON 格式化，字段平铺到顶层；堆栈作为独立键存放。

    保留键（见 RESERVED_KEYS）由日志框架写入：与它们同名的业务字段一律让位，
    否则 `log.info("消息", level="伪造")` 这类误用会污染采集侧的字段契约。
    树形格式没有这个问题——业务字段独立成行，不与元数据混排。
    """
    extra = _extra_of(record)
    level_name = _level_name_of(record)

    payload: dict[str, object] = {
        "time": _time_of(record).strftime("%Y-%m-%d %H:%M:%S"),
        "level": level_name,
        "message": _message_of(record),
        "face": _pick_face(extra.get("face"), level_name),
    }
    # 逐键写入而不是 payload.update(...)：保留键不能被业务字段覆盖
    for key, value in _fields_of(extra).items():
        if key not in RESERVED_KEYS:
            payload[key] = value

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
    "RESERVED_KEYS",
    "format_exception",
    "format_json",
    "format_line",
    "format_tree",
    "render_value",
]
