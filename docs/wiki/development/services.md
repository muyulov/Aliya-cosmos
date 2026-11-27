# 新增一个服务

服务是继承 `Service` 的类，`ServiceManager` 负责装配、启停与健康检查。依赖按**类型**声明，容器在构造时注入。

## 1. 定义服务

在 `core/service/` 新建文件，继承 `Service`：

```python
from typing import ClassVar

from core.config import CacheSettings
from core.service.base import Service


class CacheService(Service):
    # 依赖的服务类型；容器据此排序并在构造时注入
    dependencies: ClassVar[tuple[type[Service], ...]] = (DbService,)

    def __init__(self, db: DbService, config: CacheSettings) -> None:
        super().__init__()  # 必须调用：负责设置 state 与 self.log
        self._db = db
        self._config = config

    async def start(self) -> None: ...  # 建连接
    async def stop(self) -> None: ...  # 关连接
```

## 2. 注册类型

在 `core/service/registry.py` 里注册**类型**（不是实例）：

```python
manager.register(CacheService)  # 支持一次传入多个类型：register(A, B, C)，顺序即同层内的启动先后
```

## 3. 约定

| 约定 | 说明 |
| --- | --- |
| `__init__` 只赋值 | 连接、预热、加载这类动资源的活一律留到 `start()`。装配发生在 lifespan 之前，在 `__init__` 里连资源会让「装配失败」和「启动失败」混成一锅，回滚也会失去意义 |
| 声明与签名对账 | `dependencies` 与构造器参数必须一一对应，容器装配时校验，不一致直接报 `ServiceContractError` |
| 依赖写具体类型 | 写**被注册的那个具体类型**，不能拿抽象基类占位，`get()` 按精确类型查找 |
| `name` 只是标签 | 缺省取类名，不参与依赖解析，也不要求全局唯一，仅用于日志与健康检查展示 |
| 自带日志 | `self.log` 的消息已带 `[<label>]` 前缀（服务名不再重复进结构化字段），仍可继续 `bind` / `prefix` |
| 统一错误格式 | 报错用 `self.log_error("消息", exc)`，自动拆出「错误类型 / 错误消息」两个结构化字段，便于采集端按类型聚合 |
| 配置节点注入 | 需要局部配置时，把配置类定义在 `core/config/settings.py` 并挂成 `Settings` 的**顶层字段**，构造器声明该类型即可（如 `config: CacheSettings`），容器按类型注入。字段允许写成 `X \| None`（PEP 604），但只有当前值不是 `None` 时才建索引——可选字段为 `None` 时容器无法注入，装配期报 `ServiceContractError` |
| 整份配置注入 | 需要全局视野时在构造器声明 `settings: Settings`，容器注入应用持有的那份实例 |
| 超时覆盖 | 默认走 `service.start_timeout` / `service.stop_timeout`；单独调整时写 `start_timeout: ClassVar[float \| Unset \| None] = 300.0`（需 `from core.service.base import Unset`），`None` 表示该服务不限制，不写即跟随全局 |
| 判「在跑」用 `running` | `Service.running` 是 `state is ServiceState.RUNNING` 的统一出口，调用点不要再散写状态比较，改判定规则时才不必全仓搜 |
| 健康检查可附带信息 | `health()` 返回的 `HealthStatus.extra` 会作为字段带进启动时的「服务健康」日志（内置服务用它报身份：clock 报 `时区`，embedding 报 `模型` / `端点`，llm 报 `对话模型` / `对话端点` / `多模态模型` / `多模态端点`，decision 报 `jev模型` / `jev端点` / `laya模型` / `laya端点`）。与保留名同名的一律让位：`name` / `healthy` / `state` / `detail`，以及日志侧的 `服务` / `健康` / `状态` / `详情` |

## 4. 启停语义

`start` / `stop` 需幂等（重复调用不应报错），并且 **`stop()` 必须能安全作用在「从未成功启动过」的服务上**——启动失败者同样会被回滚调用。

启动按依赖**分层**：同层并发、层间串行；单个服务超时或抛错都判该服务失败，并逆序回滚本次动过的服务（**含失败者**：`start()` 可能已经申请了部分资源，`stop()` 是它唯一的回收入口；失败者的状态保持 `FAILED`，清理成功不等于它启动成功过；**已 RUNNING、被本次跳过启动的服务也在回滚名单里**——整体启动失败意味着进程即将退出，而那时 `stop_all` 不会被调用），随后抛出 `ServiceStartError`。

**被取消时**（外层 `asyncio.timeout`、`task.cancel()`）走同一条回滚路径——`lifespan()` 的 `__aenter__` 抛错时 `__aexit__` 不会执行、`stop_all` 不会被调用，所以回滚必须在 `start_all` 内部完成，已启动的服务不会留在 `RUNNING`。

关闭按启动的逆序**串行**执行，单个服务超时或出错只记日志（状态置 `FAILED`），不影响其余服务停下。

超时靠「定时取消 + 标志位」实现（不用 `asyncio.timeout`，好与业务自己抛的 `TimeoutError` 区分开），因此服务的 `start` / `stop` **不要吞掉 `CancelledError`**（写 `except Exception` 是安全的，它不会捕获 `CancelledError`）。框架侧超时抛 `HookTimeoutError`（`TimeoutError` 的**子类**），它会作为 `ServiceStartError.cause` 出现；业务自己抛的 `TimeoutError` 原样保留，记为「启动失败 / 关闭异常」。

## 5. 错误

装配期的错误都是 `ServiceError` 的子类，只会让启动失败、进程退出（fail fast）。调用 `get()` 查一个未注册的类型属于编程错误，会抛 `ServiceNotRegisteredError`。

| 类型 | 触发条件 |
| --- | --- |
| `ServiceContractError` | 非 `Service` 子类、重复注册（含同一次批量注册内重复）、装配后注册、构造器签名不可解释、声明与签名不一致 |
| `ServiceNotRegisteredError` | `get()` 查的类型未注册 |
| `MissingDependencyError` | `dependencies` 里的类型没注册 |
| `CircularDependencyError` | 依赖成环 |
| `ServiceStartError` | 某个服务 `start()` 抛错（携带 `label` 与 `cause`；超时场景的 `cause` 是 `HookTimeoutError`） |
