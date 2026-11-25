"""四层提示词拼装：准则 → 人格 → 工具说明 → 上下文。"""

from __future__ import annotations

from collections.abc import Sequence

from openai.types.chat import ChatCompletionMessageParam

from agent.aliya.turn.context import TurnContext
from core.agent import Tool, system_prompt
from core.llm import user


class PromptBuilder:
    def __init__(self, *, persona: str, charter: str, tools: Sequence[Tool]) -> None:
        self._persona = persona
        self._charter = charter
        self._tools = tools

    def build(self, *, turn: TurnContext) -> list[ChatCompletionMessageParam]:
        system_text = system_prompt(
            self._charter,
            self._persona,
            self._tool_help(),
            turn.context_text(),
        )
        messages: list[ChatCompletionMessageParam] = [system_text]
        messages.extend(turn.history_messages())
        messages.append(user(turn.user_text))
        return messages

    def _tool_help(self) -> str | None:
        if not self._tools:
            return None
        lines = ["## 平板上的工具"]
        lines.extend(f"- {item.name}：{item.description}" for item in self._tools)
        return "\n".join(lines)
