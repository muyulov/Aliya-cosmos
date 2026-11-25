"""一个回合的输入上下文。

提示词只从这里取数据：人格、准则、工具说明由 PromptBuilder 负责，
presence / tasks / world / memory 由 runtime 填好后交给 `context_text()`。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from openai.types.chat import ChatCompletionMessageParam


@dataclass(slots=True)
class TurnContext:
    user_text: str = ""
    history: list[ChatCompletionMessageParam] = field(default_factory=list)
    presence: str | None = None
    tasks: str | None = None
    world: str | None = None
    memory: str | None = None
    session_id: str = ""
    message_id: str = ""
    sent: bool = False

    def context_text(self) -> str:
        """把可选上下文拼成一段；空段直接跳过。"""
        parts = [
            part.strip()
            for part in (self.memory, self.presence, self.tasks, self.world)
            if part is not None and part.strip()
        ]
        return "\n\n".join(parts)

    def history_messages(self) -> list[ChatCompletionMessageParam]:
        return list(self.history)
