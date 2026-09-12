"""示例条目接口。

演示完整链路：路由做参数校验与响应拼装，业务逻辑在 service 层，
业务异常冒泡到全局处理器转成统一信封。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Path
from pydantic import BaseModel, Field

from core.api.deps import ItemServiceDep, RequestIdDep
from core.api.response import ok_body

router = APIRouter()


class ItemCreate(BaseModel):
    """创建请求体。"""

    name: str = Field(min_length=1, max_length=100, description="条目名称")
    description: str = Field(default="", max_length=500, description="条目描述")


class ItemOut(BaseModel):
    """条目响应体。"""

    id: int
    name: str
    description: str
    created_at: str


@router.get("")
async def list_items(service: ItemServiceDep, request_id: RequestIdDep) -> dict[str, object]:
    """列出全部条目。"""
    items = await service.list_items()
    return ok_body([item.to_dict() for item in items], request_id)


@router.get("/{item_id}")
async def get_item(
    service: ItemServiceDep,
    request_id: RequestIdDep,
    item_id: Annotated[int, Path(ge=1, description="条目编号")],
) -> dict[str, object]:
    """查询单个条目，不存在时返回 404。"""
    item = await service.get_item(item_id)
    return ok_body(item.to_dict(), request_id)


@router.post("", status_code=201)
async def create_item(
    service: ItemServiceDep,
    request_id: RequestIdDep,
    payload: Annotated[ItemCreate, Body()],
) -> dict[str, object]:
    """创建条目。"""
    item = await service.create_item(payload.name, payload.description)
    return ok_body(item.to_dict(), request_id, message="created")


@router.delete("/{item_id}")
async def delete_item(
    service: ItemServiceDep,
    request_id: RequestIdDep,
    item_id: Annotated[int, Path(ge=1, description="条目编号")],
) -> dict[str, object]:
    """删除条目，不存在时返回 404。"""
    await service.delete_item(item_id)
    return ok_body({"id": item_id}, request_id, message="deleted")
