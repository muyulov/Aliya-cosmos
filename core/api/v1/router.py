"""v1 路由聚合。新增业务路由时在此 include_router。"""

from __future__ import annotations

from fastapi import APIRouter

from core.api.v1 import health, items

api_router = APIRouter()
api_router.include_router(health.router, tags=["健康检查"])
api_router.include_router(items.router, prefix="/items", tags=["示例条目"])
