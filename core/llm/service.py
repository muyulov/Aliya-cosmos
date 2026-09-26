"""LLM 服务：把「调大模型」收成一个服务，业务代码不直接接触 SDK。

约定：
- 六项能力：chat / stream / chat_structured / chat_tools / embed / 多模态。
  多模态不单独开方法，由调用方给 chat 传 `model=svc.vision_model`。
- 工具调用只做单轮：返回 tool_calls，回传与循环由调用方写。
- 日志不记消息正文：正文该不该记由调用方自己决定并自己打。
- 重试与超时全交给 SDK，这里不包一层。
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import ClassVar, TypeVar, cast, override

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    Omit,
    omit,
)
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionMessageToolCall,
    ChatCompletionToolParam,
)
from openai.types.chat.completion_create_params import ResponseFormat
from pydantic import BaseModel, ValidationError

from core.config import LLMSettings
from core.llm.client import build_client
from core.llm.errors import (
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMRequestError,
    LLMResponseError,
    LLMSchemaError,
    LLMTimeoutError,
)
from core.service.base import HealthStatus, Service, ServiceState

#: 结构化输出的 schema 类型
T = TypeVar("T", bound=BaseModel)

#: 可选参数在「不传」时用的哨兵。SDK 3.x 的请求体只剔除 NotGiven 与 Omit，
#: 显式 None 会被原样序列化成 JSON null（端点多半 400 或当 0 处理）。


@dataclass(frozen=True, slots=True)
class ToolCall:
    """单次工具调用。

    arguments 是原始 JSON 字符串，不做解析：schema 只有调用方知道。
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class AssistantReply:
    """工具调用的单轮回复：content 与 tool_calls 可能各有其一。"""

    content: str | None
    tool_calls: tuple[ToolCall, ...]


def _or_omit[V](value: V | None) -> V | Omit:
    """None 换成 omit（不传该参数）。"""
    return omit if value is None else value


def _elapsed_ms(begin: float) -> float:
    """从 begin（`time.perf_counter()` 的取值）到现在经过的毫秒数。"""
    return round((time.perf_counter() - begin) * 1000, 1)


@asynccontextmanager
async def _wrap_errors(endpoint: str, model: str) -> AsyncGenerator[None, None]:
    """把 SDK 异常换成 LLM 层错误。

    except 顺序是硬要求：APITimeoutError 是 APIConnectionError 的子类，
    先捕子类才不会把超时一律误判成连接错误。
    """
    try:
        yield
    except APITimeoutError as exc:
        raise LLMTimeoutError(f"请求模型 {model} 超时：{exc}") from exc
    except APIStatusError as exc:
        raise LLMRequestError(
            f"模型 {model} 在 {endpoint} 返回 {exc.status_code}",
            status_code=exc.status_code,
            endpoint=endpoint,
            model=model,
            request_id=exc.request_id,
        ) from exc
    except APIConnectionError as exc:
        raise LLMConnectionError(f"连接 {endpoint} 失败：{exc}") from exc
    except APIError as exc:
        raise LLMError(f"调用模型 {model} 失败：{exc}") from exc


