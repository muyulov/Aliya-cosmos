"""服务基类与状态机。

一个服务就是一个实现了 Service 的类，由 ServiceManager 统一管理生命周期。
服务之间按**类型**声明依赖，容器据此排序并在构造时注入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, override

from core.logger import Log, log


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

    子类按需声明 name（展示标签，缺省取类名）与 dependencies（依赖的服务类型），
    并在构造器里接收依赖。容器在装配期把依赖作为普通参数注入。

    新增约定：`__init__` 只做赋值与接收依赖，连接、预热、加载这类动资源的活
    一律留到 `start()`。装配发生在 lifespan 之前，在 `__init__` 里连资源会让
    "装配失败"与"启动失败"混成一锅，回滚逻辑也会失去意义。

    start 与 stop 必须是幂等的：重复调用不应报错，因此基类提供空实现，
    子类只重写自己关心的方法，不做强制约束。

    不继承 ABC：服务的契约由 ServiceManager 在装配期校验（依赖声明与构造器
    签名对账），比抽象方法更适合"按需重写"的场景。
    """

    #: 展示名：仅用于日志与健康检查展示，缺省取类名，不参与依赖解析
    name: ClassVar[str] = ""

    #: 依赖的服务类型；容器据此排序并在构造时注入
    dependencies: ClassVar[tuple[type[Service], ...]] = ()

    def __init__(self) -> None:
        self.state: ServiceState = ServiceState.CREATED
        #: 带自身维度的日志门面：自带 服务= 字段与 [label] 消息前缀
        self.log: Log = log.bind(服务=self.label).prefix(self.label)

    @property
    def label(self) -> str:
        """展示名：显式 name 优先，否则取类名。"""
        return self.name or type(self).__name__

    def log_error(
        self,
        message: str,
        exc: BaseException | None = None,
        **fields: object,
    ) -> None:
        """统一错误日志：自动拼「错误=类型: 消息」，可选带异常对象。

        收敛各处重复的 f"{type(exc).__name__}: {exc}" 格式化。
        传 exc 时会写入 错误= 字段，同名传入字段会被覆盖。
        """
        if exc is not None:
            fields["错误"] = f"{type(exc).__name__}: {exc}"
        self.log.error(message, **fields)

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
        return HealthStatus(name=self.label, healthy=healthy, state=self.state, detail=detail)

    @override
    def __repr__(self) -> str:
        return f"<{type(self).__name__} label={self.label!r} state={self.state.value}>"
