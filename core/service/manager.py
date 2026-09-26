"""服务容器：类型注册、惰性装配与生命周期编排。

职责：
- 注册服务**类型**（不是实例），服务之间按**类型**声明依赖。
- 惰性装配：首次访问时校验契约、拓扑排序、按序构造并注入依赖。
- 启动：按依赖分层，同层并发、层间串行；单层有失败或整体被取消都逆序回滚本次动过的服务。
- 关闭：按启动顺序的逆序逐个关闭，单个超时或异常只记日志，保证其余服务都能停下。
- 健康检查：聚合所有服务的状态。

装配失败一律 fail fast，抛 ServiceError 子树；ServiceError 不是 AppError，
因此只会让 lifespan 启动失败、进程退出，不会变成 4xx。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TypeVar, cast, get_args, get_type_hints

from pydantic import BaseModel

from core.config import ServiceSettings, Settings, get_settings
from core.logger import faces, log
from core.service.base import HealthStatus, Service, ServiceState, Unset

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


def _config_node_type(annotation: object) -> type[BaseModel] | None:
    """从字段注解中取出配置节点类型。

    支持 `X` 与 `X | None`（PEP 604 / `Optional`）：后者只认「恰好一个 BaseModel
    分支、其余分支都是 NoneType」。`X | Y` 无法确定注入哪个、`list[X]` 这类泛型
    根本不是节点，都返回 None。
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    # get_args 的返回类型是 tuple[Any, ...]：显式 cast 成 object 元组，
    # 否则 Any 会渗进下面的 isinstance / issubclass（只写注解挡不住）。
    args = cast("tuple[object, ...]", get_args(annotation))
    members = [arg for arg in args if isinstance(arg, type) and issubclass(arg, BaseModel)]
    if len(members) == 1 and len(args) == len(members) + 1 and type(None) in args:
        return members[0]
    return None


def _resolve_timeout(override: float | Unset | None, default: float | None) -> float | None:
    """三态解析：UNSET 跟随全局默认，None 表示不限制，数字即该值。"""
    return default if isinstance(override, Unset) else override


