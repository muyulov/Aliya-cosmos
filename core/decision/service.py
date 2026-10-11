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
import random
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
    DecisionRetryError,
    DecisionTimeoutError,
)
from core.decision.questions import Question, choice, noul, score
from core.service.base import HealthStatus, Service, not_running_detail

#: 没启用任何端点时的统一措辞：健康检查的详情用它
NO_ENDPOINT = "未启用任何判断端点，判断功能不可用"

#: 两个端点，顺序固定（jev 云端在前，laya 本地在后）。`_endpoints` 由它展开，
#: `_settings_of` 负责名到配置的映射：加第三个端点时这两处按同一份名字对齐即可。
ENDPOINTS: tuple[Backend, ...] = ("jev", "laya")

#: 需要退避重试的状态码：429 限流、529 过载（Jev 文档里的码），
#: 502 / 503 / 504 是网关与上游的瞬时不可用，同样值得退避后再试一次。
RETRY_STATUS: frozenset[int] = frozenset({429, 502, 503, 504, 529})

#: 退避基数（秒）：delay = _BACKOFF_BASE * 2 ** 已重试次数。测试 patch 成 0 用。
_BACKOFF_BASE: float = 0.5

#: 便捷方法内部用的问题 id
_QID = "answer"

#: state 是任意 JSON：字符串、数字、布尔、null、对象、数组都可以
type State = str | int | float | bool | dict[str, object] | list[object] | None

#: predict_many 的一项入参：一个 state 配一组问题
type PredictCall = tuple[State, Mapping[str, Question]]


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """一次 predict 的结果。

    answers 原样透传协议结构（键与 questions 一一对应）：两家 answer 并不同构，
    这一层不做承诺，调用方按键取。routing 只有 Laya 返回。
    backend 记录本次实际走的端点，方便调用方按后端分别校准阈值（confidence 跨后端不可比）。
    """

    model: str
    answers: dict[str, dict[str, object]]
    input_tokens: int | None
    output_tokens: int | None
    routing: dict[str, object] | None
    backend: Backend

    def answer(self, qid: str) -> dict[str, object]:
        """按键取一条答案；键不在、或 value 不是对象时抛 DecisionResponseError。

        `_parse` 只保证 answers 本身是 dict，没管每个 value 的形态。这里补上：
        value 不是对象（含键压根不在、`get` 返回 None 的情形）一律换
        DecisionResponseError——否则下游 `answer.get(...)` 会对非 dict 调 `.get`，
        把类型错误漏成 AttributeError。便捷方法与调用方共用这一个出口。
        """
        value = self.answers.get(qid)
        if not isinstance(value, dict):
            raise DecisionResponseError(
                f"返回体里 {qid} 的答案不是对象（实到 {sorted(self.answers)}）"
            )
        return value


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


