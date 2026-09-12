"""健康检查。

返回进程状态与各服务状态，使「进程在但依赖没就绪」可被反映出来，
而不是只回一个静态 ok。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from core.api.deps import RequestIdDep, SettingsDep, manager_dep
from core.api.response import ok_body
from core.service import ServiceManager

router = APIRouter()


@router.get("/health")
async def health(
    response: Response,
    request_id: RequestIdDep,
    settings: SettingsDep,
    manager: Annotated[ServiceManager, Depends(manager_dep)],
) -> dict[str, object]:
    """健康检查。任一服务不健康时返回 503。"""
    statuses = await manager.health()
    healthy = all(item.healthy for item in statuses)

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ok_body(
        {
            "status": "ok" if healthy else "degraded",
            "env": settings.app.env,
            "services": [item.to_dict() for item in statuses],
        },
        request_id,
    )


@router.get("/ready")
async def ready(request_id: RequestIdDep) -> dict[str, object]:
    """存活探针：只表示进程可响应。"""
    return ok_body({"status": "ok"}, request_id)