async def _call_with_timeout(
    action: Callable[[], Coroutine[object, object, None]], timeout: float | None
) -> None:
    """按超时调用服务钩子，超时统一抛 HookTimeoutError。

    刻意不用 asyncio.timeout / wait_for：它们把「框架超时」也表达成 TimeoutError，
    与业务自己抛的 TimeoutError 撞型，会把真实原因伪装成「启动超时」。这里改成
    「定时取消 + 标志位」自行实现，只有本函数发出的取消才算超时；业务异常与外部
    取消都原样冒泡。异常不在这里吞，由调用方决定语义。
    """
    if timeout is None:
        await action()
        return

    hook = asyncio.ensure_future(action())
    loop = asyncio.get_running_loop()
    timed_out = False

    def _cancel_hook() -> None:
        nonlocal timed_out
        timed_out = True
        _ = hook.cancel()

    handle = loop.call_later(timeout, _cancel_hook)
    try:
        await hook
    except asyncio.CancelledError:
        if not timed_out:  # 外层取消，不是超时
            raise
        raise HookTimeoutError(f"超过 {timeout}s") from None
    finally:
        handle.cancel()


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
        #: 生命周期超时配置。装配前是默认值占位：启停路径必然经过 _ensure_built()，
        #: 占位值不会被读到。反过来不能在 __init__ 里调 get_settings()——
        #: 那会让模块级 default_manager = build_manager() 在 import 期读配置文件。
        self._service_settings: ServiceSettings = ServiceSettings()
        #: 装配产出的实例表
        self._instances: dict[type[Service], Service] = {}
        #: 装配后固化的启动顺序，关闭时直接逆序，无需可变状态
        self._order: list[type[Service]] = []
        #: 装配后按依赖分好的层，索引越小越靠前；启动时逐层推进
        self._levels: list[list[type[Service]]] = []
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
        """批量注册。全部校验通过后才一次性落盘，避免中途失败留下半截注册表。"""
        self._ensure_not_built()
        pending: list[type[Service]] = []
        for service_type in service_types:
            _ensure_service_subclass(service_type)
            if service_type in self._types or service_type in pending:
                raise ServiceContractError(f"服务类型重复注册：{service_type.__name__}")
            pending.append(service_type)
        self._types.extend(pending)

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
        """按依赖分层启动：同层并发、层间串行；任一失败或被取消都回滚已启动者。

        失败与取消共用同一条清理路径：`lifespan()` 的 `__aenter__` 抛错时
        `__aexit__` 不会执行、`stop_all` 也不会被调用，回滚只能在这里做完。
        """
        self._ensure_built()
        # 本次启动涉及的服务（含失败者与已 RUNNING 被跳过者），回滚时都要调一次
        # stop()：只登记成功者会漏掉 start() 中途抛错、已经申请了部分资源的那些；
        # 不含已 RUNNING 的则会在整体失败、进程退出时把它们留在 RUNNING。
        # 新启动的登记在协程内部完成：gather 自身被取消时调用方拿不到 outcomes。
        attempted: list[Service] = []

        try:
            for index, level in enumerate(self._levels):
                pending = [
                    self._instances[service_type]
                    for service_type in level
                    if self._instances[service_type].state is not ServiceState.RUNNING
                ]
                # 已 RUNNING 的也要进回滚名单：整体启动失败意味着进程即将退出，
                # 而 lifespan.__aenter__ 抛错时 stop_all 不会被调用——只回滚本次
                # 新启动的那些，会把之前就在跑的服务留在 RUNNING。
                attempted.extend(
                    self._instances[service_type]
                    for service_type in level
                    if self._instances[service_type].state is ServiceState.RUNNING
                )
                if not pending:
                    continue

                begin = time.perf_counter()
                outcomes = await asyncio.gather(
                    *(self._start_one(service, attempted) for service in pending),
                    return_exceptions=True,
                )
                elapsed_ms = round((time.perf_counter() - begin) * 1000, 1)

                # gather 保序 → 失败者与 pending 同序，取首个即注册顺序最靠前者
                failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
                if failures:
                    raise failures[0]

                # 服务名与耗时都给出来：只报「服务数」看不出慢的是哪一个，
                # 层号是展示用的 1-based，与代码里的 index 差 1。
                log.info(
                    "同层服务启动完成",
                    层=index + 1,
                    服务="、".join(service.label for service in pending),
                    耗时毫秒=elapsed_ms,
                )
        except BaseException:
            # 回滚必须留痕：否则日志里只有几条「服务已启动」后凭空冒出失败，
            # 看不出那些服务是被谁清理掉的。
            log.warning("启动未完成，回滚本次动过的服务", 服务数=len(attempted))
            await self._rollback(attempted)
            raise

        log.info("全部服务启动完成", 服务数=len(self._instances))

    async def _start_one(self, service: Service, attempted: list[Service]) -> None:
        """启动单个服务；超时与失败都收敛成 ServiceStartError。

        登记发生在所有失败可能发生之前：`start()` 抛错或超时（协程被取消）时，
        服务可能已经申请了连接、句柄这类资源，只有记进 `attempted` 才能在回滚时
        调到 `stop()` 去回收。并发向同一个 list append 是安全的——事件循环单线程，
        append 是同步操作，不会被其他协程插进来。
        """
        attempted.append(service)
        service.state = ServiceState.STARTING
        timeout = _resolve_timeout(service.start_timeout, self._service_settings.start_timeout)
        begin = time.perf_counter()
        try:
            await _call_with_timeout(service.start, timeout)
        except HookTimeoutError as exc:
            service.state = ServiceState.FAILED
            # HookTimeoutError 自身只有「超过 Ns」，这里换成带阶段说明的 cause：
            # ServiceStartError 的消息会拼上 cause 的 str()。
            cause = HookTimeoutError(f"启动超时，超过 {timeout}s")
            # 不带 错误= 字段：超时的「错误」就是超时本身，类型与秒数在消息和
            # 超时秒数 里已经说清，再拼一遍 HookTimeoutError 只是噪声。
            service.log_error("服务启动超时", 超时秒数=timeout)
            raise ServiceStartError(service.label, cause) from exc
        except Exception as exc:
            service.state = ServiceState.FAILED
            service.log_error("服务启动失败", exc)
            raise ServiceStartError(service.label, exc) from exc
        except BaseException:
            # 取消等非 Exception：必须继续冒泡，但状态不能留在 STARTING
            service.state = ServiceState.FAILED
            raise

        service.state = ServiceState.RUNNING
        elapsed_ms = round((time.perf_counter() - begin) * 1000, 1)
        # 用服务自己的门面：与服务侧的失败日志同形（[label] 前缀 + 服务= 字段），
        # 单服务耗时也让「同层里是谁慢」有据可查
        service.log.info("服务已启动", face=faces.START, 耗时毫秒=elapsed_ms)

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
            timeout = _resolve_timeout(service.stop_timeout, self._service_settings.stop_timeout)
            begin = time.perf_counter()
            try:
                await _call_with_timeout(service.stop, timeout)
            except HookTimeoutError:
                service.state = ServiceState.FAILED
                service.log_error("服务关闭超时", 超时秒数=timeout)
                continue
            except Exception as exc:
                # 关闭阶段不阻断其他服务，仅记录
                service.state = ServiceState.FAILED
                service.log_error("服务关闭异常", exc)
                continue

            service.state = ServiceState.STOPPED
            elapsed_ms = round((time.perf_counter() - begin) * 1000, 1)
            service.log.info("服务已停止", face=faces.BYE, 耗时毫秒=elapsed_ms)

        log.info("全部服务已停止", face=faces.BYE, 服务数=len(self._order))

    async def _rollback(self, attempted: Sequence[Service]) -> None:
        """逆序清理本次动过的服务；同样套 stop 超时，但不阻断其余回滚。

        成功启动的（RUNNING）与启动失败的（FAILED）都要调一次 stop()：失败者可能
        已经申请了部分资源，那是它唯一的回收入口。因此 stop() 必须幂等，并且能
        安全地作用在「从未成功启动过」的服务上。
        失败者的状态保持 FAILED——那是它的终态标记，清理成功不该把它抹掉。
        """
        for service in reversed(attempted):
            # 启动失败者的 FAILED 是终态标记：清理成功不该把它抹成 STOPPED
            was_failed = service.state is ServiceState.FAILED
            service.state = ServiceState.STOPPING
            timeout = _resolve_timeout(service.stop_timeout, self._service_settings.stop_timeout)
            try:
                await _call_with_timeout(service.stop, timeout)
            except HookTimeoutError:
                service.state = ServiceState.FAILED
                service.log_error("回滚时服务关闭超时", 超时秒数=timeout)
            except Exception as exc:
                service.state = ServiceState.FAILED
                service.log_error("回滚时服务关闭异常", exc)
            else:
                service.state = ServiceState.FAILED if was_failed else ServiceState.STOPPED
                service.log.info("回滚时服务已关闭", face=faces.BYE)

        # 回滚没有「全部服务已停止」那样的收尾，这里补一句，
        # 免得日志断在半路、看不出清理是否已经做完；颜文字不用默认的 CHEER，
        # 这是异常路径的收尾，不是喜讯
        log.info("回滚完成", face=faces.THINK, 服务数=len(attempted))

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
        # 显式判 None 而不是 `or`：配置模型一旦定义了 __bool__ / __len__，
        # 空配置会被判为假，从而静默回退到全局单例、丢掉调用方传入的那份。
        settings = self._settings if self._settings is not None else get_settings()
        nodes = self._index_config_nodes(settings)
        plans = {
            service_type: self._validate_contract(service_type, nodes)
            for service_type in self._types
        }
        levels = self._resolve_levels()

        instances: dict[type[Service], Service] = {}
        for level in levels:
            for service_type in level:
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

        self._service_settings = settings.service
        self._instances = instances
        self._levels = levels
        # 扁平序按层展开：仍是合法拓扑序，reversed 仍是合法逆拓扑序，
        # 因此 services / names 的展示顺序与 stop_all 的关闭顺序都不用改
        self._order = [service_type for level in levels for service_type in level]
        self._built = True

    @staticmethod
    def _index_config_nodes(settings: Settings) -> dict[type[BaseModel], BaseModel]:
        """按类型索引 Settings 的顶层配置节点，供构造器注解命中。

        只认顶层字段、不递归：服务要拿的配置写在哪一眼可见，也避免"某个嵌套
        深处的同类节点被静默注入"。
        注解写 `X | None` 时，只有当前值非 None 才建索引——值为 None 说明这一节
        没配，注入 None 只会把问题推到运行期。
        """
        nodes: dict[type[BaseModel], BaseModel] = {}
        for field_name, field_info in type(settings).model_fields.items():
            # 显式收成 object：model_fields 是运行期字典，取值是 Any，
            # 直接用会让 Any 渗进后面的 isinstance 与 cast。
            annotation: object = field_info.annotation
            node_type = _config_node_type(annotation)
            if node_type is None:
                continue
            value = cast("BaseModel | None", getattr(settings, field_name))
            if value is None:
                continue
            if node_type in nodes:
                raise ServiceContractError(
                    f"配置节点类型 {node_type.__name__} 在 Settings 中出现多次，"
                    + "容器无法确定注入哪个"
                )
            nodes[node_type] = value
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
                        + "它不在 Settings 的顶层节点索引里：请确认该字段已挂在 "
                        + "core/config/settings.py 上，且当前值不是 None"
                        + "（可选字段为 None 时容器无法注入）"
                    )
                plan.nodes[param_name] = annotation
                continue

            shown = annotation.__name__ if isinstance(annotation, type) else repr(annotation)
            raise ServiceContractError(
                f"{prefix} 的参数 {param_name} 注解为 {shown}，" + "容器只支持 Service 与配置节点"
            )

        declared_items = service_type.dependencies
        declared = set(declared_items)
        if len(declared) != len(declared_items):
            raise ServiceContractError(
                f"{service_type.__name__} 的 dependencies 里有重复类型，请去重"
            )
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

    def _resolve_levels(self) -> list[list[type[Service]]]:
        """按 dependencies 分层：同层之间必无依赖，可并发启动。

        层号 = max(依赖层号) + 1；同层内按注册顺序排列，保证结果确定。
        顺带完成环检测与未注册依赖检测。
        """
        depth: dict[type[Service], int] = {}
        visiting: set[type[Service]] = set()

        def visit(service_type: type[Service]) -> int:
            if service_type in depth:
                return depth[service_type]
            if service_type in visiting:
                raise CircularDependencyError(service_type.__name__)
            if service_type not in self._types:
                raise MissingDependencyError(service_type.__name__)

            visiting.add(service_type)
            level = 0
            for dependency in service_type.dependencies:
                level = max(level, visit(dependency) + 1)
            visiting.discard(service_type)
            depth[service_type] = level
            return level

        for service_type in self._types:
            _ = visit(service_type)

        levels: list[list[type[Service]]] = [[] for _ in range(max(depth.values(), default=-1) + 1)]
        for service_type in self._types:  # 注册顺序 → 同层内顺序确定
            levels[depth[service_type]].append(service_type)
        return levels


