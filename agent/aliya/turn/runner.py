"""回合工具循环：LLM → tool_calls → 回填 → 再问，最多 max_rounds 轮。"""

from __future__ import annotations

import asyncio

from agent.aliya.prompt.builder import PromptBuilder
from agent.aliya.turn.context import TurnContext
from core.agent import ToolRegistry
from core.config.settings import AgentAliyaSettings
from core.llm import LLMService, assistant, tool_result


class TurnRunner:
    def __init__(
        self,
        *,
        llm: LLMService,
        tools: ToolRegistry,
        builder: PromptBuilder,
        config: AgentAliyaSettings,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._builder = builder
        self._config = config

    async def run(self, *, turn: TurnContext) -> None:
        messages = self._builder.build(turn=turn)
        for _ in range(self._config.reply.max_rounds):
            reply = await asyncio.wait_for(
                self._llm.chat_tools(messages, self._tools.declarations()),
                timeout=self._config.reply.turn_timeout_seconds,
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
                    await turn.send(reply.content)
            return

        if not turn.sent:
            await turn.send(self._config.reply.fallback_text)
