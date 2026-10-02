"""判断服务测试。

约定：
- 客户端在 start() 里建，容器又不认它，所以除「未启用」那条外一律白盒塞 service._clients。
- 用 httpx2.MockTransport 注入 handler：不起 mock server、不打桩 socket，
  handler 直接拿到 httpx2.Request，可断言请求体与请求头（本层最值得断言的东西）。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx2
import pytest

from core.config import DecisionSettings
from core.decision import DecisionConfigError, DecisionResult, DecisionService, noul


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
