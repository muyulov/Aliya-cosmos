# Python + uv 通用脚手架 设计与开发计划

日期：2026-09-12
状态：设计已确认，待实施

## 一、目标与范围

产出一个不绑定具体业务、可长期复用的 Python 项目脚手架。技术栈 Python + uv，Web 框架 FastAPI，配置用 pydantic-settings，日志用 loguru 并带自定义树形格式化，工程化配置按团队仓库标准配齐（ruff、pytest、pre-commit、GitHub Actions）。不引入 Makefile 与 Docker，命令统一走 `uv run`。

脚手架自带一个最小但完整的示例链路：健康检查 + 内存仓储的 CRUD，用来演示「api → service → config/logger」的依赖方向与错误传递方式。后续填业务时照抄示例结构即可。

非目标（本次不做）：数据库与 ORM、用户鉴权、迁移工具、异步任务队列、多租户。这些留作扩展位，不预先引入依赖。

## 二、架构决策

整体采用「瘦 API + 胖 Service + 显式依赖注入」。单向依赖：`api → service → (config, logger)`，`service` 绝不反向依赖 `api`，`config` 与 `logger` 是横切模块，谁都能用但互不依赖。这样 service 层能脱离 Web 被 CLI、测试、定时任务复用。

被否决的两个替代方案：端口与适配器（需新增 `core/model`、`core/repository` 两个目录，超出给定骨架且当前无数据库需求，属于过度设计）；快速单体（一个 `app.py` 写完全部路由，业务一多就退化，与脚手架长期复用的目标冲突）。

## 三、目录结构

```text
.
├── core/
│   ├── __init__.py
│   ├── main.py                 # 入口：app = create_app() + uvicorn 启动
│   ├── api/
│   │   ├── __init__.py
│   │   ├── app.py              # create_app() 应用工厂
│   │   ├── deps.py             # Depends 依赖提供者
│   │   ├── errors.py           # AppError 定义与全局异常处理器
│   │   ├── middleware.py       # request-id / 上下文绑定 / access 日志
│   │   ├── response.py         # 统一响应信封
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── router.py       # v1 路由聚合
│   │       ├── health.py       # 健康检查（聚合各服务状态）
│   │       └── items.py        # 示例 CRUD
│   ├── service/
│   │   ├── __init__.py
│   │   ├── base.py             # Service 抽象基类 + 状态机
│   │   ├── manager.py          # ServiceManager：注册/启停/健康检查
│   │   ├── registry.py         # 全局默认 ServiceManager 实例
│   │   └── item_service.py     # 示例服务（内存仓储）
│   ├── logger/
│   │   ├── __init__.py         # 对外导出 log
│   │   ├── faces.py            # 颜文字常量表
│   │   ├── formatters.py       # 树形 / JSON 格式化
│   │   └── setup.py            # sink 装配、轮转、stdlib 桥接
│   └── config/
│       ├── __init__.py
│       └── settings.py         # pydantic-settings 分组配置
├── tests/
│   ├── conftest.py
│   ├── test_health.py
│   ├── test_items.py
│   ├── test_logger.py
│   └── test_service_manager.py
├── .github/workflows/ci.yml
├── .pre-commit-config.yaml
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
```

## 四、config 模块

`pydantic-settings` 的 `BaseSettings`，`env_file=".env"`，全局前缀 `APP_`，嵌套分隔符 `__`。

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="APP_", env_nested_delimiter="__", extra="ignore"
    )
    app: AppSettings  # app_name, env(dev/test/prod), debug, host, port
    log: LogSettings  # level, json, dir, rotation, retention
```

`env` 用 `Literal["dev", "test", "prod"]` 约束，写错在启动时即失败，而不是运行到一半踩坑。单例通过 `@lru_cache` 的 `get_settings()` 暴露，测试里用 `get_settings.cache_clear()` 或依赖覆盖替换。仓库提交 `.env.example`，`.env` 进 `.gitignore`。所有字段给合理默认值，保证克隆后不配任何东西就能跑。

后续需要数据库、Redis、第三方密钥时，加一个子模型即可，不改结构。

## 五、logger 模块

底层用 loguru 负责输出与轮转，其上做一层薄封装，把「树形字段」变成 API 而不是手拼字符串。

调用方式：

```python
from core.logger import log

