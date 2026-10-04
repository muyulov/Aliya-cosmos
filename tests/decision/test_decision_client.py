"""判断层客户端工厂测试。"""

from __future__ import annotations

import httpx2

from core.config import DecisionEndpointSettings
from core.decision import (
    DecisionConfigError,
    DecisionConnectionError,
    DecisionError,
    DecisionRequestError,
    DecisionResponseError,
    DecisionTimeoutError,
    build_client,
)


def test_有密钥时带鉴权头() -> None:
    endpoint = DecisionEndpointSettings(base_url="https://api.typesafe.ai/v1", api_key="sk-x")

    client = build_client(endpoint)

    assert client.headers["authorization"] == "Bearer sk-x"
    # httpx2.AsyncClient 会把 base_url 规范成带尾斜杠，所以断言里也要带
    assert str(client.base_url) == "https://api.typesafe.ai/v1/"


def test_没密钥时不带鉴权头() -> None:
    endpoint = DecisionEndpointSettings(base_url="http://127.0.0.1:8000/v1")

    client = build_client(endpoint)

    assert "authorization" not in client.headers


def test_超时按配置传给客户端() -> None:
    endpoint = DecisionEndpointSettings(base_url="http://127.0.0.1:8000/v1", timeout=12.5)

    client = build_client(endpoint)

    assert isinstance(client.timeout, httpx2.Timeout)
    assert client.timeout.read == 12.5


def test_错误都继承自基类() -> None:
    assert issubclass(DecisionConfigError, DecisionError)
    assert issubclass(DecisionRequestError, DecisionError)
    assert issubclass(DecisionTimeoutError, DecisionError)
    assert issubclass(DecisionConnectionError, DecisionError)
    assert issubclass(DecisionResponseError, DecisionError)


def test_请求错误带对账坐标() -> None:
    error = DecisionRequestError(
        "端点返回 422", status_code=422, endpoint="http://x/v1", model="m", request_id="r"
    )

    assert (error.status_code, error.endpoint, error.model, error.request_id) == (
        422,
        "http://x/v1",
        "m",
        "r",
    )
