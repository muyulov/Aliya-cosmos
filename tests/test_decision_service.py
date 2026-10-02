"""判断服务测试。

约定：
- 客户端在 start() 里建，容器又不认它，所以除「未启用」那条外一律白盒塞 service._clients。
- 用 httpx2.MockTransport 注入 handler：不起 mock server、不打桩 socket，
  handler 直接拿到 httpx2.Request，可断言请求体与请求头（本层最值得断言的东西）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx2
import pytest

import core.decision.service as decision_service
from core.config import DecisionEndpointSettings, DecisionSettings, LogSettings
from core.decision import (
    DecisionConfigError,
    DecisionConnectionError,
    DecisionRequestError,
    DecisionResponseError,
    DecisionResult,
    DecisionService,
    DecisionTimeoutError,
    choice,
    noul,
    score,
)
from core.decision.service import _QID  # pyright: ignore[reportPrivateUsage]
from core.logger import setup_logging


def _service(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> DecisionService:
    """构造一个已「启动」的服务：客户端直接白盒塞进去。"""
    settings = DecisionSettings()
    service = DecisionService(settings)
    service._clients = {  # pyright: ignore[reportPrivateUsage]
        "laya": httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler), base_url=settings.laya.base_url
        )
    }
    return service


async def test_predict_请求体与返回体() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "model": "laya-multilingual",
                "answers": {"退款": {"type": "noul", "noul": 0.93}},
                "routing": {"model": "multilingual"},
                "usage": {"input_tokens": 11, "output_tokens": 0},
            },
        )

    service = _service(handler)

    result = await service.predict("我要退款", {"退款": noul("是否要求退款？")})

    assert isinstance(result, DecisionResult)
    assert result.model == "laya-multilingual"
    assert result.answers["退款"] == {"type": "noul", "noul": 0.93}
    assert (result.input_tokens, result.output_tokens) == (11, 0)
    assert result.routing == {"model": "multilingual"}
    assert seen[0].url.path == "/v1/systemone"
    assert seen[0].method == "POST"
    body = cast("dict[str, object]", json.loads(seen[0].content))
    assert body["state"] == "我要退款"
    assert body["questions"] == {"退款": {"type": "noul", "instructions": "是否要求退款？"}}


async def test_laya默认端点且model为空时不带该字段() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    service = _service(handler)

    _ = await service.predict("state", {})

    assert "model" not in json.loads(seen[0].content)


async def test_jev端点带上配置的model() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"model": "jev-1.13.0", "answers": {}, "usage": {}})

    service = _service(handler)
    service._clients["jev"] = httpx2.AsyncClient(  # pyright: ignore[reportPrivateUsage]
        transport=httpx2.MockTransport(handler), base_url="https://api.typesafe.ai/v1"
    )

    _ = await service.predict("state", {}, backend="jev")

    assert json.loads(seen[0].content)["model"] == "jev-latest"


async def test_usage缺失时不报错() -> None:
    service = _service(lambda request: httpx2.Response(200, json={"model": "m", "answers": {}}))

    result = await service.predict("state", {})

    assert (result.input_tokens, result.output_tokens) == (None, None)


async def test_未启用端点调用报配置错误() -> None:
    service = DecisionService(DecisionSettings())

    with pytest.raises(DecisionConfigError) as excinfo:
        _ = await service.predict("state", {})

    assert excinfo.value.__cause__ is None


async def test_答案不是对象时报返回体错误() -> None:
    """回归：answers 只校验到「是不是 dict」，value 形态漏到下游会成 AttributeError。"""
    service = _service(
        lambda request: httpx2.Response(200, json={"model": "m", "answers": {_QID: 5}})
    )

    with pytest.raises(DecisionResponseError) as excinfo:
        _ = await service.ask("state", "是否？")

    assert "answer" in str(excinfo.value)


async def test_状态码错误带上对账坐标() -> None:
    service = _service(
        lambda request: httpx2.Response(
            422, json={"error": "bad"}, headers={"x-request-id": "req-7"}
        )
    )

    with pytest.raises(DecisionRequestError) as excinfo:
        _ = await service.predict("state", {})

    assert excinfo.value.status_code == 422
    assert excinfo.value.request_id == "req-7"
    assert excinfo.value.endpoint == DecisionSettings().laya.base_url


async def test_超时映射成超时错误() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow", request=request)

    with pytest.raises(DecisionTimeoutError):
        _ = await _service(handler).predict("state", {})


async def test_连不上映射成连接错误() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(DecisionConnectionError):
        _ = await _service(handler).predict("state", {})


async def test_非法JSON报返回体错误() -> None:
    service = _service(lambda request: httpx2.Response(200, content=b"not json"))

    with pytest.raises(DecisionResponseError, match="不是合法 JSON"):
        _ = await service.predict("state", {})


async def test_顶层不是对象报返回体错误() -> None:
    service = _service(lambda request: httpx2.Response(200, json=[1, 2]))

    with pytest.raises(DecisionResponseError, match="顶层不是对象"):
        _ = await service.predict("state", {})


async def test_缺answers报返回体错误() -> None:
    service = _service(lambda request: httpx2.Response(200, json={"model": "m"}))

    with pytest.raises(DecisionResponseError, match="缺少 answers"):
        _ = await service.predict("state", {})


async def test_429退避后成功(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    calls: list[int] = []

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx2.Response(429, json={})
        return httpx2.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    _ = await _service(handler).predict("state", {})

    assert len(calls) == 2


async def test_529退避后成功(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    calls: list[int] = []

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx2.Response(529, json={})
        return httpx2.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    _ = await _service(handler).predict("state", {})

    assert len(calls) == 2


async def test_重试耗尽后报错(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    calls: list[int] = []

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(429, json={})

    with pytest.raises(DecisionRequestError):
        _ = await _service(handler).predict("state", {})

    assert len(calls) == 3  # retries=2 → 总请求数 3


async def test_retries为零时不重试(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    calls: list[int] = []

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(429, json={})

    service = DecisionService(
        DecisionSettings(
            laya=DecisionEndpointSettings(enabled=True, base_url="http://laya.test/v1", retries=0)
        )
    )
    service._clients = {  # pyright: ignore[reportPrivateUsage]
        "laya": httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler), base_url="http://laya.test/v1"
        )
    }

    with pytest.raises(DecisionRequestError):
        _ = await service.predict("state", {})

    assert len(calls) == 1


async def test_其他4xx不重试() -> None:
    calls: list[int] = []

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(400, json={})

    with pytest.raises(DecisionRequestError):
        _ = await _service(handler).predict("state", {})

    assert len(calls) == 1


_CHOICE_BODY = {
    "model": "m",
    "answers": {
        _QID: {
            "type": "choice",
            "choice": "billing",
            "probabilities": {"billing": 0.88, "other": 0.12},
            "confidence": 0.81,
        }
    },
    "usage": {"input_tokens": 5, "output_tokens": 0},
}
_SCORE_BODY = {
    "model": "m",
    "answers": {
        _QID: {
            "type": "score",
            "score": 1.05,
            "legend": {"0": "平静", "1": "不满", "2": "愤怒"},
            "probabilities": {"0": 0.0, "1": 0.95, "2": 0.05},
            "confidence": 0.92,
        }
    },
    "usage": {"input_tokens": 5, "output_tokens": 0},
}
_NOUL_BODY = {
    "model": "m",
    "answers": {_QID: {"type": "noul", "noul": 0.93}},
    "usage": {"input_tokens": 5, "output_tokens": 0},
}


async def test_choose返回选中选项与分布() -> None:
    service = _service(lambda request: httpx2.Response(200, json=_CHOICE_BODY))

    answer = await service.choose("要退款", "归哪个部门？", {"billing": "账单", "other": None})

    assert answer.choice == "billing"
    assert answer.probabilities["billing"] == 0.88
    assert answer.confidence == 0.81


async def test_rate返回得分与档位说明() -> None:
    service = _service(lambda request: httpx2.Response(200, json=_SCORE_BODY))

    answer = await service.rate("钱扣了两次", "愤怒程度？", ["平静", "不满", "愤怒"])

    assert answer.score == 1.05
    assert answer.legend["1"] == "不满"
    assert answer.probabilities["1"] == 0.95
    assert answer.confidence == 0.92


async def test_ask返回是概率() -> None:
    service = _service(lambda request: httpx2.Response(200, json=_NOUL_BODY))

    assert await service.ask("我要退款", "是否要求退款？") == 0.93


async def test_便捷方法缺答案键时报错() -> None:
    service = _service(lambda request: httpx2.Response(200, json={"model": "m", "answers": {}}))

    with pytest.raises(DecisionResponseError) as excinfo:
        _ = await service.ask("state", "是否？")

    assert "answer" in str(excinfo.value)


async def test_便捷方法字段不完整时报错() -> None:
    service = _service(
        lambda request: httpx2.Response(
            200,
            json={
                "model": "m",
                "answers": {_QID: {"type": "choice", "choice": "billing"}},
                "usage": {},
            },
        )
    )

    with pytest.raises(DecisionResponseError):
        _ = await service.choose("state", "选一个", {"billing": None})


async def test_score字段不完整时报错() -> None:
    service = _service(
        lambda request: httpx2.Response(
            200,
            json={
                "model": "m",
                "answers": {_QID: {"type": "score", "score": 1.0}},
                "usage": {},
            },
        )
    )

    with pytest.raises(DecisionResponseError):
        _ = await service.rate("state", "打分", ["低", "高"])


async def test_noul字段类型不对时报错() -> None:
    service = _service(
        lambda request: httpx2.Response(
            200,
            json={"model": "m", "answers": {_QID: {"type": "noul", "noul": "yes"}}, "usage": {}},
        )
    )

    with pytest.raises(DecisionResponseError):
        _ = await service.ask("state", "是否？")


async def test_predict把三原语混进同一趟() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_NOUL_BODY)

    service = _service(handler)

    _ = await service.predict(
        "state",
        {
            "a": noul("是否？"),
            "b": choice("选一个", {"x": None}),
            "c": score("打分", ["低", "高"]),
        },
    )

    body = cast("dict[str, object]", json.loads(seen[0].content))
    assert sorted(cast("dict[str, object]", body["questions"])) == ["a", "b", "c"]
    assert len(seen) == 1  # 三题只发一次请求


async def test_重试会记日志(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(decision_service, "_BACKOFF_BASE", 0.0)
    files = setup_logging(LogSettings(dir=str(tmp_path), level="TRACE"))
    calls: list[int] = []

    def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx2.Response(429, json={})
        return httpx2.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    _ = await _service(handler).predict("state", {})

    assert len(calls) == 2
    assert "判断调用重试" in files.app.read_text(encoding="utf-8")