class ServiceError(Exception):
    """服务相关错误基类。

    不是 AppError，因此不会被转成 4xx：装配期错误只会让 lifespan 启动失败、
    进程退出，这是预期的 fail fast。
    """


class HookTimeoutError(TimeoutError):
    """框架侧超时：服务钩子（start / stop）超过配置秒数被中断。

    刻意继承 TimeoutError：`ServiceStartError.cause` 的类型契约保持不变，调用方
    仍可按 TimeoutError 处理；与业务自己抛的 TimeoutError 靠**类型**区分——只有
    本模块主动抛出的这个子类才代表框架超时。

    不是 ServiceError 子类：它总是被包进 ServiceStartError.cause，不会单独冒泡。
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

    label: str
    cause: BaseException

    def __init__(self, label: str, cause: BaseException) -> None:
        self.label = label
        self.cause = cause
        super().__init__(f"服务 {label} 启动失败：{type(cause).__name__}: {cause}")


class CircularDependencyError(ServiceError):
    """检测到循环依赖。"""

    def __init__(self, service_name: str) -> None:
        super().__init__(f"检测到循环依赖，涉及服务：{service_name}")


class MissingDependencyError(ServiceError):
    """依赖的服务未注册。"""

    def __init__(self, service_name: str) -> None:
        super().__init__(f"依赖的服务未注册：{service_name}")