class LLMService(Service):
    """LLM 服务：持有客户端，暴露六项调用能力。"""

    name: ClassVar[str] = "llm"

    def __init__(self, config: LLMSettings) -> None:
        super().__init__()
        self._config: LLMSettings = config
        self._client: AsyncOpenAI | None = None

    # ---------- 模型槽位 ----------

    @property
    def chat_model(self) -> str:
        """对话模型。"""
        return self._config.chat_model

    @property
    def embed_model(self) -> str:
        """embedding 模型：留空即未启用。"""
        return self._config.embed_model

    @property
    def vision_model(self) -> str:
        """多模态模型：留空时复用 chat_model。"""
        return self._config.vision_model or self._config.chat_model

    # ---------- 生命周期 ----------

    @override
    async def start(self) -> None:
        """建客户端。没配 api_key 时只警告：脚手架不该因为没密钥就起不来。"""
        if self.state is ServiceState.RUNNING:
            return
        if not self._config.api_key:
            self.log.warning("未配置 api_key，LLM 功能不可用", 端点=self._config.base_url)
            return
        self._client = build_client(self._config)

    @override
    async def stop(self) -> None:
        """关客户端。必须幂等：回滚时它也会作用在没建过客户端的实例上。"""
        if self._client is None:
            return
        client, self._client = self._client, None
        await client.close()

    @override
    async def health(self) -> HealthStatus:
        """只看有没有客户端：健康检查不该发请求（有副作用、花钱、受网络抖动影响）。"""
        healthy = self._client is not None
        detail = "" if healthy else "未配置 api_key，LLM 功能不可用"
        return HealthStatus(name=self.label, healthy=healthy, state=self.state, detail=detail)

    # ---------- 调用能力 ----------

    async def chat(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """对话补全，返回消息正文。"""
        client = self._require_client()
        resolved = self._resolve_model(model)
        begin = time.perf_counter()
        async with _wrap_errors(self._config.base_url, resolved):
            response = await client.chat.completions.create(
                model=resolved,
                messages=list(messages),
                temperature=self._temperature(temperature),
                max_tokens=self._max_tokens(max_tokens),
            )
        usage = response.usage
        self._log_done(
            begin,
            resolved,
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
        )
        return self._content(response)

    async def stream(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式输出：逐段 yield 非空片段，不自动拼接。

        SDK 明确「流已消费则不重试」，中途断流会直接抛错，是否重放由调用方决定。
        """
        client = self._require_client()
        resolved = self._resolve_model(model)
        begin = time.perf_counter()
        async with _wrap_errors(self._config.base_url, resolved):
            chunks = await client.chat.completions.create(
                model=resolved,
                messages=list(messages),
                temperature=self._temperature(temperature),
                max_tokens=self._max_tokens(max_tokens),
                stream=True,
            )
            async for chunk in chunks:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yield delta
        self._log_done(begin, resolved, None)

    async def chat_structured(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> T:
        """结构化输出：按 schema 约束返回，返回体校验失败抛 LLMSchemaError。

        不传 strict：strict 要求所有字段 required，默认关掉更不容易踩坑。
        """
        client = self._require_client()
        resolved = self._resolve_model(model)
        begin = time.perf_counter()
        async with _wrap_errors(self._config.base_url, resolved):
            response = await client.chat.completions.create(
                model=resolved,
                messages=list(messages),
                temperature=self._temperature(temperature),
                response_format=self._response_format(schema),
            )
        usage = response.usage
        self._log_done(
            begin,
            resolved,
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
        )
        content = self._content(response)
        try:
            return schema.model_validate_json(content)
        except ValidationError as exc:
            raise LLMSchemaError(f"结构化输出不符合 {schema.__name__}：{exc}") from exc

    async def chat_tools(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        tools: Sequence[ChatCompletionToolParam],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> AssistantReply:
        """工具调用（单轮）：返回正文与 tool_calls，不自动发起第二轮。"""
        client = self._require_client()
        resolved = self._resolve_model(model)
        begin = time.perf_counter()
        async with _wrap_errors(self._config.base_url, resolved):
            response = await client.chat.completions.create(
                model=resolved,
                messages=list(messages),
                tools=list(tools),
                temperature=self._temperature(temperature),
            )
        usage = response.usage
        self._log_done(
            begin,
            resolved,
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
        )
        if not response.choices:
            raise LLMResponseError("LLM 返回体没有 choices")
        message = response.choices[0].message
        # tool_calls 是 function 与 custom 两种调用的联合，这里只认 function：
        # ToolCall 的 arguments 取自 function.arguments，custom 没有这个结构。
        calls = [
            ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
            for call in message.tool_calls or ()
            if isinstance(call, ChatCompletionMessageToolCall)
        ]
        return AssistantReply(content=message.content, tool_calls=tuple(calls))

    async def embed(self, texts: Sequence[str], *, model: str | None = None) -> list[list[float]]:
        """文本向量化：按输入顺序返回，不内置分批。"""
        client = self._require_client()
        resolved = model or self._config.embed_model
        if not resolved:
            raise LLMConfigError("未配置 embed_model，embedding 不可用")
        begin = time.perf_counter()
        async with _wrap_errors(self._config.base_url, resolved):
            response = await client.embeddings.create(model=resolved, input=list(texts))
        self._log_done(begin, resolved, response.usage.prompt_tokens)
        return [item.embedding for item in response.data]

    # ---------- 内部 ----------

    def _require_client(self) -> AsyncOpenAI:
        """取客户端；没配 api_key 时到这里才报错（配置缺失不该让进程起不来）。"""
        if self._client is None:
            raise LLMConfigError("未配置 api_key，LLM 功能不可用")
        return self._client

    def _resolve_model(self, override: str | None) -> str:
        """模型覆盖优先，否则用 chat_model。"""
        return override or self._config.chat_model

    def _temperature(self, override: float | None) -> float | Omit:
        """temperature 覆盖优先，其次配置，都没有则不传。"""
        return _or_omit(override if override is not None else self._config.temperature)

    def _max_tokens(self, override: int | None) -> int | Omit:
        """max_tokens 覆盖优先，其次配置，都没有则不传。"""
        return _or_omit(override if override is not None else self._config.max_tokens)

    def _response_format(self, schema: type[BaseModel]) -> ResponseFormat:
        """按 structured_mode 构造 response_format。

        model_json_schema() 返回 dict[str, Any]，cast 成 object 值再放进请求体，
        否则 Any 会顺着 SDK 参数渗进类型检查。
        """
        if self._config.structured_mode == "json_object":
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": cast("dict[str, object]", schema.model_json_schema()),
            },
        }

    @staticmethod
    def _content(response: ChatCompletion) -> str:
        """取首条消息正文；为空说明这次回复没有正文（可能被工具调用占用）。"""
        if not response.choices:
            raise LLMResponseError("LLM 返回体没有 choices")
        content = response.choices[0].message.content
        if content is None:
            raise LLMResponseError("LLM 返回的消息内容为 None（可能被工具调用占用）")
        return content

    def _log_done(
        self,
        begin: float,
        model: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        """调用成功日志：只记模型 / 端点 / 耗时 / token 用量，不记正文。

        兼容端点未必返回 usage，取不到就省略 token 字段，不因为没得记而报错。
        """
        fields: dict[str, object] = {
            "模型": model,
            "端点": self._config.base_url,
            "耗时毫秒": _elapsed_ms(begin),
        }
        if prompt_tokens is not None:
            fields["输入token"] = prompt_tokens
        if completion_tokens is not None:
            fields["输出token"] = completion_tokens
        # 显式传 face：fields 的值是 object，**fields 展开时会被逐个形参对账
        self.log.info("LLM 调用完成", None, **fields)
