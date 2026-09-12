"""健康检查与响应信封测试。"""

from __future__ import annotations

from typing import cast

from httpx import AsyncClient

from core.api.middleware import REQUEST_ID_HEADER
from tests.helpers import as_dict, body, data_dict


async def test_健康检查返回服务明细(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200

    assert body(resp)["code"] == "OK"
    data = data_dict(resp)
    assert data["status"] == "ok"
    assert data["env"] == "test"

    services = cast("list[object]", data["services"])
    assert len(services) == 1
    service = as_dict(services[0])
    assert service["name"] == "item"
    assert service["state"] == "running"
    assert service["healthy"] is True
    assert service["条目数"] == 1


async def test_存活探针(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/ready")
    assert resp.status_code == 200
    assert data_dict(resp)["status"] == "ok"


async def test_响应头带_request_id_且与信封一致(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/ready")
    header_id = resp.headers[REQUEST_ID_HEADER]
    assert header_id
    assert body(resp)["request_id"] == header_id


async def test_透传调用方传入的_request_id(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/ready", headers={REQUEST_ID_HEADER: "trace-abc"})
    assert resp.headers[REQUEST_ID_HEADER] == "trace-abc"
    assert body(resp)["request_id"] == "trace-abc"


async def test_未知路径返回统一信封(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/not-exist")
    assert resp.status_code == 404
    payload = body(resp)
    assert payload["code"] == "HTTP_404"
    assert set(payload) == {"code", "message", "data", "request_id"}
