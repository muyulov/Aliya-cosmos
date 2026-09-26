"""LLM 服务：把「调大模型」收成一个服务，业务代码不直接接触 SDK。

约定：
- 五项能力：chat / stream / chat_structured / chat_tools / 多模态。
  多模态不单独开方法，由调用方给 chat 传 `model=svc.vision_model`。
- chat 与 vision 是两个端点，各持一个客户端；服务按传进来的模型名挑端点。
- 工具调用只做单轮：返回 tool_calls，回传用 `assistant(tool_calls=...)` +
  `tool_result(...)`，循环由调用方写。
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

from core.config import LLMEndpointSettings, LLMSettings
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
from core.llm.messages import ToolCall
from core.service.base import HealthStatus, Service, ServiceState

#: 结构化输出的 schema 类型
T = TypeVar("T", bound=BaseModel)

#: 没配密钥时的统一措辞：start 的警告、health 的详情、调用时的报错共用
NO_API_KEY = "未配置 api_key，LLM 功能不可用"


@dataclass(frozen=True, slots=True)
class AssistantReply:
    """工具调用的单轮回复：content 与 tool_calls 可能各有其一。"""

    content: str | None
    tool_calls: tuple[ToolCall, ...]


def _or_omit[V](value: V | None) -> V | Omit:
    """None 换成 omit（不传该参数）。

    SDK 3.x 的请求体只剔除 NotGiven 与 Omit，显式 None 会被原样序列化成
    JSON null（端点多半 400 或当 0 处理），所以「不传」必须走 omit。
    """
    return omit if value is None else value


def _pick[V](override: V | None, fallback: V | None) -> V | Omit:
    """调用方的覆盖优先，其次端点配置，都没有则不传。"""
    return _or_omit(override if override is not None else fallback)


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
        self._chat: AsyncOpenAI | None = None
        self._vision: AsyncOpenAI | None = None

    # ---------- 模型槽位 ----------

    @property
    def chat_model(self) -> str:
        """对话模型。"""
        return self._config.chat.model

    @property
    def vision_model(self) -> str:
        """多模态模型：给 chat 传它就走 vision 端点。"""
        return self._config.vision.model

    # ---------- 生命周期 ----------

    @override
    async def start(self) -> None:
        """两个端点各建一个客户端。没配 api_key 的端点只警告：脚手架不该因为没密钥就起不来。"""
        if self.state is ServiceState.RUNNING:
            return
        self._chat = self._build("chat", self._config.chat, self._chat)
        self._vision = self._build("vision", self._config.vision, self._vision)

    @override
    async def stop(self) -> None:
        """关两个客户端。必须幂等：回滚时它也会作用在没建过客户端的实例上。"""
        chat, self._chat = self._chat, None
        vision, self._vision = self._vision, None
        if chat is not None:
            await chat.close()
        if vision is not None:
            await vision.close()

    @override
    async def health(self) -> HealthStatus:
        """只看有没有客户端：健康检查不该发请求（有副作用、花钱、受网络抖动影响）。

        两个端点全没配才算不健康：只启用一头也是能用的。
        """
        healthy = self._chat is not None or self._vision is not None
        detail = "" if healthy else NO_API_KEY
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
        client, endpoint, resolved = self._endpoint(model)
        begin = time.perf_counter()
        async with _wrap_errors(endpoint.base_url, resolved):
            response = await client.chat.completions.create(
                model=resolved,
                messages=list(messages),
                temperature=_pick(temperature, endpoint.temperature),
                max_tokens=_pick(max_tokens, endpoint.max_tokens),
            )
        usage = response.usage
        self._log_done(
            begin,
            endpoint,
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
        client, endpoint, resolved = self._endpoint(model)
        begin = time.perf_counter()
        async with _wrap_errors(endpoint.base_url, resolved):
            chunks = await client.chat.completions.create(
                model=resolved,
                messages=list(messages),
                temperature=_pick(temperature, endpoint.temperature),
                max_tokens=_pick(max_tokens, endpoint.max_tokens),
                stream=True,
            )
            async for chunk in chunks:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yield delta
        self._log_done(begin, endpoint, resolved, None)

    async def chat_structured(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> T:
        """结构化输出：按 schema 约束返回，返回体校验失败抛 LLMSchemaError。

        不传 strict：strict 要求所有字段 required，默认关掉更不容易踩坑。
        """
        client, endpoint, resolved = self._endpoint(model)
        begin = time.perf_counter()
        async with _wrap_errors(endpoint.base_url, resolved):
            response = await client.chat.completions.create(
                model=resolved,
                messages=list(messages),
                temperature=_pick(temperature, endpoint.temperature),
                max_tokens=_pick(max_tokens, endpoint.max_tokens),
                response_format=self._response_format(endpoint, schema),
            )
        usage = response.usage
        self._log_done(
            begin,
            endpoint,
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
        max_tokens: int | None = None,
    ) -> AssistantReply:
        """工具调用（单轮）：返回正文与 tool_calls，不自动发起第二轮。"""
        client, endpoint, resolved = self._endpoint(model)
        begin = time.perf_counter()
        async with _wrap_errors(endpoint.base_url, resolved):
            response = await client.chat.completions.create(
                model=resolved,
                messages=list(messages),
                tools=list(tools),
                temperature=_pick(temperature, endpoint.temperature),
                max_tokens=_pick(max_tokens, endpoint.max_tokens),
            )
        usage = response.usage
        self._log_done(
            begin,
            endpoint,
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

    # ---------- 内部 ----------

    def _build(
        self,
        name: str,
        endpoint: LLMEndpointSettings,
        current: AsyncOpenAI | None,
    ) -> AsyncOpenAI | None:
        """建一个端点的客户端：已建过就原样返回（幂等），没配 api_key 则警告并返回 None。"""
        if current is not None:
            return current
        if not endpoint.api_key:
            self.log.warning(NO_API_KEY, 端点=name, 地址=endpoint.base_url)
            return None
        return build_client(endpoint)

    def _endpoint(self, model: str | None) -> tuple[AsyncOpenAI, LLMEndpointSettings, str]:
        """挑端点：默认 chat；传 vision 的模型且 vision 已启用时走 vision。

        其余情况按 chat 端点覆盖模型名。

        vision 没启用时不接管自己的模型：两头 model 常常同名，
        免得 chat 调用因为 vision 没配密钥而报错。
        """
        vision = self._config.vision
        if model is not None and model == vision.model and self._vision is not None:
            return self._vision, vision, model
        chat = self._config.chat
        return self._require(self._chat, "chat"), chat, model or chat.model

    @staticmethod
    def _require(client: AsyncOpenAI | None, name: str) -> AsyncOpenAI:
        """取客户端；没配 api_key 时到这里才报错（配置缺失不该让进程起不来）。"""
        if client is None:
            raise LLMConfigError(f"{name} 端点{NO_API_KEY}")
        return client

    def _response_format(
        self, endpoint: LLMEndpointSettings, schema: type[BaseModel]
    ) -> ResponseFormat:
        """按端点的 structured_mode 构造 response_format。

        model_json_schema() 返回 dict[str, Any]，cast 成 object 值再放进请求体，
        否则 Any 会顺着 SDK 参数渗进类型检查。
        """
        if endpoint.structured_mode == "json_object":
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
        endpoint: LLMEndpointSettings,
        model: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        """调用成功日志：只记模型 / 端点 / 耗时 / token 用量，不记正文。

        兼容端点未必返回 usage，取不到就省略 token 字段，不因为没得记而报错。
        """
        fields: dict[str, object] = {
            "模型": model,
            "端点": endpoint.base_url,
            "耗时毫秒": _elapsed_ms(begin),
        }
        if prompt_tokens is not None:
            fields["输入token"] = prompt_tokens
        if completion_tokens is not None:
            fields["输出token"] = completion_tokens
        # 显式传 face：fields 的值是 object，**fields 展开时会被逐个形参对账
        self.log.info("LLM 调用完成", face=None, **fields)
