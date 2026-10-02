"""判断服务：把「让模型做一次判断」收成一个服务，业务代码不直接接触 HTTP。

约定：
- 两个端点（jev 云端 / laya 本地）走同一套 /systemone 协议，字段完全相同。
- 调用方显式传 backend 选端点，默认 laya。层内不做兜底——两个后端的 confidence
  语义不同（Jev 是 (n·p_max-1)/(n-1)，Laya 是 1-归一化熵），跨端点比较必然出错。
- predict 的 answers 原样透传：两家的 answer 并不同构（Laya 多 answer_confidence /
  abstention），建模会把这些字段静默吃掉。
- 日志不记 state 与 questions 正文。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import ClassVar, cast, override

import httpx2

from core.config import Backend, DecisionEndpointSettings, DecisionSettings
from core.decision.client import SYSTEMONE_PATH, build_client
from core.decision.errors import (
    DecisionConfigError,
    DecisionConnectionError,
    DecisionRequestError,
    DecisionResponseError,
    DecisionTimeoutError,
)
from core.decision.questions import Question, choice, noul, score
from core.service.base import HealthStatus, Service, not_running_detail

#: 没启用任何端点时的统一措辞：健康检查的详情用它
NO_ENDPOINT = "未启用任何判断端点，判断功能不可用"

#: 需要退避重试的状态码：429 限流、529 过载（Jev 文档里的码）
RETRY_STATUS: frozenset[int] = frozenset({429, 529})

#: 退避基数（秒）：delay = _BACKOFF_BASE * 2 ** 已重试次数。测试 patch 成 0 用。
_BACKOFF_BASE: float = 0.5

#: 便捷方法内部用的问题 id
_QID = "answer"

#: state 就是任意 JSON：纯文本、业务记录、聊天记录、数组都可以
type State = str | dict[str, object] | list[object]


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """一次 predict 的结果。

    answers 原样透传协议结构（键与 questions 一一对应）：两家 answer 并不同构，
    这一层不做承诺，调用方按键取。routing 只有 Laya 返回。
    """

    model: str
    answers: dict[str, dict[str, object]]
    input_tokens: int | None
    output_tokens: int | None
    routing: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    """choice 原语的答案：选中的选项、完整概率分布与置信度。"""

    choice: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    """score 原语的答案：概率加权得分、档位说明、分布与置信度。"""

    score: float
    legend: dict[str, str]
    probabilities: dict[str, float]
    confidence: float


def _elapsed_ms(begin: float) -> float:
    """从 begin（`time.perf_counter()` 的取值）到现在经过的毫秒数。"""
    return round((time.perf_counter() - begin) * 1000, 1)


def _int_or_none(value: object) -> int | None:
    """usage 字段可能缺失，非整数一律当「没给」。"""
    return value if isinstance(value, int) else None


@asynccontextmanager
async def _wrap_errors(endpoint: str, model: str) -> AsyncGenerator[None, None]:
    """把 httpx2 异常换成判断层错误。

    except 顺序是硬要求：httpx2 的 TimeoutException 是 TransportError 的子类，
    先捕子类才不会把超时一律误判成连接错误。
    model 进错误消息：出错时一并给出打在哪个模型上，省得回头翻配置。
    """
    try:
        yield
    except httpx2.TimeoutException as exc:
        raise DecisionTimeoutError(f"请求 {endpoint} 超时（模型 {model}）：{exc}") from exc
    except httpx2.TransportError as exc:
        raise DecisionConnectionError(f"连接 {endpoint} 失败（模型 {model}）：{exc}") from exc


class DecisionService(Service):
    """判断服务：持有两个端点的客户端，暴露批量提问与三个便捷方法。"""

    name: ClassVar[str] = "decision"

    def __init__(self, config: DecisionSettings) -> None:
        super().__init__()
        self._config: DecisionSettings = config
        self._clients: dict[Backend, httpx2.AsyncClient] = {}

    # ---------- 生命周期 ----------

    @override
    async def start(self) -> None:
        """只为启用的端点建客户端。未启用是用户的显式选择，不警告。"""
        if self.running:
            return
        self._clients = {
            name: build_client(settings) for name, settings in self._endpoints() if settings.enabled
        }

    @override
    async def stop(self) -> None:
        """关掉两个客户端。必须幂等：回滚时它也会作用在没建过客户端的实例上。"""
        clients, self._clients = self._clients, {}
        for client in clients.values():
            await client.aclose()

    @override
    async def health(self) -> HealthStatus:
        """只看有没有客户端，不发请求。

        两个端点都没启用才算不健康：只启用一头也是能用的。extra 里把两端的
        模型与地址都带出来——「未启用」时更要靠它看出是哪一头没开。
        """
        extra: dict[str, object] = {}
        for name, settings in self._endpoints():
            extra[f"{name}模型"] = settings.model
            extra[f"{name}端点"] = settings.base_url
        if self._clients:
            return HealthStatus(
                name=self.label, healthy=True, state=self.state, detail="", extra=extra
            )
        # 没客户端分两种：跑着但一个端点都没启用（问题在配置），以及还没启动 / 已停止
        # （问题在状态）。后者套用 NO_ENDPOINT 会误导。
        detail = NO_ENDPOINT if self.running else not_running_detail(self.state)
        return HealthStatus(
            name=self.label, healthy=False, state=self.state, detail=detail, extra=extra
        )

    # ---------- 调用能力 ----------

    async def predict(
        self, state: State, questions: Mapping[str, Question], *, backend: Backend | None = None
    ) -> DecisionResult:
        """一次前向把 questions 里所有问题问完，返回带概率的答案。"""
        name, settings, client = self._endpoint(backend)
        payload: dict[str, object] = {"state": state, "questions": dict(questions)}
        if settings.model:
            payload["model"] = settings.model
        begin = time.perf_counter()
        result = self._parse(await self._request(name, client, settings, payload))
        self._log_done(begin, settings, result, len(questions))
        return result

    async def choose(
        self,
        state: State,
        instructions: str,
        criteria: Mapping[str, str | None],
        *,
        backend: Backend | None = None,
    ) -> ChoiceAnswer:
        """从离散选项里挑一个。"""
        result = await self.predict(state, {_QID: choice(instructions, criteria)}, backend=backend)
        answer = self._answer(result)
        picked = answer.get("choice")
        probabilities = answer.get("probabilities")
        confidence = answer.get("confidence")
        if (
            not isinstance(picked, str)
            or not isinstance(probabilities, dict)
            or not isinstance(confidence, (int, float))
        ):
            raise DecisionResponseError(f"choice 答案字段不完整：{sorted(answer)}")
        return ChoiceAnswer(
            choice=picked,
            probabilities=cast("dict[str, float]", probabilities),
            confidence=float(confidence),
        )

    async def rate(
        self,
        state: State,
        instructions: str,
        criteria: Sequence[str],
        *,
        backend: Backend | None = None,
    ) -> ScoreAnswer:
        """在有序档位上打分。"""
        result = await self.predict(state, {_QID: score(instructions, criteria)}, backend=backend)
        answer = self._answer(result)
        value = answer.get("score")
        legend = answer.get("legend")
        probabilities = answer.get("probabilities")
        confidence = answer.get("confidence")
        if (
            not isinstance(value, (int, float))
            or not isinstance(legend, dict)
            or not isinstance(probabilities, dict)
            or not isinstance(confidence, (int, float))
        ):
            raise DecisionResponseError(f"score 答案字段不完整：{sorted(answer)}")
        return ScoreAnswer(
            score=float(value),
            legend=cast("dict[str, str]", legend),
            probabilities=cast("dict[str, float]", probabilities),
            confidence=float(confidence),
        )

    async def ask(
        self,
        state: State,
        instructions: str,
        *,
        true: str | None = None,
        false: str | None = None,
        backend: Backend | None = None,
    ) -> float:
        """是非题，返回 P(true)。

        返回裸浮点而不是 dataclass：Jev 的 noul 答案只有 {type, noul}，连
        confidence 都没有，包一层没东西可装。
        """
        result = await self.predict(
            state, {_QID: noul(instructions, true=true, false=false)}, backend=backend
        )
        answer = self._answer(result)
        value = answer.get("noul")
        if not isinstance(value, (int, float)):
            raise DecisionResponseError(f"noul 答案字段不合法：{value!r}")
        return float(value)

    # ---------- 内部 ----------

    def _endpoints(self) -> tuple[tuple[Backend, DecisionEndpointSettings], ...]:
        """两个端点，顺序固定。"""
        return (("jev", self._config.jev), ("laya", self._config.laya))

    @staticmethod
    def _settings_of(config: DecisionSettings, name: Backend) -> DecisionEndpointSettings:
        """按端点名取配置：显式分支，不用 getattr（会退化成 Any）。"""
        return config.jev if name == "jev" else config.laya

    def _endpoint(
        self, backend: Backend | None
    ) -> tuple[Backend, DecisionEndpointSettings, httpx2.AsyncClient]:
        """挑端点：默认 laya（本地优先：免费、数据不出域）。"""
        name: Backend = "laya" if backend is None else backend
        client = self._clients.get(name)
        if client is None:
            raise DecisionConfigError(f"未启用 {name} 端点")
        return name, self._settings_of(self._config, name), client

    async def _request(
        self,
        name: Backend,
        client: httpx2.AsyncClient,
        settings: DecisionEndpointSettings,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """发一次请求，429/529 按指数退避重试。"""
        attempt = 0
        while True:
            async with _wrap_errors(settings.base_url, settings.model):
                response = await client.post(SYSTEMONE_PATH, json=payload)
            if response.status_code not in RETRY_STATUS or attempt >= settings.retries:
                return self._body(response, settings)
            delay: float = _BACKOFF_BASE * 2.0**attempt
            self.log.warning(
                "判断调用重试",
                端点=name,
                状态码=response.status_code,
                第几次=attempt + 1,
                等待毫秒=round(delay * 1000, 1),
            )
            await asyncio.sleep(delay)
            attempt += 1

    @staticmethod
    def _body(response: httpx2.Response, settings: DecisionEndpointSettings) -> dict[str, object]:
        """状态码与 JSON 解析的收口。

        错误码换 DecisionRequestError，坏返回体换 DecisionResponseError。
        """
        if response.status_code >= 400:
            raise DecisionRequestError(
                f"判断端点返回 {response.status_code}",
                status_code=response.status_code,
                endpoint=settings.base_url,
                model=settings.model,
                request_id=response.headers.get("x-request-id"),
            )
        try:
            body = cast("object", response.json())
        except json.JSONDecodeError as exc:
            raise DecisionResponseError(f"返回体不是合法 JSON：{exc}") from exc
        if not isinstance(body, dict):
            raise DecisionResponseError("返回体顶层不是对象")
        return cast("dict[str, object]", body)

    @staticmethod
    def _parse(body: dict[str, object]) -> DecisionResult:
        """把返回体翻译成 DecisionResult。answers 内部不做任何加工。"""
        answers = body.get("answers")
        if not isinstance(answers, dict):
            raise DecisionResponseError("返回体缺少 answers")
        usage = body.get("usage")
        tokens = cast("dict[str, object]", usage) if isinstance(usage, dict) else {}
        routing = body.get("routing")
        return DecisionResult(
            model=str(body.get("model", "")),
            answers=cast("dict[str, dict[str, object]]", answers),
            input_tokens=_int_or_none(tokens.get("input_tokens")),
            output_tokens=_int_or_none(tokens.get("output_tokens")),
            routing=cast("dict[str, object]", routing) if isinstance(routing, dict) else None,
        )

    @staticmethod
    def _answer(result: DecisionResult) -> dict[str, object]:
        """取便捷方法那一题的答案。"""
        answer = result.answers.get(_QID)
        if answer is None:
            raise DecisionResponseError(
                f"返回体里没有 {_QID} 的答案（实到 {sorted(result.answers)}）"
            )
        return answer

    def _log_done(
        self,
        begin: float,
        settings: DecisionEndpointSettings,
        result: DecisionResult,
        question_count: int,
    ) -> None:
        """调用成功日志：只记模型 / 端点 / 问题数 / 耗时 / token，不记正文。"""
        fields: dict[str, object] = {
            "模型": result.model,
            "端点": settings.base_url,
            "问题数": question_count,
            "耗时毫秒": _elapsed_ms(begin),
        }
        if result.input_tokens is not None:
            fields["输入token"] = result.input_tokens
        if result.output_tokens is not None:
            fields["输出token"] = result.output_tokens
        # 显式传 face：fields 的值是 object，**fields 展开时会被逐个形参对账
        self.log.info("判断调用完成", face=None, **fields)
