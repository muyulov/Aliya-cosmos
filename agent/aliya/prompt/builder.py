"""提示词拼装：系统层只放准则、人格与技能索引。

工具索引与动态上下文（记忆、状态、任务、桌面、世界）作为一条独立的上下文消息
注入 messages，而不是塞进 system：它们每回合都变，放系统提示词会让缓存失效，
也容易让模型把「当前状态」当成永久设定。

工具结果的回填由 TurnRunner 负责，内容不截断；历史只按条数取最近若干条，
不按字符预算裁剪单条内容。
"""

from __future__ import annotations

from collections.abc import Sequence

from openai.types.chat import ChatCompletionMessageParam

from agent.aliya.turn.context import TurnContext
from core.agent import Tool, system_prompt
from core.llm import user


class PromptBuilder:
    def __init__(
        self,
        *,
        persona: str,
        charter: str,
        tools: Sequence[Tool],
        extra_index: str = "",
    ) -> None:
        self._persona: str = persona
        self._charter: str = charter
        self._tools: Sequence[Tool] = tools
        #: 技能索引；技能是稳定能力，留在 system 里
        self._extra_index: str = extra_index.strip()

    def build(self, *, turn: TurnContext) -> list[ChatCompletionMessageParam]:
        """system（准则/人格/技能）→ 上下文消息 → 历史 → 当前输入。"""
        system_text = system_prompt(self._charter, self._persona, self._extra_index)
        messages: list[ChatCompletionMessageParam] = [system_text]

        context = self._context_block(turn)
        if context:
            messages.append(user(f"[当前上下文]\n{context}"))

        # 历史窗口由上游（cosmos 的 conversation.turns）控制，这里不再二次裁剪
        messages.extend(turn.history_messages())
        messages.append(user(turn.user_text))
        return messages

    def _context_block(self, turn: TurnContext) -> str:
        """工具索引 + 动态上下文；空段直接跳过。"""
        parts = [
            part.strip()
            for part in (self._tool_help(), turn.context_text())
            if part is not None and part.strip()
        ]
        return "\n\n".join(parts)

    def _tool_help(self) -> str | None:
        """只放名称与描述首行。

        完整描述与参数 schema 已经随 `chat_tools` 的 tools 声明传给模型；
        这里再抄一遍只会重复占用上下文。
        """
        if not self._tools:
            return None
        lines = ["## 可用的平板工具"]
        for item in self._tools:
            summary = item.description.strip().splitlines()[0].strip() if item.description else ""
            lines.append(f"- {item.name}：{summary}" if summary else f"- {item.name}")
        return "\n".join(lines)
