# aliya-cosmos

Python + uv 通用脚手架。三层结构：`service` / `logger` / `config`，开箱可跑，后续往里填业务。

技术栈：Python 3.12+、uv、pydantic、PyYAML、loguru、ruff、pytest。

## 快速开始

```bash
# 安装依赖
uv sync

# 启动服务（Ctrl-C 优雅关闭）
uv run python -m core.main
```

无需任何配置即可启动，所有配置项都有默认值。要自定义时改 `data/config/app.yaml`；密钥类配置写在 `.env`（参考 `.env.example`）。

进程常驻，`SIGINT` / `SIGTERM` 触发优雅关闭。信号注册优先交给事件循环（POSIX 的标准做法）；Windows 的事件循环不实现 `add_signal_handler`，会自动退回 `signal.signal`，并经 `call_soon_threadsafe` 置位。

## 常用命令

| 目的 | 命令 |
| --- | --- |
| 安装依赖 | `uv sync` |
| 启动服务 | `uv run python -m core.main` |
| 跑测试 | `uv run pytest` |
| 覆盖率 | `uv run pytest --cov=core` |
| 代码检查 | `uv run ruff check .` |
| 代码格式化 | `uv run ruff format .` |
| 安装 git 钩子 | `uv run pre-commit install` |

## 目录结构

```text
core/
  main.py        入口：装配服务容器并驱动生命周期
  service/       业务层：服务基类、容器、注册表、具体服务实现
  logger/        日志层：颜文字、结构化上下文、树形/JSON 格式化、sink 装配
  config/        配置层：YAML 骨架加载与占位符插值
data/           配置与运行数据：config/app.yaml 为配置骨架，可安全提交
tests/           测试
```

依赖方向单向：`service → (config, logger)`。`service` 层不引用任何框架，可被 CLI、定时任务、测试直接复用。

## 配置

配置以 `data/config/app.yaml` 为唯一来源（路径相对当前工作目录）。该文件可安全提交，因为它不含任何明文密钥。要调整配置，改这个文件即可。

```yaml
app:
  env: ${APP_ENV:dev}        # 部署时可用环境变量顶掉
log:
  level: INFO
  json: false
```

### 占位符

