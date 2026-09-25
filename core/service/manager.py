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
from dataclasses import dataclass
from typing import TypeVar, cast, get_type_hints

from pydantic import BaseModel

from core.config import Settings, get_settings
from core.logger import faces, log
from core.service.base import HealthStatus, Service, ServiceState

S = TypeVar("S", bound=Service)


@dataclass(slots=True)
class _Plan:
    """单个服务的构造计划：三类注入参数按来源分组。"""

    services: dict[str, type[Service]]  # 参数名 → 依赖服务类型
    nodes: dict[str, type[BaseModel]]  # 参数名 → 配置节点类型
    whole_settings: list[str]  # 声明整份 Settings 的参数名


def _ensure_service_subclass(candidate: object) -> None:
    """运行期兜底：拒绝非 Service 子类的注册对象。

    参数刻意声明为 object。类型标注正确的调用方不会传错，但无类型标注的调用方
    与测试里用 cast 绕过类型检查的场景需要这层校验；声明成 object 而不是
    type[Service]，这层检查才在类型系统眼里有意义。
    """
    if not (isinstance(candidate, type) and issubclass(candidate, Service)):
        raise ServiceContractError(f"{candidate!r} 不是 Service 子类，无法注册")


class ServiceManager:
    """服务注册表与生命周期编排器。

    装配是惰性的：register 只登记类型，首次访问（get / services / names /
    health / start_all）才校验契约、排序并构造实例。stop_all 不是触发点：
    未装配过的容器没有实例可停，不该为"停止"去构造服务。
    因此模块级构造一个 manager 不读配置、不实例化服务，没有 import 副作用。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        #: 容器持有的配置；装配时若为 None 则回退到全局单例
        self._settings: Settings | None = settings
        #: 注册顺序（装配前）
        self._types: list[type[Service]] = []
        #: 装配产出的实例表
        self._instances: dict[type[Service], Service] = {}
        #: 装配后固化的启动顺序，关闭时直接逆序，无需可变状态
        self._order: list[type[Service]] = []
        self._built: bool = False

    # ---------- 注册 ----------

    def register(self, service_type: type[S]) -> type[S]:
        """注册服务类型。非 Service 子类、重复注册、装配后注册都会抛错。"""
        self._ensure_not_built()
        _ensure_service_subclass(service_type)
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
        未装配过的容器没有实例可停，直接返回，不为"停止"去构造服务。
        """
        if not self._built:
            return
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
        """校验契约 → 拓扑排序 → 按序构造注入 → 固化顺序。

        构造结果先攒在局部字典里，全部成功后才一次性提交：任一步失败时
        容器保持未装配状态，不会留下半成品实例。
        """
        settings = self._settings or get_settings()
        nodes = self._index_config_nodes(settings)
        plans = {
            service_type: self._validate_contract(service_type, nodes)
            for service_type in self._types
        }
        order = self._resolve_order()

        instances: dict[type[Service], Service] = {}
        for service_type in order:
            plan = plans[service_type]
            kwargs: dict[str, object] = {}
            for param_name in plan.whole_settings:
                kwargs[param_name] = settings
            for param_name, node_type in plan.nodes.items():
                kwargs[param_name] = nodes[node_type]
            for param_name, dependency_type in plan.services.items():
                kwargs[param_name] = instances[dependency_type]
            # 构造器参数是动态拼出来的，签名无法静态校验，故这里显式收敛类型
            factory = cast("Callable[..., Service]", service_type)
            instances[service_type] = factory(**kwargs)

        self._instances = instances
        self._order = order
        self._built = True

    @staticmethod
    def _index_config_nodes(settings: Settings) -> dict[type[BaseModel], BaseModel]:
        """按类型索引 Settings 的顶层配置节点，供构造器注解命中。

        只认顶层字段、不递归：服务要拿的配置写在哪一眼可见，也避免"某个嵌套
        深处的同类节点被静默注入"。
        """
        nodes: dict[type[BaseModel], BaseModel] = {}
        for field_name, field_info in type(settings).model_fields.items():
            # 显式收成 object：model_fields 是运行期字典，取值是 Any，
            # 直接用会让 Any 渗进后面的 isinstance 与 cast。
            annotation: object = field_info.annotation
            if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
                continue
            if annotation is Settings:  # 整份配置由构造器直接声明 Settings 承接
                continue
            if annotation in nodes:
                raise ServiceContractError(
                    f"配置节点类型 {annotation.__name__} 在 Settings 中出现多次，"
                    + "容器无法确定注入哪个"
                )
            nodes[annotation] = cast("BaseModel", getattr(settings, field_name))
        return nodes

    def _validate_contract(
        self, service_type: type[Service], nodes: dict[type[BaseModel], BaseModel]
    ) -> _Plan:
        """对账 dependencies 声明与 __init__ 签名，返回该服务的构造计划。

        用 get_type_hints 而非裸注解：本仓库满屏 from __future__ import
        annotations，注解都是字符串，必须显式解析。
        """
        hints = get_type_hints(service_type.__init__)
        prefix = f"{service_type.__name__}.__init__"

        plan = _Plan(services={}, nodes={}, whole_settings=[])

        for param_name, param in inspect.signature(service_type.__init__).parameters.items():
            if param_name == "self":
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                raise ServiceContractError(f"{prefix} 含可变参数 {param_name}，容器无法注入")

            # 显式收成 object：get_type_hints 取值是 Any，直接使用会让 Any
            # 扩散到后面的 isinstance / repr，触发类型检查器的未知类型告警。
            annotation: object = hints.get(param_name)
            if annotation is None:
                raise ServiceContractError(
                    f"{prefix} 的参数 {param_name} 缺少类型注解，容器无法注入"
                )
            if annotation is Settings:
                plan.whole_settings.append(param_name)
                continue
            if isinstance(annotation, type) and issubclass(annotation, Service):
                plan.services[param_name] = annotation
                continue
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                if annotation not in nodes:
                    raise ServiceContractError(
                        f"{prefix} 的参数 {param_name} 注解为 {annotation.__name__}，"
                        + "它不是 Settings 的顶层字段，请在 core/config/settings.py 中注册"
                    )
                plan.nodes[param_name] = annotation
                continue

            shown = annotation.__name__ if isinstance(annotation, type) else repr(annotation)
            raise ServiceContractError(
                f"{prefix} 的参数 {param_name} 注解为 {shown}，" + "容器只支持 Service 与配置节点"
            )

        declared = set(service_type.dependencies)
        injected = set(plan.services.values())

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

        return plan

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
