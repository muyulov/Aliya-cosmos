"""LLM 层对外出口。

用法：
    from core.llm import LLMService, system, user

    reply = await svc.chat([system("你是助手"), user("你好")])

消息与工具一律用本模块的构造器拼，业务代码不 import openai SDK。
"""

from core.llm.errors import (
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMRequestError,
    LLMResponseError,
    LLMSchemaError,
    LLMTimeoutError,
)
from core.llm.messages import (
    ImagePart,
    assistant,
    image_base64,
    image_url,
    system,
    tool,
    tool_result,
    user,
    user_with_images,
)
from core.llm.service import AssistantReply, LLMService, ToolCall

__all__ = [
    "AssistantReply",
    "ImagePart",
    "LLMConfigError",
    "LLMConnectionError",
    "LLMError",
    "LLMRequestError",
    "LLMResponseError",
    "LLMSchemaError",
    "LLMService",
    "LLMTimeoutError",
    "ToolCall",
    "assistant",
    "image_base64",
    "image_url",
    "system",
    "tool",
    "tool_result",
    "user",
    "user_with_images",
]