`${VAR}` 与 `${VAR:默认值}` 两种写法，取值来源是进程环境变量与 `.env`（[进程环境变量优先](https://12factor.net/zh_cn/config)）：

| 写法 | 语义 |
| --- | --- |
| `${DEEPSEEK_API_KEY}` | 取不到值就启动失败，进程退出 |
| `${APP_ENV:dev}` | 取不到值时用 `dev` |

占位符只作用于字符串值，`app.yaml` 里 dict 的键不会被替换；展开后一律是字符串，类型由 pydantic 转换（如 `"true"` → `bool`）。

`.env` 的唯一职责就是给占位符喂值，**不能**直接覆盖配置项。变量名大小写敏感：`${App_Key}` 与 `${APP_KEY}` 是两个不同的名字。

取值表包含**全部**进程环境变量，因此 `${PATH}`、`${HOME}` 这类写法会直接命中系统变量——建议用项目自己的变量名（如 `APP_` 前缀）避免撞车。

### 配置项

| YAML 路径 | 默认值 | 说明 |
| --- | --- | --- |
| `app.app_name` | `aliya-cosmos` | 应用名 |
| `app.env` | `dev` | 环境，只能是 dev / test / prod |
| `app.debug` | `true` | 调试开关 |
| `log.level` | `INFO` | 日志级别 |
| `log.json` | `false` | 是否输出 JSON 日志 |
| `log.dir` | `logs` | 日志目录 |
| `log.file_name` | `app.log` | 主日志文件名 |
| `log.error_file_name` | `error.log` | 错误日志文件名 |
| `log.rotation` | `00:00` | 轮转阈值（默认按天），写进 YAML 时必须加引号 |
| `log.retention` | `7 days` | 保留时长 |
| `log.compression` | `zip` | 归档压缩方式 |
| `service.start_timeout` | `30` | 单个服务 `start()` 的超时秒数；写 `null` 表示不限制 |
| `service.stop_timeout` | `30` | 单个服务 `stop()` 的超时秒数；同上 |
| `clock.tz` | `UTC` | 时钟服务的时区，如 `Asia/Shanghai` |

### 失败行为

配置在进程启动时加载，出错即退出：

| 场景 | 行为 |
| --- | --- |
| `data/config/app.yaml` 不存在 | 全部走默认值继续启动，启动日志显示 `配置源=内置默认值（未找到 …）` |
| 配置文件不是合法 UTF-8，或读不动（权限等） | 报错退出 |
| YAML 语法错误或顶层不是映射 | 报错退出 |
| 占位符取不到值且没写默认值 | 报错退出，并指出是哪个变量 |
| 字段取值非法（如 `env: staging`） | 报错退出 |

启动日志里的 `配置源` 字段会写明本次配置来自哪个文件，路径是相对工作目录解析的，从别处启动时请核对这一项。

## 日志

定位问题最快的方式是看结构化字段，而不是把变量拼进字符串。传进 `log.info` 的额外 kwargs 会自动渲染成树：

```python
from core.logger import log

log.info("用户回合已入队", 参与者="qq:6329133635628374381", 已取消旧计划=0)
```

输出：

```text
2026-08-27 23:09:52 [I] (^_^)/ 用户回合已入队
    ├─ 参与者: qq:6329133635628374381
    └─ 已取消旧计划: 0
```

颜文字按级别自动选择，也可用 `face=` 指定，常量表在 `core/logger/faces.py`。`log.json: true` 时改为单行 JSON，字段平铺，便于日志采集；`time` / `level` / `message` / `face` / `exception` 是保留键，业务字段与它们同名时以保留键为准（树形格式没有这个限制——业务字段独立成行，不与元数据混排）。

`context.request_scope` 作用域内的 `request_id` 会自动携带，调用点无需手写字段；标准库与第三方库（`sqlalchemy` / `httpx` / `asyncio`）经桥接后的日志同样携带这些上下文：

```python
from core.logger import context, log

with context.request_scope():
    log.info("处理订单", 订单号="A001")  # request_id 自动出现在字段里
```

需要临时附加业务维度时用上下文管理器，退出自动还原：

```python
with log.context(会话="qq:123"):
    log.info("会话内")
```

异常堆栈会渲染成独立块，不与字段混淆：

```text
2026-09-12 10:00:00 [E] (x_x) 未捕获异常
    ├─ 阶段: 装配
    ├─ 异常: KeyError
    └─ 堆栈
       Traceback (most recent call last):
         ...
       KeyError: 'item_id'
```

日志文件有两个：`logs/app.log`（跟随 `log.level`）与 `logs/error.log`（固定 ERROR 级），均按天轮转。

## 如何新增一个服务

服务是继承 `Service` 的类，`ServiceManager` 负责装配、启停与健康检查。
依赖按**类型**声明，容器在构造时注入。

1. 在 `core/service/` 新建文件，继承 `Service`：

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

2. 在 `core/service/registry.py` 里注册**类型**（不是实例）：

```python
_ = manager.register(CacheService)
```

约定：

| 约定 | 说明 |
| --- | --- |
| `__init__` 只赋值 | 连接、预热、加载这类动资源的活一律留到 `start()`。装配发生在 lifespan 之前，在 `__init__` 里连资源会让「装配失败」和「启动失败」混成一锅，回滚也会失去意义 |
| 声明与签名对账 | `dependencies` 与构造器参数必须一一对应，容器装配时校验，不一致直接报 `ServiceContractError` |
| 依赖写具体类型 | 写**被注册的那个具体类型**，不能拿抽象基类占位，`get()` 按精确类型查找 |
| `name` 只是标签 | 缺省取类名，不参与依赖解析，也不要求全局唯一，仅用于日志与健康检查展示 |
| 自带日志 | `self.log` 已带 `服务=<label>` 字段与 `[<label>]` 消息前缀，仍可继续 `bind` / `prefix` |
| 统一错误格式 | 报错用 `self.log_error("消息", exc)`，自动拼「错误=类型: 消息」 |
| 配置节点注入 | 需要局部配置时，把配置类定义在 `core/config/settings.py` 并挂成 `Settings` 的**顶层字段**，构造器声明该类型即可（如 `config: CacheSettings`），容器按类型注入。字段允许写成 `X \| None`（PEP 604），但只有当前值不是 `None` 时才建索引——可选字段为 `None` 时容器无法注入，装配期报 `ServiceContractError` |
| 整份配置注入 | 需要全局视野时在构造器声明 `settings: Settings`，容器注入应用持有的那份实例 |
| 超时覆盖 | 默认走 `service.start_timeout` / `service.stop_timeout`；单独调整时写 `start_timeout: ClassVar[float \| Unset \| None] = 300.0`（需 `from core.service.base import Unset`），`None` 表示该服务不限制，不写即跟随全局 |

启停语义：`start` / `stop` 需幂等（重复调用不应报错），并且 **`stop()` 必须能安全作用在「从未成功启动过」的服务上**——启动失败者同样会被回滚调用。启动按依赖**分层**：同层并发、层间串行；单个服务超时或抛错都判该服务失败，并逆序回滚本次动过的服务（**含失败者**：`start()` 可能已经申请了部分资源，`stop()` 是它唯一的回收入口；失败者的状态保持 `FAILED`，清理成功不等于它启动成功过），随后抛出 `ServiceStartError`；**被取消时（外层 `asyncio.timeout`、`task.cancel()`）走同一条回滚路径**——`lifespan()` 的 `__aenter__` 抛错时 `__aexit__` 不会执行、`stop_all` 不会被调用，所以回滚必须在 `start_all` 内部完成，已启动的服务不会留在 `RUNNING`。关闭按启动的逆序**串行**执行，单个服务超时或出错只记日志（状态置 `FAILED`），不影响其余服务停下。

超时靠 `asyncio.timeout` 的取消实现，因此服务的 `start` / `stop` **不要吞掉 `CancelledError`**（写 `except Exception` 是安全的，它不会捕获 `CancelledError`）。

装配期的错误都是 `ServiceError` 的子类，只会让启动失败、进程退出（fail fast）。调用 `get()` 查一个未注册的类型属于编程错误，会抛 `ServiceNotRegisteredError`。

| 类型 | 触发条件 |
| --- | --- |
| `ServiceContractError` | 非 `Service` 子类、重复注册、装配后注册、构造器签名不可解释、声明与签名不一致 |
| `ServiceNotRegisteredError` | `get()` 查的类型未注册 |
| `MissingDependencyError` | `dependencies` 里的类型没注册 |
| `CircularDependencyError` | 依赖成环 |
| `ServiceStartError` | 某个服务 `start()` 抛错（携带 `service_name` 与 `cause`） |