log.info("用户回合已入队", face=log.face.CHEER, 参与者="qq:6329133635628374381", 已取消旧计划=0)
```

渲染结果：

```text
2026-08-27 23:09:52 [I] (^_^)/ 用户回合已入队
    ├─ 参与者: qq:6329133635628374381
    └─ 已取消旧计划: 0
```

实现要点：字段以 `**kwargs` 进入 `record["extra"]["fields"]`，Python 3.7+ 的有序 dict 保证声明顺序；自定义 sink 对最后一个字段用 `└─`、其余用 `├─`，统一缩进 4 空格；有字段时消息与字段块换行拼接，无字段时保持单行。级别标记映射为 `[D] [I] [W] [E] [C]` 并对齐到固定宽度。颜文字由 `faces.py` 提供常量表，按级别给默认值，允许每次调用用 `face=` 覆盖，位置在消息文本之前。

颜色与落盘：控制台按级别着色（仅 TTY 生效），文件 sink 去除 ANSI 码。路径 `logs/app.log`（`LOG_DIR` 可覆盖），`rotation="10 MB"`、`retention="7 days"`、`compression="zip"`。标准库日志（uvicorn、SQLAlchemy 等）通过 `InterceptHandler` 桥接进 loguru，保证全项目格式统一。`LOG_JSON=true` 时切换为单行 JSON，字段平铺进 JSON 而不渲染树形，便于日志采集。

`logs/` 落在项目根并写入 `.gitignore`。

## 六、service 层与生命周期管理

`core/service/base.py`：

```python
class ServiceState(StrEnum):
    CREATED / STARTING / RUNNING / STOPPING / STOPPED / FAILED


