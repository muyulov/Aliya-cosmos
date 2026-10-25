"""工具注册表测试：声明顺序、重名、执行与异常回填。"""

from __future__ import annotations

from typing import cast

import pytest

from core.agent import ToolDefinitionError, ToolNotFoundError, ToolRegistry, tool
from core.llm import ToolCall


@tool
def echo(text: str) -> str:
    """回显文本。

    Args:
        text: 要回显的内容。
    """
    return text


@tool
def ping() -> str:
    """探活。"""
    return "pong"


@tool
def boom() -> str:
    """必定失败。"""
    raise RuntimeError("炸了")


def test_声明顺序与注册顺序一致() -> None:
    registry = ToolRegistry()
    registry.register(echo, ping)

    declarations = registry.declarations()

    functions = [
        cast("dict[str, object]", cast("object", item["function"])) for item in declarations
    ]
    assert [function["name"] for function in functions] == ["echo", "ping"]
    assert functions[0]["parameters"] == echo.parameters
    assert registry.names == ("echo", "ping")
    assert len(registry) == 2


def test_重名注册报错() -> None:
    registry = ToolRegistry()
    registry.register(echo)

    with pytest.raises(ToolDefinitionError, match="重名"):
        registry.register(echo)


async def test_未知工具名抛错() -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolNotFoundError, match="missing"):
        _ = await registry.execute(ToolCall(id="1", name="missing", arguments="{}"))


async def test_执行成功返回文本() -> None:
    registry = ToolRegistry()
    registry.register(echo)

    result = await registry.execute(ToolCall(id="1", name="echo", arguments='{"text": "嗨"}'))

    assert result == "嗨"


async def test_handler异常回填且不含堆栈() -> None:
    registry = ToolRegistry()
    registry.register(boom)

    result = await registry.execute(ToolCall(id="1", name="boom", arguments="{}"))

    assert result == "工具执行失败：[RuntimeError] 炸了"
    assert "Traceback" not in result


@pytest.mark.parametrize("arguments", ["", "{}", "随便什么"])
async def test_无参工具忽略arguments(arguments: str) -> None:
    registry = ToolRegistry()
    registry.register(ping)

    assert await registry.execute(ToolCall(id="1", name="ping", arguments=arguments)) == "pong"


async def test_参数校验失败不抛() -> None:
    registry = ToolRegistry()
    registry.register(echo)

    result = await registry.execute(ToolCall(id="1", name="echo", arguments='{"text": 1}'))

    assert result.startswith("参数校验失败：text: ")
