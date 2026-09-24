"""日志格式化测试。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import cast

from core.logger import faces
from core.logger.formatters import (
    colorize,
    format_exception,
    format_json,
    format_tree,
    render_value,
)
from core.logger.types import Record


@dataclass
class FakeLevel:
    """模拟 loguru 的级别对象。"""

    name: str


def make_record(
    message: str = "消息",
    *,
    level: str = "INFO",
    face: str = "",
    exception: object = None,
    **fields: object,
) -> Record:
    """构造一个字段形状与 loguru Record 一致的最小记录。

    loguru 的 Record 是 TypedDict，渲染函数按 record["key"] 取值，
    因此这里直接造 dict。Record 的必填字段远多于渲染所需，故用 cast
    声明这是有意为之的测试替身。
    """
    raw: dict[str, object] = {
        "message": message,
        "level": FakeLevel(level),
        "time": datetime(2026, 8, 27, 23, 9, 52),
        "exception": exception,
        "extra": {"face": face, "fields": fields},
    }
    return cast(Record, cast(object, raw))


def test_无字段时输出单行() -> None:
    line = format_tree(make_record("无字段的单行日志", level="WARNING"))
    assert "\n" not in line
    assert "└─" not in line and "├─" not in line


def test_单字段使用末项分支() -> None:
    line = format_tree(make_record("用户消息", face=faces.LOVE, 参与者="Alice"))
    lines = line.split("\n")
    assert lines[0] == "2026-08-27 23:09:52 [I] (*^▽^*) 用户消息"
    assert lines[1] == "    └─ 参与者: Alice"


def test_多字段树形渲染与参考样式一致() -> None:
    line = format_tree(
        make_record("用户回合已入队", 参与者="qq:6329133635628374381", 已取消旧计划=0)
    )
    assert line == (
        "2026-08-27 23:09:52 [I] (^_^)/ 用户回合已入队\n"
        "    ├─ 参与者: qq:6329133635628374381\n"
        "    └─ 已取消旧计划: 0"
    )


def test_未指定颜文字时按级别回填默认值() -> None:
    line = format_tree(make_record("默认颜文字", level="ERROR"))
    assert faces.BOOM in line


def test_字段保持声明顺序() -> None:
    line = format_tree(make_record("顺序", 甲=1, 乙=2, 丙=3))
    keys = [ln.split("─ ")[1].split(":")[0] for ln in line.split("\n")[1:]]
    assert keys == ["甲", "乙", "丙"]


def test_多行渲染不污染后续记录() -> None:
    """确保渲染函数无副作用：同一 record 连续渲染两次结果一致。"""
    record = make_record("用户回合已入队", 参与者="qq:1", 已取消旧计划=0)
    assert format_tree(record) == format_tree(record)


def test_布尔与复杂值渲染() -> None:
    line = format_tree(make_record("值", 开关=True, 列表=[1, 2]))
    assert "开关: true" in line
    assert "列表: [1, 2]" in line


def test_级别标记映射() -> None:
    assert "[E]" in format_tree(make_record("错误", level="ERROR"))
    assert "[D]" in format_tree(make_record("调试", level="DEBUG"))
    assert "[C]" in format_tree(make_record("致命", level="CRITICAL"))


def test_json_模式字段平铺() -> None:
    import json

    raw = format_json(make_record("调用开始", face=faces.START, 模型="Flash"))
    payload = cast("dict[str, object]", json.loads(raw))
    assert payload["level"] == "INFO"
    assert payload["message"] == "调用开始"
    assert payload["模型"] == "Flash"
    assert "fields" not in payload


def test_着色只包裹第一行() -> None:
    text = "第一行\n    └─ 键: 值"
    colored = colorize(text, "ERROR")
    first, _, rest = colored.partition("\n")
    assert first.startswith("\x1b[31m") and first.endswith("\x1b[0m")
    assert "\x1b[" not in rest


def test_渲染各类值() -> None:
    assert render_value("文本") == "文本"
    assert render_value(True) == "true"
    assert render_value(False) == "false"
    assert render_value(12) == "12"
    assert render_value(None) == "null"
    assert render_value({"甲": 1}) == '{"甲": 1}'


class FakeException:
    """模拟 loguru 的 RecordException（含 type/value/traceback 三元组）。"""

    def __init__(self, exc: BaseException) -> None:
        self.type: type[BaseException] = type(exc)
        self.value: BaseException = exc
        self.traceback: TracebackType | None = exc.__traceback__


def _make_exception() -> FakeException:
    try:
        raise KeyError("item_id")
    except KeyError as exc:
        return FakeException(exc)


def test_无堆栈时不渲染堆栈块() -> None:
    line = format_tree(make_record("正常"))
    assert "堆栈" not in line


def test_format_exception_从_record_提取堆栈() -> None:
    text = format_exception(make_record("异常", exception=_make_exception()))
    assert text is not None
    assert "KeyError" in text
    assert text.endswith("KeyError: 'item_id'")
    assert not text.endswith("\n")


def test_format_exception_无异常时返回_None() -> None:
    assert format_exception(make_record("正常")) is None


def test_传入堆栈时字段块完整且堆栈独立成块() -> None:
    record = make_record("未捕获异常", level="ERROR", 路径="/x")
    line = format_tree(record, stack=format_exception(make_record(exception=_make_exception())))
    lines = line.split("\n")
    assert lines[0].startswith("2026-08-27 23:09:52 [E]")
    assert lines[1] == "    └─ 路径: /x"
    assert lines[2] == "    └─ 堆栈"
    body = "\n".join(lines[3:])
    assert "KeyError" in body


def test_堆栈渲染不会把_exception_塞进字段() -> None:
    line = format_tree(make_record("异常"))
    assert "exception:" not in line


def test_json_模式下堆栈作为独立键且保持单行() -> None:
    import json

    stack = format_exception(make_record(exception=_make_exception()))
    raw = format_json(make_record("未捕获异常"), stack=stack)
    assert "\n" not in raw
    payload = cast("dict[str, object]", json.loads(raw))
    assert isinstance(payload["exception"], str)
    assert "KeyError" in payload["exception"]
