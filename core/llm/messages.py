"""消息、图片与工具构造器。

约定：
- 调用方只碰这里的构造器，不 import openai SDK。
- 返回 SDK 的具体 TypedDict（不是 `ChatCompletionMessageParam` 那样的联合体）：
  联合体会放过拼错的键，具体类型才能在构造处报错。
- 构造器只做「填好字面量」，不封装成自有类型：消息结构就是 SDK 的结构。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageToolCallUnionParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)
from pydantic import BaseModel

#: 一条多模态内容里的图片片段
type ImagePart = ChatCompletionContentPartImageParam


@dataclass(frozen=True, slots=True)
class ToolCall:
    """一次工具调用。

    chat_tools() 返回它，assistant() 接收它回传第二轮：
    来回用同一个结构，调用方不必碰 SDK 的类型。

    arguments 是原始 JSON 字符串，不做解析：schema 只有调用方知道。
    """

    id: str
    name: str
    arguments: str


def system(text: str) -> ChatCompletionSystemMessageParam:
    """系统消息。

    统一用 system：OpenAI 新模型推荐 developer，但第三方兼容端点普遍只认 system。
    """
    return {"role": "system", "content": text}


def user(text: str) -> ChatCompletionUserMessageParam:
    """用户文本消息。"""
    return {"role": "user", "content": text}


def assistant(
    text: str | None = None, *, tool_calls: Sequence[ToolCall] | None = None
) -> ChatCompletionAssistantMessageParam:
    """助手消息：回传历史用纯文本，回传工具调用结果时带上 tool_calls。

    两者都给是合法的（部分模型边说边调工具）；都不给就是一条空消息。
    """
    message: ChatCompletionAssistantMessageParam = {"role": "assistant", "content": text}
    if tool_calls:
        # list 不变型：形参声明成联合类型，具体函数工具调用才能装进去
        calls: list[ChatCompletionMessageToolCallUnionParam] = [
            ChatCompletionMessageFunctionToolCallParam(
                id=call.id,
                type="function",
                function={"name": call.name, "arguments": call.arguments},
            )
            for call in tool_calls
        ]
        message["tool_calls"] = calls
    return message


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
        if isinstance(schema, type)  # 注解里 type[BaseModel] 与 Mapping 二选一
        else dict(schema)
    )
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }
