"""LLM 服务测试。

约定：
- 不走 start() 建客户端：容器只认配置节点，客户端只能白盒塞进 service._client，
  因此除了「未配 key」那条走容器外，其余用例直接构造服务再注入假客户端。
- 不起 mock server、不 mock HTTP：假客户端按预设返回 SDK 风格的假对象。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, cast

import httpx2
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    omit,
)
from openai.types import CompletionUsage, CreateEmbeddingResponse, Embedding
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessage,
    ChatCompletionMessageToolCall,
    ChatCompletionMessageToolCallUnion,
)
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_chunk import ChoiceDelta
from openai.types.chat.chat_completion_message_tool_call import Function
from openai.types.create_embedding_response import Usage
from pydantic import BaseModel, ValidationError

from core.config import LLMSettings, Settings
from core.llm import (
    LLMConfigError,
    LLMConnectionError,
    LLMRequestError,
    LLMResponseError,
    LLMSchemaError,
    LLMService,
    LLMTimeoutError,
    ToolCall,
    assistant,
    image_url,
    tool,
    tool_result,
    user,
    user_with_images,
)
from core.service.base import ServiceState
from core.service.manager import ServiceManager

#: 测试用的端点，只用于构造假请求
_URL = "https://api.openai.com/v1/chat/completions"


class _FakeCompletions:
    """chat.completions 替身：记录入参，按预设返回或抛错。"""

    def __init__(self, owner: _FakeClient) -> None:
        self.owner: _FakeClient = owner

    async def create(self, **kwargs: object) -> object:
        self.owner.calls.append(kwargs)
        if self.owner.error is not None:
            raise self.owner.error
        if kwargs.get("stream"):
            return self._chunks()
        return self.owner.completion

    async def _chunks(self) -> AsyncIterator[ChatCompletionChunk]:
        for chunk in self.owner.chunks:
            yield chunk


class _FakeChat:
    """client.chat 替身。"""

    def __init__(self, owner: _FakeClient) -> None:
        self.completions: _FakeCompletions = _FakeCompletions(owner)


class _FakeEmbeddings:
    """client.embeddings 替身。"""

    def __init__(self, owner: _FakeClient) -> None:
        self.owner: _FakeClient = owner

    async def create(self, **kwargs: object) -> CreateEmbeddingResponse:
        self.owner.calls.append(kwargs)
        if self.owner.error is not None:
            raise self.owner.error
        assert self.owner.embedding is not None
        return self.owner.embedding


class _FakeClient:
    """AsyncOpenAI 替身：鸭子类型，只实现被测代码用到的三个入口。"""

    def __init__(
        self,
        completion: ChatCompletion | None = None,
        chunks: tuple[ChatCompletionChunk, ...] = (),
        embedding: CreateEmbeddingResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.close_count: int = 0
        self.completion: ChatCompletion | None = completion
        self.chunks: tuple[ChatCompletionChunk, ...] = chunks
        self.embedding: CreateEmbeddingResponse | None = embedding
        self.error: Exception | None = error
        self.chat: _FakeChat = _FakeChat(self)
        self.embeddings: _FakeEmbeddings = _FakeEmbeddings(self)

    async def close(self) -> None:
        self.close_count += 1


class Item(BaseModel):
    """结构化输出的 schema。"""

    name: str
    price: float


def _service(
    *,
    api_key: str = "k",
    chat_model: str = "chat-m",
    embed_model: str = "embed-m",
    vision_model: str = "",
    structured_mode: Literal["json_schema", "json_object"] = "json_schema",
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> LLMService:
    return LLMService(
        LLMSettings(
            api_key=api_key,
            chat_model=chat_model,
            embed_model=embed_model,
            vision_model=vision_model,
            structured_mode=structured_mode,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    )


def _attach(service: LLMService, client: _FakeClient) -> None:
    """塞入替身：容器无法注入客户端，这是唯一接缝。"""
    service._client = cast(  # pyright: ignore[reportPrivateUsage]
        "AsyncOpenAI", cast("object", client)
    )


def _completion(
    content: str | None = "你好",
    tool_calls: list[ChatCompletionMessageToolCallUnion] | None = None,
) -> ChatCompletion:
    return ChatCompletion(
        id="c1",
        choices=[
            Choice(
                finish_reason="stop",
                index=0,
                message=ChatCompletionMessage(
                    role="assistant", content=content, tool_calls=tool_calls
                ),
            )
        ],
        created=0,
        model="chat-m",
        object="chat.completion",
        usage=CompletionUsage(completion_tokens=2, prompt_tokens=1, total_tokens=3),
    )


def _chunk(text: str | None) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="c1",
        choices=[ChunkChoice(delta=ChoiceDelta(content=text), index=0)],
        created=0,
        model="chat-m",
        object="chat.completion.chunk",
    )


def _embedding(*vectors: list[float]) -> CreateEmbeddingResponse:
    return CreateEmbeddingResponse(
        data=[
            Embedding(index=index, embedding=vector, object="embedding")
            for index, vector in enumerate(vectors)
        ],
        model="embed-m",
        object="list",
        usage=Usage(prompt_tokens=3, total_tokens=3),
    )


def _status_error(status: int = 500, request_id: str = "req-1") -> APIStatusError:
    response = httpx2.Response(
        status, request=httpx2.Request("POST", _URL), headers={"x-request-id": request_id}
    )
    return APIStatusError("boom", response=response, body=None)


# ---- 生命周期与配置 ----


async def test_未配key时start不建客户端() -> None:
    mgr = ServiceManager(Settings(llm=LLMSettings(api_key="")))
    _ = mgr.register(LLMService)
    await mgr.start_all()

    service = mgr.get(LLMService)
    assert service.state is ServiceState.RUNNING
    status = await service.health()
    assert status.healthy is False
    assert "未配置" in status.detail


async def test_未配key时调用才报错() -> None:
    service = _service(api_key="")
    await service.start()

    with pytest.raises(LLMConfigError, match="api_key"):
        _ = await service.chat([user("你好")])


async def test_embed_model留空时embed报错() -> None:
    service = _service(embed_model="")
    _attach(service, _FakeClient())

    with pytest.raises(LLMConfigError, match="embed_model"):
        _ = await service.embed(["文本"])


def test_vision_model留空时复用chat_model() -> None:
    assert _service(vision_model="").vision_model == "chat-m"
    assert _service(vision_model="vision-m").vision_model == "vision-m"


async def test_start重复调用不重建客户端() -> None:
    """幂等看客户端而不是状态：重复 start 不该把已建的客户端顶掉（前一个会泄漏）。"""
    service = _service()
    await service.start()
    client = service._client  # pyright: ignore[reportPrivateUsage]

    await service.start()
    assert service._client is client  # pyright: ignore[reportPrivateUsage]


async def test_有客户端时健康检查为健康() -> None:
    service = _service()
    _attach(service, _FakeClient())

    status = await service.health()
    assert status.healthy is True
    assert status.detail == ""


def test_非法timeout与retries被拒() -> None:
    with pytest.raises(ValidationError):
        _ = LLMSettings(timeout=0)
    with pytest.raises(ValidationError):
        _ = LLMSettings(retries=-1)


# ---- 对话与流式 ----


async def test_chat返回正文() -> None:
    service = _service()
    client = _FakeClient(completion=_completion("你好"))
    _attach(service, client)

    assert await service.chat([user("在吗")]) == "你好"
    assert client.calls[0]["model"] == "chat-m"


async def test_未设置temperature与max_tokens时不传该参数() -> None:
    """显式 None 会被 SDK 序列化成 JSON null，必须换成 omit（不传）。"""
    service = _service()
    client = _FakeClient(completion=_completion())
    _attach(service, client)

    _ = await service.chat([user("在吗")])
    assert client.calls[0]["temperature"] is omit
    assert client.calls[0]["max_tokens"] is omit


async def test_配置了temperature时传给端点() -> None:
    service = _service(temperature=0.3)
    client = _FakeClient(completion=_completion())
    _attach(service, client)

    _ = await service.chat([user("在吗")])
    assert client.calls[0]["temperature"] == 0.3


async def test_content为None时报LLMResponseError() -> None:
    service = _service()
    _attach(service, _FakeClient(completion=_completion(None)))

    with pytest.raises(LLMResponseError):
        _ = await service.chat([user("在吗")])


async def test_stream逐段产出且跳过空片段() -> None:
    service = _service()
    _attach(
        service,
        _FakeClient(chunks=(_chunk("你"), _chunk(None), _chunk("好"))),
    )

    assert [part async for part in service.stream([user("在吗")])] == ["你", "好"]


async def test_chat传model覆盖配置() -> None:
    """多模态走的就是这条路：不单独开方法，传 model=svc.vision_model。"""
    service = _service(vision_model="vision-m")
    client = _FakeClient(completion=_completion())
    _attach(service, client)

    _ = await service.chat([user("在吗")], model=service.vision_model)
    assert client.calls[0]["model"] == "vision-m"


async def test_流式错误同样走错误映射() -> None:
    service = _service()
    _attach(service, _FakeClient(error=_status_error(429)))

    with pytest.raises(LLMRequestError):
        _ = [part async for part in service.stream([user("在吗")])]


# ---- 结构化输出 ----


async def test_结构化输出按schema解析() -> None:
    service = _service()
    client = _FakeClient(completion=_completion('{"name": "苹果", "price": 1.5}'))
    _attach(service, client)

    item = await service.chat_structured([user("报价")], Item)
    assert item == Item(name="苹果", price=1.5)
    assert client.calls[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Item", "schema": Item.model_json_schema()},
    }


async def test_结构化输出不合schema时报LLMSchemaError() -> None:
    service = _service()
    _attach(service, _FakeClient(completion=_completion('{"name": "苹果"}')))

    with pytest.raises(LLMSchemaError):
        _ = await service.chat_structured([user("报价")], Item)


async def test_json_object模式只传类型标记() -> None:
    service = _service(structured_mode="json_object")
    client = _FakeClient(completion=_completion('{"name": "苹果", "price": 1.5}'))
    _attach(service, client)

    _ = await service.chat_structured([user("报价")], Item)
    assert client.calls[0]["response_format"] == {"type": "json_object"}


async def test_结构化输出与工具调用也吃配置的max_tokens() -> None:
    """结构化输出最怕输出被截断，max_tokens 不能只对 chat / stream 生效。"""
    service = _service(max_tokens=512)
    client = _FakeClient(completion=_completion('{"name": "苹果", "price": 1.5}'))
    _attach(service, client)

    _ = await service.chat_structured([user("报价")], Item)
    assert client.calls[0]["max_tokens"] == 512

    tools_client = _FakeClient(completion=_completion(None))
    _attach(service, tools_client)
    _ = await service.chat_tools([user("天气")], [tool("get_weather", "查天气", Item)])
    assert tools_client.calls[0]["max_tokens"] == 512


# ---- 工具调用 ----


async def test_工具调用只发一轮() -> None:
    call = ChatCompletionMessageToolCall(
        id="call_1",
        type="function",
        function=Function(name="get_weather", arguments='{"city": "上海"}'),
    )
    service = _service()
    client = _FakeClient(completion=_completion(None, [call]))
    _attach(service, client)

    reply = await service.chat_tools([user("天气")], [tool("get_weather", "查天气", Item)])

    # 不自动发起第二轮：假客户端只被调了一次
    assert len(client.calls) == 1
    assert reply.content is None
    assert reply.tool_calls == (
        ToolCall(id="call_1", name="get_weather", arguments='{"city": "上海"}'),
    )


def test_回传工具调用的消息结构() -> None:
    """第二轮要 assistant(tool_calls=...) + tool_result()，调用方不该为此 import SDK。"""
    call = ToolCall(id="call_1", name="get_weather", arguments='{"city": "上海"}')

    assert assistant(tool_calls=(call,)) == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": "上海"}'},
            }
        ],
    }
    assert tool_result(call.id, "晴 26℃") == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "晴 26℃",
    }


# ---- 错误映射 ----


async def test_状态码错误映射成LLMRequestError() -> None:
    service = _service()
    _attach(service, _FakeClient(error=_status_error()))

    with pytest.raises(LLMRequestError) as info:
        _ = await service.chat([user("在吗")])
    assert info.value.status_code == 500
    assert info.value.request_id == "req-1"


async def test_超时映射成LLMTimeoutError() -> None:
    service = _service()
    _attach(service, _FakeClient(error=APITimeoutError(request=httpx2.Request("POST", _URL))))

    with pytest.raises(LLMTimeoutError):
        _ = await service.chat([user("在吗")])


async def test_连接错误映射成LLMConnectionError() -> None:
    service = _service()
    _attach(
        service,
        _FakeClient(
            error=APIConnectionError(message="连不上", request=httpx2.Request("POST", _URL))
        ),
    )

    with pytest.raises(LLMConnectionError):
        _ = await service.chat([user("在吗")])


# ---- embedding 与收尾 ----


async def test_embed按输入顺序返回() -> None:
    service = _service()
    _attach(service, _FakeClient(embedding=_embedding([0.1], [0.2], [0.3])))

    vectors = await service.embed(["a", "b", "c"])
    assert vectors == [[0.1], [0.2], [0.3]]


async def test_stop幂等() -> None:
    service = _service()
    client = _FakeClient()
    _attach(service, client)

    await service.stop()
    await service.stop()
    assert client.close_count == 1


def test_多模态消息结构() -> None:
    message = user_with_images("描述一下", image_url("http://x/y.png"))

    assert message["content"] == [
        {"type": "text", "text": "描述一下"},
        {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
    ]
