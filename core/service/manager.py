"""服务生命周期管理器。

负责：
- 注册与查找服务（按实例或按类型）。
- 启动：按依赖拓扑排序，逐个启动并记录耗时；任一失败则逆序回滚已启动的服务。
- 关闭：严格逆序，逐个吞掉异常，保证所有服务都能尝试停下。
- 健康检查：聚合所有服务的状态。
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import TypeVar

from core.logger import faces, log
from core.service.base import HealthStatus, Service, ServiceState

S = TypeVar("S", bound=Service)


class ServiceManager:
    """服务注册表与生命周期编排器。"""

    def __init__(self) -> None:
        self._services: dict[str, Service] = {}
        #: 启动顺序，用于关闭时逆序
        self._start_order: list[str] = []

    # ---------- 注册与查找 ----------

    def register(self, service: Service) -> Service:
        """注册一个服务实例。同名服务重复注册会抛错。"""
        if not service.name:
            raise ValueError(f"{type(service).__name__} 未设置 name，无法注册")
        if service.name in self._services:
            raise ValueError(f"服务名重复：{service.name}")
        self._services[service.name] = service
        return service

    def register_all(self, services: Sequence[Service]) -> None:
        for service in services:
            _ = self.register(service)

    def get(self, service_type: type[S]) -> S:
        """按类型取服务实例。"""
        for service in self._services.values():
            if isinstance(service, service_type):
                return service
        raise KeyError(f"服务未注册：{service_type.__name__}")

    def get_by_name(self, name: str) -> Service:
        try:
            return self._services[name]
        except KeyError as exc:
            raise KeyError(f"服务未注册：{name}") from exc

    @property
    def services(self) -> Sequence[Service]:
        return tuple(self._services.values())

    @property
    def names(self) -> Sequence[str]:
        return tuple(self._services)

    # ---------- 启动与关闭 ----------

    async def start_all(self) -> None:
        """按依赖顺序启动全部服务，失败则回滚。"""
        order = self._resolve_order()
        started: list[Service] = []

        for name in order:
            service = self._services[name]
            if service.state is ServiceState.RUNNING:
                continue

            service.state = ServiceState.STARTING
            begin = time.perf_counter()
            try:
                await service.start()
            except Exception as exc:
                service.state = ServiceState.FAILED
                log.error(
                    "服务启动失败，开始回滚",
                    face=faces.BOOM,
                    服务=name,
                    错误=f"{type(exc).__name__}: {exc}",
                    已启动=len(started),
                )
                await self._rollback(started)
                raise ServiceStartError(name, exc) from exc

            service.state = ServiceState.RUNNING
            started.append(service)
            elapsed_ms = round((time.perf_counter() - begin) * 1000, 1)
            log.info("服务已启动", face=faces.START, 服务=name, 耗时=f"{elapsed_ms}ms")

        self._start_order = [s.name for s in started]
        log.info("全部服务启动完成", 服务数=len(started))

    async def stop_all(self) -> None:
        """按启动的逆序关闭所有服务。"""
        names = list(self._start_order) or list(self._services)
        for name in reversed(names):
            service = self._services.get(name)
            if service is None or service.state in (ServiceState.STOPPED, ServiceState.CREATED):
                continue

            service.state = ServiceState.STOPPING
            try:
                await service.stop()
            except Exception as exc:
                # 关闭阶段不阻断其他服务，仅记录
                service.state = ServiceState.FAILED
                log.error(
                    "服务关闭异常",
                    face=faces.BOOM,
                    服务=name,
                    错误=f"{type(exc).__name__}: {exc}",
                )
                continue

            service.state = ServiceState.STOPPED
            log.info("服务已停止", face=faces.BYE, 服务=name)

        self._start_order.clear()
        log.info("全部服务已停止")

    async def _rollback(self, started: Sequence[Service]) -> None:
        """逆序回滚已启动的服务。"""
        for service in reversed(started):
            try:
                await service.stop()
            except Exception as exc:
                log.error(
                    "回滚时服务关闭异常",
                    face=faces.BOOM,
                    服务=service.name,
                    错误=f"{type(exc).__name__}: {exc}",
                )
            else:
                service.state = ServiceState.STOPPED

    # ---------- 健康检查 ----------

    async def health(self) -> list[HealthStatus]:
        """聚合所有服务的健康状态。"""
        results: list[HealthStatus] = []
        for service in self._services.values():
            try:
                results.append(await service.health())
            except Exception as exc:
                results.append(
                    HealthStatus(
                        name=service.name,
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

    # ---------- 依赖排序 ----------

    def _resolve_order(self) -> list[str]:
        """按依赖做拓扑排序，检测循环依赖与缺失依赖。"""
        order: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise CircularDependencyError(name)
            service = self._services.get(name)
            if service is None:
                raise MissingDependencyError(name)

            visiting.add(name)
            for dep in service.dependencies:
                visit(dep)
            visiting.discard(name)
            visited.add(name)
            order.append(name)

        for name in self._services:
            visit(name)
        return order


class ServiceError(Exception):
    """服务相关错误基类。"""


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
