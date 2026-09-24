"""服务容器：类型注册、惰性装配与生命周期编排。

职责：
- 注册服务**类型**（不是实例），服务之间按**类型**声明依赖。
- 惰性装配：首次访问时校验契约、拓扑排序、按序构造并注入依赖。
- 启动：按装配期固化的顺序逐个启动并记录耗时；任一失败则逆序回滚。
- 关闭：按启动顺序的逆序逐个关闭，吞掉单个异常，保证其余服务都能停下。
- 健康检查：聚合所有服务的状态。

装配失败一律 fail fast，抛 ServiceError 子树；ServiceError 不是 AppError，
因此只会让 lifespan 启动失败、进程退出，不会变成 4xx。
"""

from __future__ import annotations

import inspect
import time
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import TypeVar, cast, get_type_hints

from core.config import Settings, get_settings
from core.logger import faces, log
from core.service.base import HealthStatus, Service, ServiceState

S = TypeVar("S", bound=Service)

#: 单个服务的构造计划：（服务依赖的参数名 → 依赖类型, 需要注入配置的参数名）
_Plan = tuple[dict[str, type[Service]], list[str]]


class ServiceManager:
    """服务注册表与生命周期编排器。

    装配是惰性的：register 只登记类型，首次访问（get / services / names /
    health / start_all / stop_all）才校验契约、排序并构造实例。
    因此模块级构造一个 manager 不读配置、不实例化服务，没有 import 副作用。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        #: 容器持有的配置；装配时若为 None 则回退到全局单例
        self._settings = settings
        #: 注册顺序（装配前）
        self._types: list[type[Service]] = []
        #: 装配产出的实例表
        self._instances: dict[type[Service], Service] = {}
        #: 装配后固化的启动顺序，关闭时直接逆序，无需可变状态
        self._order: list[type[Service]] = []
        self._built = False

    # ---------- 注册 ----------

    def register(self, service_type: type[S]) -> type[S]:
        """注册服务类型。非 Service 子类、重复注册、装配后注册都会抛错。"""
        self._ensure_not_built()
        if not (isinstance(service_type, type) and issubclass(service_type, Service)):
            name = getattr(service_type, "__name__", repr(service_type))
            raise ServiceContractError(f"{name} 不是 Service 子类，无法注册")
        if service_type in self._types:
            raise ServiceContractError(f"服务类型重复注册：{service_type.__name__}")
        self._types.append(service_type)
        return service_type

    def register_all(self, service_types: Sequence[type[Service]]) -> None:
        for service_type in service_types:
            _ = self.register(service_type)

    # ---------- 查找 ----------

    def get(self, service_type: type[S]) -> S:
        """按**精确类型**取服务实例。

        刻意不做 isinstance 线性扫描：那会在"注册的是子类、查的是基类"时
        静默返回第一个匹配，属于隐式行为。因此依赖必须声明被注册的具体类型。
        """
        self._ensure_built()
        try:
            instance = self._instances[service_type]
        except KeyError as exc:
            raise ServiceNotRegisteredError(service_type) from exc
        return cast("S", instance)

    @property
    def services(self) -> Sequence[Service]:
        """全部服务实例，按启动顺序。"""
        self._ensure_built()
        return tuple(self._instances[service_type] for service_type in self._order)

    @property
    def names(self) -> Sequence[str]:
        """全部服务的展示名，按启动顺序。"""
        return tuple(service.label for service in self.services)

    # ---------- 启动与关闭 ----------

    async def start_all(self) -> None:
        """按依赖顺序启动全部服务，失败则回滚。"""
        self._ensure_built()
        started: list[Service] = []

        for service_type in self._order:
            service = self._instances[service_type]
            if service.state is ServiceState.RUNNING:
                continue

            service.state = ServiceState.STARTING
            begin = time.perf_counter()
            try:
                await service.start()
            except Exception as exc:
                service.state = ServiceState.FAILED
                service.log_error("服务启动失败，开始回滚", exc, 已启动=len(started))
                await self._rollback(started)
                raise ServiceStartError(service.label, exc) from exc

            service.state = ServiceState.RUNNING
            started.append(service)
            elapsed_ms = round((time.perf_counter() - begin) * 1000, 1)
            log.info("服务已启动", face=faces.START, 服务=service.label, 耗时=f"{elapsed_ms}ms")

        log.info("全部服务启动完成", 服务数=len(started))

    async def stop_all(self) -> None:
        """按启动的逆序关闭所有服务。

        顺序在装配期就已固定为 reversed(self._order)，因此这里不需要记录
        "本次启动了哪些"，重复调用天然幂等。
        """
        self._ensure_built()
        for service_type in reversed(self._order):
            service = self._instances[service_type]
            if service.state in (ServiceState.STOPPED, ServiceState.CREATED):
                continue

            service.state = ServiceState.STOPPING
            try:
                await service.stop()
            except Exception as exc:
                # 关闭阶段不阻断其他服务，仅记录
                service.state = ServiceState.FAILED
                service.log_error("服务关闭异常", exc)
                continue

            service.state = ServiceState.STOPPED
            log.info("服务已停止", face=faces.BYE, 服务=service.label)

        log.info("全部服务已停止")

    async def _rollback(self, started: Sequence[Service]) -> None:
        """逆序回滚本次已启动的服务。"""
        for service in reversed(started):
            try:
                await service.stop()
            except Exception as exc:
                service.state = ServiceState.FAILED
                service.log_error("回滚时服务关闭异常", exc)
            else:
                service.state = ServiceState.STOPPED

    # ---------- 健康检查 ----------

    async def health(self) -> list[HealthStatus]:
        """聚合所有服务的健康状态。"""
        results: list[HealthStatus] = []
        for service in self.services:
            try:
                results.append(await service.health())
            except Exception as exc:
                results.append(
                    HealthStatus(
                        name=service.label,
                        healthy=False,
                        state=service.state,
                        detail=f"健康检查抛错：{type(exc).__name__}: {exc}",
                    )
                )
        return results

    # ---------- 生命周期上下文 ----------

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[ServiceManager]:
        """异步上下文：进入时启动全部服务，退出时全部关闭。"""
        await self.start_all()
        try:
            yield self
        finally:
            await self.stop_all()

    # ---------- 装配 ----------

    def _ensure_built(self) -> None:
        """首次访问时触发一次装配。"""
        if not self._built:
            self._build()

    def _ensure_not_built(self) -> None:
        if self._built:
            msg = "服务容器已完成装配，不能再注册新服务"
            raise ServiceContractError(msg)

    def _build(self) -> None:
        """校验契约 → 拓扑排序 → 按序构造注入 → 固化顺序。"""
        settings = self._settings or get_settings()
        plans = {
            service_type: self._validate_contract(service_type) for service_type in self._types
        }
        order = self._resolve_order()

        for service_type in order:
            service_params, settings_params = plans[service_type]
            kwargs: dict[str, object] = {}
            for param_name in settings_params:
                kwargs[param_name] = settings
            for param_name, dependency_type in service_params.items():
                kwargs[param_name] = self._instances[dependency_type]
            # 构造器参数是动态拼出来的，签名无法静态校验，故这里显式收敛类型
            factory = cast("Callable[..., Service]", service_type)
            self._instances[service_type] = factory(**kwargs)

        self._order = order
        self._built = True

    def _validate_contract(self, service_type: type[Service]) -> _Plan:
        """对账 dependencies 声明与 __init__ 签名，返回该服务的构造计划。

        用 get_type_hints 而非裸注解：本仓库满屏 from __future__ import
        annotations，注解都是字符串，必须显式解析。
        """
        hints = get_type_hints(service_type.__init__)
        _ = hints.pop("return", None)

        service_params: dict[str, type[Service]] = {}
        settings_params: list[str] = []

        for param_name, param in inspect.signature(service_type.__init__).parameters.items():
            if param_name == "self":
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                raise ServiceContractError(
                    f"{service_type.__name__}.__init__ 含可变参数 {param_name}，容器无法注入"
                )

            param_type = hints.get(param_name)
            if param_type is None:
                raise ServiceContractError(
                    f"{service_type.__name__}.__init__ 的参数 {param_name} 缺少类型注解，"
                    f"容器无法注入"
                )
            if param_type is Settings:
                settings_params.append(param_name)
                continue
            if isinstance(param_type, type) and issubclass(param_type, Service):
                service_params[param_name] = param_type
                continue

            label = getattr(param_type, "__name__", repr(param_type))
            raise ServiceContractError(
                f"{service_type.__name__}.__init__ 的参数 {param_name} 注解为 {label}，"
                f"容器只支持 Service 与 Settings"
            )

        declared = set(service_type.dependencies)
        injected = set(service_params.values())

        undeclared = injected - declared
        if undeclared:
            names = "、".join(sorted(item.__name__ for item in undeclared))
            raise ServiceContractError(
                f"{service_type.__name__} 的构造器依赖 {names} 未在 dependencies 中声明"
            )

        unbound = declared - injected
        if unbound:
            names = "、".join(sorted(item.__name__ for item in unbound))
            raise ServiceContractError(
                f"{service_type.__name__} 声明了依赖 {names}，但构造器没有对应参数"
            )

        return service_params, settings_params

    def _resolve_order(self) -> list[type[Service]]:
        """按 dependencies 做拓扑排序，检测环与未注册依赖。"""
        order: list[type[Service]] = []
        visiting: set[type[Service]] = set()
        visited: set[type[Service]] = set()

        def visit(service_type: type[Service]) -> None:
            if service_type in visited:
                return
            if service_type in visiting:
                raise CircularDependencyError(service_type.__name__)
            if service_type not in self._types:
                raise MissingDependencyError(service_type.__name__)

            visiting.add(service_type)
            for dependency in service_type.dependencies:
                visit(dependency)
            visiting.discard(service_type)
            visited.add(service_type)
            order.append(service_type)

        for service_type in self._types:
            visit(service_type)
        return order


class ServiceError(Exception):
    """服务相关错误基类。

    不是 AppError，因此不会被转成 4xx：装配期错误只会让 lifespan 启动失败、
    进程退出，这是预期的 fail fast。
    """


class ServiceContractError(ServiceError):
    """服务契约不合法。

    覆盖：非 Service 子类、重复注册、装配后注册、构造器签名不可解释、
    dependencies 声明与构造器签名不一致。
    """


class ServiceNotRegisteredError(ServiceError):
    """按类型查找时该类型未注册。"""

    service_type: type[Service]

    def __init__(self, service_type: type[Service]) -> None:
        self.service_type = service_type
        super().__init__(f"服务未注册：{service_type.__name__}")


class ServiceStartError(ServiceError):
    """服务启动失败。"""

    service_name: str
    cause: BaseException

    def __init__(self, service_name: str, cause: BaseException) -> None:
        self.service_name = service_name
        self.cause = cause
        super().__init__(f"服务 {service_name} 启动失败：{type(cause).__name__}: {cause}")


class CircularDependencyError(ServiceError):
    """检测到循环依赖。"""

    def __init__(self, service_name: str) -> None:
        super().__init__(f"检测到循环依赖，涉及服务：{service_name}")


class MissingDependencyError(ServiceError):
    """依赖的服务未注册。"""

    def __init__(self, service_name: str) -> None:
        super().__init__(f"依赖的服务未注册：{service_name}")
