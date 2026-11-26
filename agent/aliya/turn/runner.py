"""回合工具循环：LLM → tool_calls → 回填 → 再问，最多 max_rounds 轮。"""

from __future__ import annotations

import asyncio
import time

from agent.aliya.prompt.builder import PromptBuilder
from agent.aliya.turn.context import TurnContext
from core.agent import ToolRegistry
from core.config.settings import AgentAliyaSettings
from core.llm import LLMService, assistant, tool_result
from core.logger import log


class TurnRunner:
    def __init__(
        self,
        *,
        llm: LLMService,
        tools: ToolRegistry,
        builder: PromptBuilder,
        config: AgentAliyaSettings,
    ) -> None:
        self._llm: LLMService = llm
        self._tools: ToolRegistry = tools
        self._builder: PromptBuilder = builder
        self._config: AgentAliyaSettings = config

    async def run(self, *, turn: TurnContext) -> None:
        messages = self._builder.build(turn=turn)
        log.debug(
            "提示词已构建",
            来源=turn.origin,
            消息数=len(messages),
            上下文条目=turn.context_text().count("\n\n") + 1,
            输入字数=len(turn.user_text),
        )
        for round_index in range(1, self._config.reply.max_rounds + 1):
            started = time.monotonic()
            try:
                reply = await asyncio.wait_for(
                    self._llm.chat_tools(messages, self._tools.declarations()),
                    timeout=self._config.reply.turn_timeout_seconds,
                )
            except TimeoutError:
                log.warning(
                    "LLM 调用超时",
                    轮次=round_index,
                    超时秒=self._config.reply.turn_timeout_seconds,
                    来源=turn.origin,
                )
                raise
            log.debug(
                "LLM 已返回",
                轮次=round_index,
                耗时毫秒=round((time.monotonic() - started) * 1000),
                工具调用数=len(reply.tool_calls),
                回复字数=len(reply.content or ""),
                来源=turn.origin,
            )
            if reply.tool_calls:
                messages.append(assistant(reply.content, tool_calls=reply.tool_calls))
                for call in reply.tool_calls:
                    result = await self._tools.execute(call)
                    messages.append(tool_result(call.id, result))
                continue

            if reply.content:
                messages.append(assistant(reply.content))
                if not turn.sent:
                    _ = await turn.send(reply.content)
                return

            # 主动回合可能主动选择这次不开口（例如 defer_proactive）；此时不兜底。
            if turn.silent:
                log.debug("回合选择静默，不发送兜底回复", 轮次=round_index, 来源=turn.origin)
                return

            # 空回复既没有工具调用也没有内容：用户会收不到任何消息，按失败兜底。
            log.warning("模型返回空回复，发送兜底回复", 轮次=round_index, 来源=turn.origin)
            if not turn.sent:
                _ = await turn.send(self._config.reply.fallback_text)
            return

        log.warning(
            "达到最大工具轮次，发送兜底回复",
            轮次上限=self._config.reply.max_rounds,
            来源=turn.origin,
        )
        if not turn.sent and not turn.silent:
            _ = await turn.send(self._config.reply.fallback_text)
