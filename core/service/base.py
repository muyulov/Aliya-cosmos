"""服务基类与状态机。

一个服务就是一个实现了 Service 的类，由 ServiceManager 统一管理生命周期。
服务之间通过 name 声明依赖，manager 据此做拓扑排序。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, override


class ServiceState(StrEnum):
    """服务状态。"""

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(slots=True)
class HealthStatus:
    """单个健康检查结果。"""

    name: str
    healthy: bool
    state: ServiceState
    detail: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "healthy": self.healthy,
            "state": self.state.value,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.extra:
            payload.update(self.extra)
        return payload


class Service:
    """服务基类。

    子类需要声明唯一的 name，并按需重写 start / stop / health。
    start 与 stop 必须是幂等的：重复调用不应报错，因此基类提供空实现，
    子类只重写自己关心的方法，不做强制约束。

    不继承 ABC：服务的契约由 ServiceManager 在 register 阶段校验（name 非空、
    不重复），比抽象方法更适合"按需重写"的场景。
    """

    #: 服务名，全局唯一，供依赖声明与查找使用
    name: ClassVar[str] = ""

    #: 依赖的服务名列表，manager 启动前会先启动这些服务。
    #: 用 tuple 而非 Sequence：类变量覆写要求类型不变（Invariant），
    #: 子类声明 tuple[str, ...] 时若基类是 Sequence[str] 会被判为不兼容覆写。
    #: 元组本身不可变，作为类级常量也更安全。
    dependencies: ClassVar[tuple[str, ...]] = ()

    def __init__(self) -> None:
        self.state: ServiceState = ServiceState.CREATED

    @property
    def running(self) -> bool:
        return self.state is ServiceState.RUNNING

    async def start(self) -> None:
        """启动服务。子类重写时应先判断自身状态以保证幂等。"""

    async def stop(self) -> None:
        """停止服务。子类重写时应先判断自身状态以保证幂等。"""

    async def health(self) -> HealthStatus:
        """健康检查，默认按状态判断。"""
        healthy = self.state is ServiceState.RUNNING
        detail = "" if healthy else f"服务未运行，当前状态 {self.state.value}"
        return HealthStatus(name=self.name, healthy=healthy, state=self.state, detail=detail)

    @override
    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} state={self.state.value}>"
