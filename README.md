# aliya-cosmos

Python + uv 通用脚手架。三层结构：`service` / `logger` / `config`，开箱可跑，后续往里填业务。

技术栈：Python 3.12+、uv、pydantic-settings、loguru、ruff、pytest。

## 快速开始

```bash
# 安装依赖
uv sync

# 启动服务（Ctrl-C 优雅关闭）
uv run python -m core.main
```

无需任何配置即可启动，所有配置项都有默认值。需要自定义时复制 `.env.example` 为 `.env` 修改。

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
  config/        配置层：pydantic-settings 分组配置
tests/           测试
```

依赖方向单向：`service → (config, logger)`。`service` 层不引用任何框架，可被 CLI、定时任务、测试直接复用。

## 配置

环境变量前缀 `APP_`，嵌套层级用双下划线：

```bash
APP_APP__ENV=prod
APP_LOG__LEVEL=DEBUG
APP_LOG__JSON=true
```

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_APP__APP_NAME` | `aliya-cosmos` | 应用名 |
| `APP_APP__ENV` | `dev` | 环境，只能是 dev / test / prod |
| `APP_APP__DEBUG` | `true` | 调试开关 |
| `APP_LOG__LEVEL` | `INFO` | 日志级别 |
| `APP_LOG__JSON` | `false` | 是否输出 JSON 日志 |
| `APP_LOG__DIR` | `logs` | 日志目录 |
| `APP_LOG__FILE_NAME` | `app.log` | 主日志文件名 |
| `APP_LOG__ERROR_FILE_NAME` | `error.log` | 错误日志文件名 |
| `APP_LOG__ROTATION` | `00:00` | 轮转阈值（默认按天） |
| `APP_LOG__RETENTION` | `7 days` | 保留时长 |

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

颜文字按级别自动选择，也可用 `face=` 指定，常量表在 `core/logger/faces.py`。`APP_LOG__JSON=true` 时改为单行 JSON，字段平铺，便于日志采集。

`context.request_scope` 作用域内的 `request_id` 会自动携带，调用点无需手写字段：

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
    └─ 异常: KeyError
    └─ 堆栈
       Traceback (most recent call last):
         ...
       KeyError: 'item_id'
```

日志文件有两个：`logs/app.log`（跟随 `APP_LOG__LEVEL`）与 `logs/error.log`（固定 ERROR 级），均按天轮转。

## 如何新增一个服务

服务是继承 `Service` 的类，`ServiceManager` 负责装配、启停与健康检查。
依赖按**类型**声明，容器在构造时注入。

1. 在 `core/service/` 新建文件，继承 `Service`：

```python
from typing import ClassVar

from core.service.base import Service


class CacheService(Service):
    # 依赖的服务类型；容器据此排序并在构造时注入
    dependencies: ClassVar[tuple[type[Service], ...]] = (DbService,)

    def __init__(self, db: DbService) -> None:
        super().__init__()  # 必须调用：负责设置 state 与 self.log
        self._db = db

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
| 配置注入 | 需要配置时在构造器声明 `settings: Settings`，容器会注入应用持有的配置 |

启停语义：`start` / `stop` 需幂等；启动按依赖拓扑排序，失败会逆序回滚；关闭严格按启动的逆序，单个服务出错不影响其余服务停下。

装配期的错误都是 `ServiceError` 的子类，只会让启动失败、进程退出（fail fast）。调用 `get()` 查一个未注册的类型属于编程错误，会抛 `ServiceNotRegisteredError`。

| 类型 | 触发条件 |
| --- | --- |
| `ServiceContractError` | 非 `Service` 子类、重复注册、装配后注册、构造器签名不可解释、声明与签名不一致 |
| `ServiceNotRegisteredError` | `get()` 查的类型未注册 |
| `MissingDependencyError` | `dependencies` 里的类型没注册 |
| `CircularDependencyError` | 依赖成环 |
| `ServiceStartError` | 某个服务 `start()` 抛错（携带 `service_name` 与 `cause`） |