def _number_or_none(value: object) -> float | None:
    """数值归一成 float；非数值返回 None。

    显式排除 bool：JSON 的 true 在 Python 里是 int 的子类，`isinstance(True, int)`
    为真，不拦会被当成 1。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int_or_none(value: object) -> int | None:
    """usage 字段可能缺失，非整数一律当「没给」。

    同样排除 bool：token 为 true 时不该被记成 1。
    """
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _float_map(value: object) -> dict[str, float] | None:
    """校验「键为字符串、值为数值」的映射（probabilities），并把数值归一成 float。

    不合法返回 None 而不抛错：便捷方法据此统一走「字段不完整」那条
    DecisionResponseError，判定与措辞收在一处。
    """
    if not isinstance(value, dict):
        return None
    # 键按协议保证是字符串（JSON 对象的键），这里只在值上做校验
    result: dict[str, float] = {}
    for key, item in cast("dict[str, object]", value).items():
        number = _number_or_none(item)
        if number is None:
            return None
        result[key] = number
    return result


def _str_map(value: object) -> dict[str, str] | None:
    """校验「值是字符串」的映射（score 的 legend）；不合法返回 None。"""
    if not isinstance(value, dict):
        return None
    for item in cast("dict[str, object]", value).values():
        if not isinstance(item, str):
            return None
    return cast("dict[str, str]", value)


def _backoff_delay(attempt: int) -> float:
    """指数退避 + 全抖动：上限 _BACKOFF_BASE * 2**attempt，实际取 [0, 上限] 的随机值。

    抖动用来打散同一时刻打进来的重试（同端点多并发时避免齐步重试造成二次冲击）。
    `_BACKOFF_BASE` 为 0 时返回值恒为 0，测试据此免去伪造 asyncio.sleep。
    """
    ceiling = _BACKOFF_BASE * 2.0**attempt
    return random.uniform(0.0, ceiling)


@asynccontextmanager
async def _wrap_errors(endpoint: str, model: str) -> AsyncGenerator[None, None]:
    """把 httpx2 异常换成判断层错误。

    except 顺序是硬要求：httpx2 的层级是 TimeoutException → TransportError →
    DecodingError → RequestError，一律子类在前，否则会被更宽的那个先接走。
    model 进错误消息：出错时一并给出打在哪个模型上，省得回头翻配置。

    RequestError 这条宽底座是兜底而非装饰：DecodingError 虽是 RequestError 的
    子类却**不是** TransportError（端点或中间代理声明了 Content-Encoding 但响应体
    不是该编码时抛它），没有它就会裸抛出去。宽底座一并保证将来 httpx2 新增的
    RequestError 子类也不会穿透——这一层的对外承诺是「异常都归 DecisionError」。
    """
    try:
        yield
    except httpx2.TimeoutException as exc:
        raise DecisionTimeoutError(f"请求 {endpoint} 超时（模型 {model}）：{exc}") from exc
    except httpx2.TransportError as exc:
        raise DecisionConnectionError(f"连接 {endpoint} 失败（模型 {model}）：{exc}") from exc
    except httpx2.DecodingError as exc:
        raise DecisionResponseError(f"返回体解码失败（模型 {model}）：{exc}") from exc
    except httpx2.RequestError as exc:
        raise DecisionConnectionError(f"请求 {endpoint} 失败（模型 {model}）：{exc}") from exc


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
        result = self._parse(await self._request(name, client, settings, payload), name)
        self._log_done(begin, name, settings, result, len(questions))
        return result

    async def predict_many(
        self,
        calls: Sequence[PredictCall],
        *,
        backend: Backend | None = None,
        limit: int | None = None,
    ) -> list[DecisionResult]:
        """并发跑多组相互独立的提问，返回顺序与入参一致。

        与 predict 走同一端点（数组级的 backend 在这里一次选定，不在每题上重复声明）；
        `limit` 限制同时在飞的请求数，用来在批量场景下压住对端点的并发压力，None 不设上限。
        任何一题失败都会原样抛出（层内不做兜底）。空入参直接返回空表：不发请求、不查端点。
        """
        if not calls:
            return []
        if limit is not None and limit < 1:
            raise ValueError(f"limit 必须为正整数（实到 {limit}）")

        async def run(call: PredictCall) -> DecisionResult:
            state, questions = call
            return await self.predict(state, questions, backend=backend)

        if limit is None:
            return list(await asyncio.gather(*(run(call) for call in calls)))

        semaphore = asyncio.Semaphore(limit)

        async def bounded(call: PredictCall) -> DecisionResult:
            async with semaphore:
                return await run(call)

        return list(await asyncio.gather(*(bounded(call) for call in calls)))

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
        answer = result.answer(_QID)
        picked = answer.get("choice")
        probabilities = _float_map(answer.get("probabilities"))
        confidence = _number_or_none(answer.get("confidence"))
        if not isinstance(picked, str) or probabilities is None or confidence is None:
            raise DecisionResponseError(f"choice 答案字段不完整：{sorted(answer)}")
        return ChoiceAnswer(
            choice=picked,
            probabilities=probabilities,
            confidence=confidence,
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
        answer = result.answer(_QID)
        value = _number_or_none(answer.get("score"))
        legend = _str_map(answer.get("legend"))
        probabilities = _float_map(answer.get("probabilities"))
        confidence = _number_or_none(answer.get("confidence"))
        if value is None or legend is None or probabilities is None or confidence is None:
            raise DecisionResponseError(f"score 答案字段不完整：{sorted(answer)}")
        return ScoreAnswer(
            score=value,
            legend=legend,
            probabilities=probabilities,
            confidence=confidence,
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
        answer = result.answer(_QID)
        value = _number_or_none(answer.get("noul"))
        if value is None:
            raise DecisionResponseError(f"noul 答案字段不合法：{answer.get('noul')!r}")
        return value

    # ---------- 内部 ----------

    def _endpoints(self) -> tuple[tuple[Backend, DecisionEndpointSettings], ...]:
        """两个端点，顺序固定（由 ENDPOINTS 单一决定）。"""
        return tuple((name, self._settings_of(self._config, name)) for name in ENDPOINTS)

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
        """发一次请求，可重试状态码按指数退避 + 抖动重试。"""
        attempt = 0
        while True:
            async with _wrap_errors(settings.base_url, settings.model):
                response = await client.post(SYSTEMONE_PATH, json=payload)
            retryable = response.status_code in RETRY_STATUS
            if not retryable or attempt >= settings.retries:
                # 重试耗尽（仍是可重试状态码）时换子类：调用方可据此区分「限流 / 过载持续」
                # 与普通 4xx / 5xx；非可重试状态码原样走 DecisionRequestError。
                return self._body(response, settings, retry_exhausted=retryable)
            delay = _backoff_delay(attempt)
            # 端点取 settings.base_url，与成功日志同一个取值：同一个字段在两条日志里
            # 值不同（一头是裸名 "laya"、另一头是 URL）会让采集端按端点聚合时分叉。
            # 是哪一头由后端的 后端=name 表达。
            self.log.warning(
                "判断调用重试",
                端点=settings.base_url,
                后端=name,
                状态码=response.status_code,
                第几次=attempt + 1,
                等待毫秒=round(delay * 1000, 1),
            )
            await asyncio.sleep(delay)
            attempt += 1

    @staticmethod
    def _body(
        response: httpx2.Response,
        settings: DecisionEndpointSettings,
        *,
        retry_exhausted: bool = False,
    ) -> dict[str, object]:
        """状态码与 JSON 解析的收口。

        错误码换 DecisionRequestError（重试耗尽的可重试码换子类 DecisionRetryError），
        坏返回体换 DecisionResponseError。这里在 `_wrap_errors` 之外，异常必须自己收干净，
        不能指望外层兜。

        UnicodeDecodeError 与 json.JSONDecodeError 并列而非多此一举：`response.json()`
        先按 charset 解字节再解析，响应体不是合法 UTF-8 时抛的是前者（它是
        ValueError 的子类，但**不是** json.JSONDecodeError），只收后者会裸抛出去。
        两者都归 DecisionResponseError：问题都在返回体本身，端点这一趟是通的。
        """
        if response.status_code >= 400:
            error_type = DecisionRetryError if retry_exhausted else DecisionRequestError
            message = f"判断端点返回 {response.status_code}"
            if retry_exhausted:
                message += "（退避重试已耗尽）"
            raise error_type(
                message,
                status_code=response.status_code,
                endpoint=settings.base_url,
                model=settings.model,
                request_id=response.headers.get("x-request-id"),
            )
        try:
            body = cast("object", response.json())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DecisionResponseError(f"返回体不是合法 JSON：{exc}") from exc
        if not isinstance(body, dict):
            raise DecisionResponseError("返回体顶层不是对象")
        return cast("dict[str, object]", body)

    @staticmethod
    def _parse(body: dict[str, object], backend: Backend) -> DecisionResult:
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
            backend=backend,
        )

    def _log_done(
        self,
        begin: float,
        name: Backend,
        settings: DecisionEndpointSettings,
        result: DecisionResult,
        question_count: int,
    ) -> None:
        """调用成功日志：只记模型 / 端点 / 后端 / 问题数 / 耗时 / token，不记正文。

        后端与重试日志同源（裸名），端点统一取 settings.base_url（URL）：
        同一个字段在两条日志里取值一致，采集端按端点 / 后端聚合才不会分叉。
        """
        fields: dict[str, object] = {
            "模型": result.model,
            "端点": settings.base_url,
            "后端": name,
            "问题数": question_count,
            "耗时毫秒": _elapsed_ms(begin),
        }
        if result.input_tokens is not None:
            fields["输入token"] = result.input_tokens
        if result.output_tokens is not None:
            fields["输出token"] = result.output_tokens
        # 显式传 face：fields 的值是 object，**fields 展开时会被逐个形参对账
        self.log.info("判断调用完成", face=None, **fields)
