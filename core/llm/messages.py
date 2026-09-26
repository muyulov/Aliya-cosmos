"""消息、图片与工具构造器。

约定：
- 调用方只碰这里的构造器，不 import openai SDK。
- 返回 SDK 的具体 TypedDict（不是 `ChatCompletionMessageParam` 那样的联合体）：
  联合体会放过拼错的键，具体类型才能在构造处报错。
- 构造器只做「填好字面量」，不封装成自有类型：消息结构就是 SDK 的结构。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)
from pydantic import BaseModel

#: 一条多模态内容里的图片片段
type ImagePart = ChatCompletionContentPartImageParam


def system(text: str) -> ChatCompletionSystemMessageParam:
    """系统消息。

    统一用 system：OpenAI 新模型推荐 developer，但第三方兼容端点普遍只认 system。
    """
    return {"role": "system", "content": text}


def user(text: str) -> ChatCompletionUserMessageParam:
    """用户文本消息。"""
    return {"role": "user", "content": text}


def assistant(text: str) -> ChatCompletionAssistantMessageParam:
    """助手消息，用于回传历史。"""
    return {"role": "assistant", "content": text}


def tool_result(call_id: str, content: str) -> ChatCompletionToolMessageParam:
    """工具执行结果：按 call_id 回填给对应的 tool_call。"""
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def image_url(url: str) -> ImagePart:
    """图片片段：传可访问的 URL。"""
    return {"type": "image_url", "image_url": {"url": url}}


def image_base64(data: str, media_type: str = "image/png") -> ImagePart:
    """图片片段：传 base64 编码后的字节（不含 data URI 前缀）。"""
    return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}


def user_with_images(text: str, *images: ImagePart) -> ChatCompletionUserMessageParam:
    """带图片的用户消息：文字在前，图片按传入顺序跟在后面。"""
    parts: list[ChatCompletionContentPartTextParam | ImagePart] = [
        {"type": "text", "text": text},
        *images,
    ]
    return {"role": "user", "content": parts}


def tool(
    name: str, description: str, schema: type[BaseModel] | Mapping[str, object]
) -> ChatCompletionToolParam:
    """工具声明：schema 可以是 pydantic 模型，也可以是手写 JSON Schema 字典。"""
    parameters: dict[str, object] = (
        cast("dict[str, object]", schema.model_json_schema())
        if isinstance(schema, type) and issubclass(schema, BaseModel)
        else dict(schema)
    )
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }
