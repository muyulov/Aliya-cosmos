# Aliya-cosmos

Python + uv 通用脚手架。三层结构：`service` / `logger` / `config`，开箱可跑，后续往里填业务。

技术栈：Python 3.12+、uv、pydantic、PyYAML、loguru、openai、httpx2、ruff、pytest。

## 快速开始

```bash
# 安装依赖
uv sync

# 启动基础服务（Ctrl-C 优雅关闭）
uv run python -m core.main

# 启动主 agent（core 4 个服务 + cosmos 12 个服务）
ALIYA_CONTROL_TOKEN=dev uv run python -m agent.cosmos.app

# 另开终端，启动 Aliya（先 cosmos 后 Aliya）
ALIYA_CONTROL_TOKEN=dev uv run python -m agent.aliya.app

# 或者用终端通道直接对话（CLI 即 cosmos）
ALIYA_CONTROL_TOKEN=dev uv run python -m agent.cosmos.cli
```

无需任何配置即可启动，所有配置项都有默认值。要自定义时改 `data/config/cosmos.yaml`；密钥类配置写在 `.env`（参考 `.env.example`）。

## 文档

完整文档已整理为 wiki 站点，源文件在 `docs/wiki/`：

| 我想…… | 入口 |
| --- | --- |
| 把项目跑起来 | [快速开始](docs/wiki/getting-started.md) |
| 看懂进程与组件怎么连 | [架构总览](docs/wiki/architecture/overview.md) |
| 改配置、接自己的模型端点 | [配置](docs/wiki/config.md) |
| 写一个工具给 agent 用 | [工具开发](docs/wiki/development/tools.md) |
| 了解 cosmos 的注册、调度、终端 | [cosmos 定位与组件](docs/wiki/agents/cosmos/overview.md) |
| 了解 Aliya 的人格与主循环 | [Aliya Agent](docs/wiki/agents/aliya.md) |
| 排查问题、看日志 | [日志](docs/wiki/operations/logging.md) |
| 全部章节 | [wiki 首页](docs/wiki/index.md) |

本地预览与构建：

```bash
uv run mkdocs serve            # 热重载预览
uv run mkdocs build --strict   # 校验链接与导航
```

## 目录结构

```text
core/           通用能力层：service / logger / config / llm / embedding / decision / agent
agent/cosmos/   主 agent：契约、常驻服务、总线、装配、进程入口、终端通道
agent/aliya/    Aliya：独立进程、UDS 控制面客户端、回合循环、记忆与自主行动
data/           配置与运行数据（config 可提交，state / Aliya / memory 为运行时产物）
docs/wiki/      文档站点源文件
tests/          测试
mkdocs.yml      文档站点配置
```

依赖方向单向：`agent.aliya → agent.cosmos.contracts → core`，`core` 不认识 `agent`。详见[代码边界](docs/wiki/architecture/boundaries.md)。

## 常用命令

| 目的 | 命令 |
| --- | --- |
| 跑测试 | `uv run pytest` |
| 覆盖率 | `uv run pytest --cov=core --cov=agent` |
| 代码检查 | `uv run ruff check .` |
| 代码格式化 | `uv run ruff format .` |

更多命令见[命令速查](docs/wiki/reference/commands.md)。
