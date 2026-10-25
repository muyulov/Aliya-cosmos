"""系统提示拼装。

约定：
- 只做「分段 → 一条 system 消息」：去首尾空白、跳过空段、空行连接。
- 不做模板插值：变量由调用方 f-string 填好再传进来，不引入第二套字符串语法。
- 不做长度裁剪：裁剪与会话历史绑定，属会话层的事。
"""

from __future__ import annotations

from openai.types.chat import ChatCompletionSystemMessageParam

from core.agent.errors import PromptError
from core.llm.messages import system


def system_prompt(*sections: str | None) -> ChatCompletionSystemMessageParam:
    """把若干段文本合成一条 system 消息。

    `None` 与纯空白段会被跳过，因此可以放心写 `system_prompt(角色, 可选上下文)`；
    全部为空时报错——没有内容的 system 消息只会白占 token。
    """
    blocks = [section.strip() for section in sections if section is not None and section.strip()]
    if not blocks:
        raise PromptError("系统提示至少需要一段非空内容")
    return system("\n\n".join(blocks))
