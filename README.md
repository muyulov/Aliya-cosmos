# aliya-cosmos

Python + uv 通用脚手架。四层结构：`api` / `service` / `logger` / `config`，开箱可跑，后续往里填业务。

技术栈：Python 3.12+、uv、FastAPI、pydantic-settings、loguru、ruff、pytest。

## 快速开始

```bash
# 安装依赖
uv sync

# 启动服务
uv run python -m core.main

# 访问接口文档
# http://127.0.0.1:8000/docs
```

无需任何配置即可启动，所有配置项都有默认值。需要自定义时复制 `.env.example` 为 `.env` 修改。

## 常用命令

| 目的 | 命令 |
| --- | --- |
| 安装依赖 | `uv sync` |
| 启动服务 | `uv run python -m core.main` |
| 开发热重载 | `uv run uvicorn core.main:app --reload` |
| 跑测试 | `uv run pytest` |
| 覆盖率 | `uv run pytest --cov=core` |
| 代码检查 | `uv run ruff check .` |
| 代码格式化 | `uv run ruff format .` |
| 安装 git 钩子 | `uv run pre-commit install` |

## 目录结构

```text
core/
  main.py        入口，仅做 uvicorn 启动
  api/           HTTP 层：应用工厂、路由、中间件、异常处理、依赖注入
  service/       业务层：服务基类、生命周期管理器、具体服务实现
  logger/        日志层：颜文字、树形/JSON 格式化、sink 装配
  config/        配置层：pydantic-settings 分组配置
tests/           测试
```

依赖方向单向：`api → service → (config, logger)`。`service` 层不引用 FastAPI，可被 CLI、定时任务、测试直接复用。

## 配置

环境变量前缀 `APP_`，嵌套层级用双下划线：

```bash
APP_APP__ENV=prod
APP_APP__PORT=9000
APP_LOG__LEVEL=DEBUG
APP_LOG__JSON=true
```

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_APP__APP_NAME` | `aliya-cosmos` | 应用名 |
| `APP_APP__ENV` | `dev` | 环境，只能是 dev / test / prod |
| `APP_APP__DEBUG` | `true` | 调试开关 |
| `APP_APP__HOST` | `0.0.0.0` | 监听地址 |
| `APP_APP__PORT` | `8000` | 监听端口 |
| `APP_LOG__LEVEL` | `INFO` | 日志级别 |
| `APP_LOG__JSON` | `false` | 是否输出 JSON 日志 |
| `APP_LOG__DIR` | `logs` | 日志目录 |
| `APP_LOG__ROTATION` | `10 MB` | 轮转阈值 |
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

## 如何新增一个服务

服务就是实现了 `Service` 基类的类，`ServiceManager` 负责启停与健康检查。

1. 在 `core/service/` 新建文件，继承 `Service`：

```python
from core.service.base import HealthStatus, Service


class CacheService(Service):
    name = "cache"
    dependencies = ()  # 依赖的其他服务 name

    async def start(self) -> None: ...  # 建连接

    async def stop(self) -> None: ...  # 关连接
```

2. 在 `core/service/registry.py` 里注册，它会随应用启动自动启停。

`start` / `stop` 需幂等。启动按依赖拓扑排序，失败会逆序回滚；关闭严格逆序，单个服务出错不影响其余服务停下。

## 如何新增一组路由

在 `core/api/v1/` 下新建文件，用 `APIRouter` 定义路由，然后在 `core/api/v1/router.py` 里 `include_router`。业务逻辑放 `service` 层，路由只做参数校验与响应拼装。

抛业务异常用 `core/api/errors.py` 里的 `AppError` 子类，会被全局处理器转成统一信封 `{code, message, data, request_id}`，无需手写 try/except。

## 示例接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 健康检查，聚合各服务状态 |
| GET | `/api/v1/items` | 列表 |
| GET | `/api/v1/items/{item_id}` | 详情，不存在返回 404 |
| POST | `/api/v1/items` | 创建 |
| DELETE | `/api/v1/items/{item_id}` | 删除 |

示例数据存内存，重启即清空，仅用于演示链路。