class Service(ABC):
    name: str
    dependencies: ClassVar[Sequence[str]] = ()
    state: ServiceState

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def health(self) -> HealthStatus: ...
```

`core/service/manager.py` 的 `ServiceManager` 提供 `register(service)`、`get(cls)`、`start_all()`、`stop_all()`、`lifespan()`。启动按 `dependencies` 做拓扑排序并逐条记录服务名与耗时；任一服务启动失败则对已启动服务逆序回滚，整体抛出带原因的异常；关闭严格逆序、逐个吞掉异常保证全部停下。`start`/`stop` 幂等，重复调用不报错。检测到循环依赖直接报错。

与 Web 的衔接：manager 挂到 `app.state.services`，lifespan 内 `await manager.start_all()` → `yield` → `await manager.stop_all()`；`deps.py` 通过 `request.app.state.services.get(ItemService)` 取实例。`registry.py` 另提供全局默认实例，供 CLI 与脚本场景使用。

`/health` 聚合各服务 `health()`，逐个返回服务名与状态，使「进程在但依赖没就绪」可被健康检查反映，而不是只回一个静态 `ok`。

`ItemService` 作为示例实现：`start()` 预热内存仓储，`stop()` 清空，演示完整生命周期。

## 七、api 层

`create_app()` 放在 `core/api/app.py`，`core/main.py` 只保留 `app = create_app()` 与 uvicorn 启动，便于测试直接构造实例。装配顺序：lifespan（启动/关闭各打一条 I 级日志）、`RequestContextMiddleware`、`/api/v1` 路由、异常处理器。

中间件职责：为每个请求生成 `request_id` 并写入响应头，bind 进 loguru 上下文使该请求后续日志自动携带，请求结束打一条含方法、路径、状态码、耗时的 access 日志。

错误分两类。`AppError(code, message, status_code)` 为业务异常基类，由 handler 转成统一信封 `{code, message, data, request_id}`；未捕获异常走兜底 handler，记 E 级日志并带 traceback，对外只暴露 500 与 `request_id`，不泄漏堆栈。

示例 CRUD 覆盖列表、详情、创建、删除四条路由；「详情不存在」由 service 抛 `ItemNotFoundError`，api 层映射为 404，演示跨层错误传递。

## 八、工程化配置

`pyproject.toml`：`requires-python = ">=3.12"`；运行时依赖 fastapi、uvicorn[standard]、pydantic-settings、loguru；dev 组含 pytest、pytest-asyncio、httpx、ruff、pre-commit。ruff 行宽 100，启用 `E/F/I/UP/B/SIM/RUF` 规则集，忽略与颜文字/中文排版无关的项。pytest 设 `asyncio_mode = "auto"`。

不引入 Makefile：命令统一用 `uv run`，常用命令在 README 中以表格列出，避免多一层间接封装，也免去 Windows 环境下的兼容问题。不引入 Dockerfile：部署形态由使用方决定，脚手架不预设容器化；如后续需要，按 `uv sync --frozen --no-dev` 产出 `.venv` 再拷贝即可。

常用命令：

| 目的 | 命令 |
| --- | --- |
| 安装依赖 | `uv sync` |
| 启动服务 | `uv run python -m core.main` |
| 开发热重载 | `uv run uvicorn core.main:app --reload` |
| 跑测试 | `uv run pytest` |
| 覆盖率 | `uv run pytest --cov=core` |
| 检查 | `uv run ruff check .` |
| 格式化 | `uv run ruff format .` |

`pre-commit`：ruff check、ruff format、trailing-whitespace、end-of-file-fixer、check-yaml、check-merge-conflict、check-added-large-files。

`.github/workflows/ci.yml`：push 与 PR 触发，Python 3.12 单版本，astral-sh/setup-uv 带缓存，依次执行 `uv sync --all-extras --dev`、`ruff check`、`ruff format --check`、`pytest`。

## 九、测试策略

`conftest.py` 提供两个 fixture：用 `create_app()` 构造的应用实例、基于 httpx `ASGITransport` 的异步客户端，并覆盖 `get_settings` 依赖以隔离环境。测试分四块：健康检查与聚合状态；示例 CRUD 的 happy path 与 404 分支；日志格式化（无字段单行、有字段树形、末项 `└─`、JSON 模式）；ServiceManager 的拓扑启动、失败回滚、逆序关闭、循环依赖检测。

不追求覆盖率数字，测的是「脚手架的约定是否成立」，例如 service 层不 import fastapi 这条由一条静态断言守住。

## 十、实施阶段

| 阶段 | 内容 | 验收标准 |
| --- | --- | --- |
| P0 | `uv init`、pyproject、目录骨架、`.gitignore`、`.env.example` | `uv sync` 成功，目录齐备 |
| P1 | config 模块 | 不配 `.env` 能启动，`APP_ENV` 非法值报错 |
| P2 | logger 模块（faces、formatters、setup） | 输出与参考样式逐字符一致 |
| P3 | service 层（base、manager、registry、item_service） | 拓扑启动、失败回滚、逆序关闭单测通过 |
| P4 | api 层（app、middleware、errors、response、deps） | `uv run python -m core.main` 起服务，健康检查返回 200 |
| P5 | v1 路由与示例 CRUD | 四条路由可用，404 走统一信封 |
| P6 | tests | `uv run pytest` 全绿 |
| P7 | ruff、pre-commit、CI | `uv run ruff check .` 与 `uv run pytest` 均通过 |
| P8 | README（快速开始、常用命令、目录说明、如何加新服务） | 照 README 从零跑通 |

## 十一、扩展位

后续接入能力时的落点：数据库在 `core/config/settings.py` 加子模型、在 `core/service/` 加一个实现 `Service` 的连接池服务并声明依赖；鉴权放 `core/api/deps.py`；异步任务作为另一 `Service` 注册进 manager；多环境配置靠 `APP_ENV` 切换不同的 `.env` 文件。
