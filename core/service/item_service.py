"""示例服务：内存仓储的条目管理。

演示完整链路：
- 声明依赖类型，由容器在构造时注入。
- start 预热数据，stop 释放资源。
- 时间来源取自 ClockService，因此测试可注入固定时间。
- 业务异常直接用 AppError 子类表达，由 api 层统一转成 HTTP 响应。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import ClassVar, override

from core.service.base import HealthStatus, Service, ServiceState
from core.service.clock_service import ClockService


@dataclass(slots=True)
class Item:
    """示例实体。

    created_at 的缺省值仅供脱离服务单独构造时兜底，
    服务路径上一律由 ClockService 供给，时间来源保持唯一。
    """

    id: int
    name: str
    description: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at.isoformat(),
        }


class ItemNotFoundError(LookupError):
    """条目不存在。"""

    item_id: int

    def __init__(self, item_id: int) -> None:
        self.item_id = item_id
        super().__init__(f"条目不存在：{item_id}")


class ItemService(Service):
    """示例服务实现。"""

    name: ClassVar[str] = "item"
    dependencies: ClassVar[tuple[type[Service], ...]] = (ClockService,)

    def __init__(self, clock: ClockService) -> None:
        super().__init__()
        self._clock: ClockService = clock
        self._items: dict[int, Item] = {}
        self._counter: count[int] = count(1)

    @override
    async def start(self) -> None:
        """幂等启动：预热内存数据。"""
        if self.state is ServiceState.RUNNING:
            return
        self._items.clear()
        self._counter = count(1)
        _ = self._create("示例条目", "启动时预热的示例数据")

    @override
    async def stop(self) -> None:
        """幂等关闭：释放内存数据。"""
        if self.state in (ServiceState.STOPPED, ServiceState.CREATED):
            return
        self._items.clear()

    @override
    async def health(self) -> HealthStatus:
        return HealthStatus(
            name=self.label,
            healthy=self.running,
            state=self.state,
            extra={"条目数": len(self._items)},
        )

    async def list_items(self) -> list[Item]:
        return sorted(self._items.values(), key=lambda item: item.id)

    async def get_item(self, item_id: int) -> Item:
        try:
            return self._items[item_id]
        except KeyError as exc:
            raise ItemNotFoundError(item_id) from exc

    async def create_item(self, name: str, description: str = "") -> Item:
        return self._create(name, description)

    async def delete_item(self, item_id: int) -> None:
        if item_id not in self._items:
            raise ItemNotFoundError(item_id)
        del self._items[item_id]

    def _create(self, name: str, description: str) -> Item:
        item = Item(
            id=next(self._counter),
            name=name,
            description=description,
            created_at=self._clock.now(),
        )
        self._items[item.id] = item
        return item
