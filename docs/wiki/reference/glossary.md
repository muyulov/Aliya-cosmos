# 术语表

## 项目结构

| 术语 | 含义 |
| --- | --- |
| `core` | 通用能力层：service / logger / config / llm / embedding / decision / agent |
| `agent.cosmos` | 主 agent 的编排实现：通信终端、调度中枢、状态感知 |
| `agent.aliya` | Aliya 的实现：对话主体与控制入口，独立进程 |
| `core.agent` | 工具 / Skill / MCP 的**零件库**，不含 ReAct 循环 |
| 子 agent | 注册到 cosmos 桌面的执行单元，如 Edit agent；不是 LLM 的 tool |
| 世界推理 agent | Aliya 的后台参谋，持续推演世界状态，不占桌面、不触发调度 |

## cosmos 概念

| 术语 | 含义 |
| --- | --- |
| AgentCard | 子 agent 的注册元数据 + 桌面展示元数据 |
| Agent 桌面 | cosmos 向 Aliya 呈现的子 agent 列表，形态类似手机桌面 APP |
| 可解读状态 | 由原始字段派生的自然语言表述（如「你在线，5 分钟没说话了」） |
| 调度上下文 | 与子 agent 任务相关的状态（task_id、进度、错误），不给用户看 |
| 对话上下文 | 与用户聊天相关的历史与摘要，给 Aliya 用 |
| 主动消息 | 非用户触发的消息（Aliya 主动搭话、任务完成通知、状态变化提示） |
| 控制面 | Aliya 调用 cosmos 能力的接口，生产环境走 UDS + NDJSON |
| 链路状态 | `up` / `unstable` / `down`；人设中对应「曲率航行期间通讯中断」 |
| supersede | 新任务取代同目标的旧任务，旧任务被取消 |

## 能力层

| 术语 | 含义 |
| --- | --- |
| Tool | `@tool` 装饰后的工具对象，含名称、描述、JSON Schema 与调用入口 |
| ToolRegistry | 显式工具注册表，负责注册、声明导出与执行回填 |
| Skill | `data/skills/<name>/SKILL.md` 描述的技能，按需披露正文 |
| 渐进式披露 | 系统提示只放名称与一句话描述，正文由 `load_skill` 按需读取 |
| MCP | Model Context Protocol，外部工具 server 的接入协议 |
| ReAct 循环 | 「模型思考 → 调工具 → 回填 → 再思考」的循环，由调用方实现 |
| 端点（endpoint） | 一个模型服务的地址 + 密钥 + 采样参数组合 |
| 结构化输出 | 让模型按给定 schema 返回，失败抛 `LLMSchemaError` |

## 运维

| 术语 | 含义 |
| --- | --- |
| 优雅关闭 | 收到 `SIGINT` / `SIGTERM` 后按启动逆序停止服务 |
| 回滚 | 启动失败时逆序停止本次已启动的服务（含失败者） |
| fail fast | 配置或编程错误在启动期直接抛错退出 |
| 死信（dead letter） | 出站投递重试耗尽后落盘的消息 |
