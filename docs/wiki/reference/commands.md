# 命令速查

## 运行

| 目的 | 命令 |
| --- | --- |
| 安装依赖 | `uv sync` |
| 启动基础服务 | `uv run python -m core.main` |
| 启动主 agent | `ALIYA_CONTROL_TOKEN=dev uv run python -m agent.cosmos.app` |
| 终端通道对话 | `ALIYA_CONTROL_TOKEN=dev uv run python -m agent.cosmos.cli` |
| 启动 Aliya | `ALIYA_CONTROL_TOKEN=dev uv run python -m agent.aliya.app` |
| 浏览器终端 | 启动 cosmos 后打开 `http://127.0.0.1:8780/` |

`agent.cosmos.cli` 与 `agent.cosmos.app` 二选一，都占用同一个用户出口与控制面 socket。

## 质量

| 目的 | 命令 |
| --- | --- |
| 跑测试 | `uv run pytest` |
| 覆盖率 | `uv run pytest --cov=core --cov=agent` |
| 代码检查 | `uv run ruff check .` |
| 代码格式化 | `uv run ruff format .` |
| 格式检查 | `uv run ruff format --check .` |

## 文档站点

| 目的 | 命令 |
| --- | --- |
| 本地预览（热重载） | `uv run mkdocs serve` |
| 构建 | `uv run mkdocs build` |
| 严格构建（链接与导航校验） | `uv run mkdocs build --strict` |

## 本地依赖数据库

| 目的 | 命令 |
| --- | --- |
| 启动 | `docker compose -f docker/docker-compose.yml up -d` |
| 停止 | `docker compose -f docker/docker-compose.yml down` |

!!! note
    这套编排供记忆层使用，当前 `core/memory` 尚未实现，主流程不依赖它。详见[部署](../operations/deploy.md#3-本地依赖数据库)。

## 世界推理校准

```bash
python -m agent.cosmos.world.calibration \
  --trace data/state/world_trace.jsonl \
  --out data/state/world_calibration.json \
  --model-out data/state/world_trigger_model.json
```
