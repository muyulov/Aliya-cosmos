"""示例 CRUD 测试。"""

from __future__ import annotations

from httpx import AsyncClient

from tests.helpers import as_dict, body, data_dict, data_list


async def test_启动时预热一条示例数据(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/items")
    assert resp.status_code == 200

    items = data_list(resp)
    assert len(items) == 1
    assert as_dict(items[0])["name"] == "示例条目"


async def test_创建后可查到(client: AsyncClient) -> None:
    created = await client.post("/api/v1/items", json={"name": "新条目", "description": "描述"})
    assert created.status_code == 201

    assert body(created)["message"] == "created"
    item_id = data_dict(created)["id"]

    fetched = await client.get(f"/api/v1/items/{item_id}")
    assert fetched.status_code == 200
    assert data_dict(fetched)["name"] == "新条目"


async def test_详情不存在返回_404_信封(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/items/9999")
    assert resp.status_code == 404
    payload = body(resp)
    assert payload["code"] == "ITEM_NOT_FOUND"
    assert payload["data"] is None


async def test_删除后不可再查(client: AsyncClient) -> None:
    created = await client.post("/api/v1/items", json={"name": "待删"})
    item_id = data_dict(created)["id"]

    deleted = await client.delete(f"/api/v1/items/{item_id}")
    assert deleted.status_code == 200
    assert body(deleted)["message"] == "deleted"

    assert (await client.get(f"/api/v1/items/{item_id}")).status_code == 404


async def test_删除不存在的条目返回_404(client: AsyncClient) -> None:
    resp = await client.delete("/api/v1/items/9999")
    assert resp.status_code == 404
    assert body(resp)["code"] == "ITEM_NOT_FOUND"


async def test_名称缺失触发_422(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/items", json={})
    assert resp.status_code == 422
    payload = body(resp)
    assert payload["code"] == "INVALID_ARGUMENT"
    assert "错误" in as_dict(payload["data"])


async def test_名称超长触发_422(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/items", json={"name": "x" * 200})
    assert resp.status_code == 422


async def test_编号非法触发_422(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/items/0")
    assert resp.status_code == 422
