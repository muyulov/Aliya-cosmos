"""工具模型测试：签名推导、docstring 解析、执行与返回值归一。

约定：
- 不打桩 pydantic：schema 与参数校验都真跑。
- 本地函数定义在测试体内时，注解里只引用模块级类型（get_type_hints 用模块全局解析）。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from functools import partial
from typing import Literal, cast

import pytest
from pydantic import BaseModel

from core.agent.errors import ToolDefinitionError
from core.agent.tool import Tool, tool


class _Point(BaseModel):
    x: int
    y: int


class _Unsupported:
    """pydantic 生成不出 schema 的类型。"""


def _props(item: Tool) -> dict[str, dict[str, object]]:
    return cast("dict[str, dict[str, object]]", item.parameters["properties"])


def test_从签名推导schema() -> None:
    def place(
        city: str,
        tags: list[str],
        point: _Point,
        mode: Literal["fast", "slow"] = "fast",
        note: str | None = None,
    ) -> str:
        """下个单。

        Args:
            city: 城市。
            tags: 标签。
            point: 坐标。
            mode: 模式。
            note: 备注。
        """
        return f"{city}{tags}{point}{mode}{note}"

    item = tool(place)

    assert item.name == "place"
    assert item.description == "下个单。"
    assert item.parameters["required"] == ["city", "tags", "point"]
    props = _props(item)
    assert props["city"]["description"] == "城市。"
    assert props["mode"]["enum"] == ["fast", "slow"]
    assert props["mode"]["default"] == "fast"
    assert props["note"]["default"] is None


def test_docstring参数段支持续行与空描述() -> None:
    def run(first: int, second: str) -> str:
        """跑一下。

        Args:
            first:
                分两行写的描述。
            second: 一句话。

            这是不成形的说明行。

        Returns:
            结果。
        """
        return f"{first}{second}"

    item = tool(run)

    assert item.description == "跑一下。"
    props = _props(item)
    assert props["first"]["description"] == "分两行写的描述。"
    assert props["second"]["description"] == "一句话。"


def test_无参工具的schema是空对象() -> None:
    def ping() -> None:
        """探活。"""

    item = tool(ping)

    assert item.parameters == {"type": "object", "properties": {}}
    assert asyncio.run(item.invoke("{}")) == "ok"


def test_同步工具在线程里执行() -> None:
    seen: list[bool] = []

    def probe() -> None:
        """记录执行线程。"""
        seen.append(threading.current_thread() is threading.main_thread())

    item = tool(probe)

    assert asyncio.run(item.invoke("{}")) == "ok"
    assert seen == [False]


def test_异步工具直接await() -> None:
    async def greet(name: str) -> str:
        """打招呼。

        Args:
            name: 名字。
        """
        return f"你好，{name}"

    item = tool(greet)

    assert asyncio.run(item.invoke('{"name": "世界"}')) == "你好，世界"


def test_返回值归一() -> None:
    def as_model() -> _Point:
        """返回模型。"""
        return _Point(x=1, y=2)

    def as_dict() -> dict[str, int]:
        """返回字典。"""
        return {"值": 1}

    def as_int() -> int:
        """返回整数。"""
        return 7

    assert asyncio.run(tool(as_model).invoke("{}")) == '{"x":1,"y":2}'
    assert asyncio.run(tool(as_dict).invoke("{}")) == '{"值": 1}'
    assert asyncio.run(tool(as_int).invoke("{}")) == "7"


def test_参数校验失败回填单行() -> None:
    def add(a: int) -> int:
        """加一。

        Args:
            a: 数字。
        """
        return a + 1

    item = tool(add)

    result = asyncio.run(item.invoke('{"a": "x"}'))
    assert result.startswith("参数校验失败：a: ")
    assert "\n" not in result

    root = asyncio.run(item.invoke("[]"))
    assert root.startswith("参数校验失败：参数: ")


def test_handler异常原样冒泡() -> None:
    def boom() -> str:
        """必定失败。"""
        raise RuntimeError("炸了")

    with pytest.raises(RuntimeError, match="炸了"):
        _ = asyncio.run(tool(boom).invoke("{}"))


def test_工具装饰后仍可直接调用() -> None:
    def double(value: int) -> int:
        """翻倍。

        Args:
            value: 数字。
        """
        return value * 2

    item = tool(double)

    assert item(3) == 6


def test_没有本地实现的工具调用即报错() -> None:
    async def invoke(arguments: str) -> str:
        return arguments

    item = Tool(name="remote", description="远程工具", parameters={}, invoke=invoke)

    with pytest.raises(TypeError, match="没有本地实现"):
        _ = item()


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(lambda: tool(lambda: None), id="无docstring"),
        pytest.param(lambda: tool(partial(lambda: None)), id="没有名字"),
    ],
)
def test_定义期错误_缺少名字或描述(factory: Callable[[], object]) -> None:
    with pytest.raises(ToolDefinitionError):
        _ = factory()


def test_定义期错误_只有参数段没有正文() -> None:
    def only_args(value: int) -> int:
        """Args:
        value: 数字。
        """
        return value

    with pytest.raises(ToolDefinitionError, match="docstring 描述"):
        _ = tool(only_args)


def test_定义期错误_星号参数() -> None:
    def varargs(*values: int) -> int:
        """求和。"""
        return sum(values)

    def kwargs(**values: int) -> int:
        """求和。"""
        return sum(values.values())

    with pytest.raises(ToolDefinitionError, match=r"\*args"):
        _ = tool(varargs)
    with pytest.raises(ToolDefinitionError, match=r"\*\*kwargs"):
        _ = tool(kwargs)


def test_定义期错误_缺少注解() -> None:
    def no_hint(value) -> int:  # pyright: ignore[reportUnknownParameterType, reportMissingParameterType, reportUnusedParameter]
        """原样返回。"""
        return 1

    with pytest.raises(ToolDefinitionError, match="缺少类型注解"):
        _ = tool(no_hint)  # pyright: ignore[reportUnknownArgumentType]


def test_定义期错误_注解无法解析() -> None:
    def dangling(value: int) -> str:
        """原样返回。"""
        return str(value)

    # 运行期把注解换成解析不到的名字，模拟前向引用写错
    dangling.__annotations__["value"] = "不存在的类型"

    with pytest.raises(ToolDefinitionError, match="类型注解无法解析"):
        _ = tool(dangling)


def test_定义期错误_类型不受支持() -> None:
    def weird(value: _Unsupported) -> str:
        """原样返回。"""
        return str(value)

    with pytest.raises(ToolDefinitionError, match="参数类型不受支持"):
        _ = tool(weird)
