"""四层提示词拼装：准则 → 人格 → 工具说明 → 上下文。"""

from __future__ import annotations

from collections.abc import Sequence

from openai.types.chat import ChatCompletionMessageParam

from agent.aliya.turn.context import TurnContext
from core.agent import Tool, system_prompt
from core.llm import user

#: 预算按字符近似（1 token ≈ 2 字符）；人格与准则永不参与裁剪。
DEFAULT_CONTEXT_MAX_TOKENS = 6000
DEFAULT_CONTEXT_MAX_CHARS = DEFAULT_CONTEXT_MAX_TOKENS * 2
MAX_HISTORY_MESSAGES = 12


class PromptBuilder:
    def __init__(
        self,
        *,
        persona: str,
        charter: str,
        tools: Sequence[Tool],
        max_chars: int | None = None,
    ) -> None:
        self._persona = persona
        self._charter = charter
        self._tools = tools
        self._max_chars = max_chars or DEFAULT_CONTEXT_MAX_CHARS

    def build(self, *, turn: TurnContext) -> list[ChatCompletionMessageParam]:
        tool_help = self._tool_help()
        fixed = "\n\n".join(
            part.strip()
            for part in (self._charter, self._persona, tool_help)
            if part is not None and part.strip()
        )
        context = turn.context_text()
        available = max(0, self._max_chars - len(fixed) - len(turn.user_text))
        if len(context) > available:
            context = context[:available].rstrip()
            available = 0
        else:
            available -= len(context)

        system_text = system_prompt(self._charter, self._persona, tool_help, context)
        messages: list[ChatCompletionMessageParam] = [system_text]
        messages.extend(self._fit_history(turn.history_messages(), available))
        messages.append(user(turn.user_text))
        return messages

    def _fit_history(
        self, history: list[ChatCompletionMessageParam], budget: int
    ) -> list[ChatCompletionMessageParam]:
        """从新到旧保留，最多 12 条；预算不够时优先丢旧消息。"""
        kept: list[ChatCompletionMessageParam] = []
        used = 0
        for message in reversed(history[-MAX_HISTORY_MESSAGES:]):
            content = message.get("content")
            length = len(content) if isinstance(content, str) else 0
            length += 16
            if used + length > budget:
                break
            kept.append(message)
            used += length
        kept.reverse()
        return kept

    def _tool_help(self) -> str | None:
        if not self._tools:
            return None
        lines = ["## 平板上的工具"]
        lines.extend(f"- {item.name}：{item.description}" for item in self._tools)
        return "\n".join(lines)
