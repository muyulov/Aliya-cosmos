"""工具注册表：声明导出与执行回填。

约定：
- 注册表是显式实例，没有模块级全局表：隐式共享状态会让测试互相污染，
  也让「哪些工具对哪个 agent 可见」说不清。
- 不加锁：约定装配期注册完、运行期只读。
- 执行只认 Tool.invoke；异常在这里统一转成回填文本，堆栈只进日志。
"""

from __future__ import annotations

import time

from openai.types.chat import ChatCompletionToolParam

from core.agent.errors import ToolDefinitionError, ToolNotFoundError
from core.agent.tool import Tool
from core.llm import ToolCall
from core.llm.messages import tool as llm_tool
from core.logger import log


class ToolRegistry:
    """按注册顺序保存工具，导出声明并按调用执行。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, *tools: Tool) -> None:
        """注册工具（可一次传多个）。重名立即报错，不覆盖。"""
        for item in tools:
            if item.name in self._tools:
                raise ToolDefinitionError(f"工具重名：{item.name}")
            self._tools[item.name] = item

    @property
    def names(self) -> tuple[str, ...]:
        """已注册的工具名，顺序即声明顺序。"""
        return tuple(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def declarations(self) -> list[ChatCompletionToolParam]:
        """给 LLM 的工具声明列表。

        复用 core.llm.tool()：声明结构只由 LLM 层定义一次，这里不自己拼 dict。
        """
        return [
            llm_tool(item.name, item.description, item.parameters) for item in self._tools.values()
        ]

    async def execute(self, call: ToolCall) -> str:
        """执行一次工具调用，返回可直接回填给模型的文本。

        未知工具名属于调用方契约破坏，抛错；参数与执行失败都回填成文本，
        让模型自己纠正（比整轮中断再重试省一次往返）。
        """
        item = self._tools.get(call.name)
        if item is None:
            raise ToolNotFoundError(call.name)
        begin = time.perf_counter()
        try:
            result = await item.invoke(call.arguments)
        except Exception as exc:
            self._log(item.name, begin, exc)
            return f"工具执行失败：[{type(exc).__name__}] {exc}"
        self._log(item.name, begin, None)
        return result

    @staticmethod
    def _log(name: str, begin: float, error: BaseException | None) -> None:
        """执行日志：只记工具名 / 耗时 / 成败 / 错误类型，不记参数与返回值。"""
        fields: dict[str, object] = {
            "工具": name,
            "耗时毫秒": round((time.perf_counter() - begin) * 1000, 1),
        }
        if error is None:
            fields["成功"] = True
        else:
            fields["成功"] = False
            fields["错误类型"] = type(error).__name__
        # face 显式声明：**fields 的值是 object，展开时会被逐个形参对账
        log.debug("工具执行完成", face=None, **fields)
