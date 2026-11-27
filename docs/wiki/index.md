# Aliya-cosmos


技术栈：Python 3.12+、uv、pydantic、PyYAML、loguru、openai、httpx2、ruff、pytest。

## 按目标找入口A

| 我想…… | 从这里开始 |
| --- | --- |
| 把项目跑起来 | [快速开始](getting-started.md) |
| 看懂进程与组件怎么连 | [架构总览](architecture/overview.md) → [cosmos 通信拓扑](architecture/cosmos-topology.md) |
| 改配置、接自己的模型端点 | [配置](config.md) → [能力层](capabilities/llm.md) |
| 写一个工具给 agent 用 | [工具开发](development/tools.md) |
| 了解 cosmos 的注册、调度、终端 | [cosmos 定位与组件](agents/cosmos/overview.md) |
| 了解 Aliya 的人格与主循环 | [Aliya Agent](agents/aliya.md) |
| 排查问题、看日志 | [日志](operations/logging.md) |

## 章节地图

| 章节 | 内容 |
| --- | --- |
| [快速开始](getting-started.md) | 安装、启动、常用命令、目录结构 |
| [架构](architecture/overview.md) | 进程拓扑、消息流向、代码边界 |
| [配置](config.md) | YAML 骨架、占位符、全部配置项、失败行为 |
| [能力层](capabilities/llm.md) | LLM / 向量 / 判断 / Agent 工具四层的接口与约定 |
| [Agent](agents/index.md) | cosmos 的 11 项规范与 Aliya 的实现边界 |
| [开发指南](development/index.md) | 工具、Skill、MCP、ReAct、测试门禁 |
| [运维](operations/logging.md) | 日志、部署形态、隐私边界 |
| [参考](reference/glossary.md) | 术语表、数据目录、命令速查 |

## 文档约定

- 本 wiki 是项目现行文档的唯一信息源。原先分散在 `README.md`、`docs/agent-development.md`、`agent/**/README.md`、`agent/cosmos/docs/*` 的内容已合并至此，原文件已删除，历史可从 git 追溯。
- `docs/plans/` 下的设计与实施计划属于历史归档，不进入本站点；其中已落地的结论已并入对应章节。
- 站点构建：`uv run mkdocs serve` 本地预览，`uv run mkdocs build --strict` 校验（含内部链接与导航完整性）。
