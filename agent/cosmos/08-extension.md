# 8. 扩展性：新增子 agent

新增一个 agent 需要完成三件事：实现契约、注册清单、配置桌面。cosmos 核心代码不需要修改。本文以新增 `docs` agent 为例，给出完整步骤与检查清单。

## 8.1 能力边界设计

先回答四个问题，再写代码：

| 问题 | docs agent 示例 |
| --- | --- |
| 负责什么 | 生成、更新、校验项目文档 |
| 能力 id | `docs.write`、`docs.validate` |
| 在哪里执行 | `/workspace` 内，只写 `docs/` 与 `agent/` |
| 明确不做什么 | 不执行 shell、不访问网络、不修改源码 |

边界越窄，路由越稳定，审批规则也越容易配置。一个 agent 同时承担编辑、测试、部署时，路由与权限都会变得模糊。

## 8.2 新增步骤

### 步骤 1：定义标识与能力

- `agent_id` 使用小写短横线，注册后不复用，例如 `docs`、`test-runner`。
- 能力 id 使用 `domain.verb`，发布后保持稳定，例如 `docs.write`。
- 版本从 `0.1.0` 开始，能力变化升次版本，契约破坏升主版本。

### 步骤 2：实现 agent 契约

按 [05-contracts.md](05-contracts.md) 实现：

| 能力 | 要求 |
| --- | --- |
| 健康检查 | `GET /health` 返回状态、版本、运行时长 |
| 自述 | `GET /describe` 返回 agent_id、版本、能力、特性、限制 |
| 任务接收 | `POST /v1/tasks` 校验信封并返回 `202` |
| 执行 | `start_timeout_ms` 内上报 `task.started`，按 `heartbeat_ms` 上报心跳 |
| 终态 | 恰好一个 `task.completed` 或 `task.failed` 或 `task.cancelled` |
| 取消 | 宽限期内收敛，重复取消幂等 |
| 输入 | 支持 `task.needs_input` 与输入回答 |
| 审批 | 支持 `task.approval_required` 与审批结果 |
| 工件 | 上传后引用，事件中只放元数据 |
| 查询 | `GET /v1/tasks/{task_id}` 返回任务快照，供事件缺口对账 |

进程内 agent 实现 `SubAgent` 协议即可，事件通过 `EventEmitter` 上报。

### 步骤 3：编写清单

`data/cosmos/agents/docs.yaml`：

```yaml
schema_version: 1
agent_id: docs
display_name: Docs agent
version: 0.1.0
summary: 生成、更新与校验项目文档
description: |
  读取代码与现有文档，生成结构化说明，校验链接与术语一致性。
  只写入 docs/ 与 agent/ 目录，不修改源码。
icon:
  asset: icons/docs.svg
  accent: "#30A46C"
capabilities:
  - id: docs.write
    description: 生成或更新文档
    risk: write
  - id: docs.validate
    description: 校验文档链接、术语与结构
    risk: read
boundaries:
  workspace_only: true
  allowed_paths: ["/workspace/docs", "/workspace/agent"]
  denied_actions: ["shell.exec", "network.fetch", "workspace.delete"]
  max_input_bytes: 131072
  max_artifacts: 16
routing:
  keywords: ["文档", "说明", "README", "注释", "wiki"]
  examples:
    - "给 cosmos 补一份开发文档"
    - "检查文档里的相对链接是否有效"
  embedding_text: "项目文档 生成 更新 校验 README wiki 术语 链接"
transport:
  kind: http
  endpoint: "http://127.0.0.1:8102"
  health_path: /health
  describe_path: /describe
  timeout_ms: 180000
  start_timeout_ms: 10000
  heartbeat_ms: 5000
  cancel_grace_ms: 5000
concurrency:
  mode: serial_write
  max_running: 1
  queue_limit: 10
features: ["cancel", "input_request", "approval", "artifacts"]
permissions:
  scopes: ["workspace.read", "workspace.write"]
  approval_required_for: ["docs.bulk_rewrite"]
desktop:
  group: dev
  order: 30
  pinned: false
  visible: false
  size: 1x1
notifications:
  on_started: false
  on_completed: true
  on_failed: true
  progress_step: 20
```

首次接入时把 `desktop.visible` 设为 `false`，完成契约测试与首次试跑后再显示，避免半成品出现在桌面上。

### 步骤 4：本地契约测试

最低测试集：

| 测试 | 验证点 |
| --- | --- |
| schema | 信封、结果、事件通过 JSON Schema |
| 正常执行 | 事件顺序、心跳、终态、结果结构 |
| 取消 | 取消后宽限期内收敛，重复取消幂等 |
| 输入 | 待输入状态、回答后恢复、过期处理 |
| 审批 | 审批单绑定任务，拒绝与过期路径 |
| 事件重放 | 重复 `event_id` 被去重，乱序按 `seq` 收敛 |
| 事件缺口 | 人为丢事件后，`GET /v1/tasks/{id}` 对账恢复 |
| 大输入 | 超过 256 KB 返回明确错误 |
| 冲突 | 资源冲突返回稳定错误码与 `retryable` |
| 安全 | 越界路径、非法令牌、跨任务事件均被拒绝 |

契约测试应作为 agent 仓库的固定测试，不依赖 cosmos 的人工操作。

### 步骤 5：注册与探测

```bash
# 注册 docs agent
curl -X POST http://127.0.0.1:8080/v1/agents \
  -H "Authorization: Bearer $COSMOS_ADMIN_TOKEN" \
  -H "Content-Type: application/yaml" \
  --data-binary @data/cosmos/agents/docs.yaml

# 探测
curl -X POST http://127.0.0.1:8080/v1/agents/docs/probe \
  -H "Authorization: Bearer $COSMOS_ADMIN_TOKEN"
```

注册成功的三个条件：`lifecycle=active`、探测 `ok`、桌面快照出现卡片（可按 `visible=false` 隐藏）。

### 步骤 6：配置桌面展示

| 配置 | 作用 | 建议 |
| --- | --- | --- |
| `desktop.group` | 分组 id | 归入已有业务分组，避免分组膨胀 |
| `desktop.order` | 组内顺序 | 与同类 agent 保持 10 的间隔，便于插入 |
| `desktop.pinned` | 是否固定 | 高频 agent 固定，其余不固定 |
| `desktop.visible` | 是否显示 | 首次接入先隐藏，验证后显示 |
| `desktop.size` | 卡片尺寸 | 默认 `1x1`，复杂 agent 可用 `2x1` |
| `icon.asset` | 图标资源 | 使用统一风格的 SVG，避免使用文字截图 |
| `icon.accent` | 强调色 | 与分组色协调，保证对比度 |

桌面注册的完整字段见 [03-desktop.md](03-desktop.md)。分组不存在时 cosmos 自动创建并记录 WARNING；上线前应把分组写入 `data/cosmos/desktop.yaml`，避免运行时漂移。

### 步骤 7：权限与审批

- 只申请实际需要的 scope，例如只读 agent 不申请 `workspace.write`。
- 写类与破坏类能力声明风险等级：`read`、`write`、`destructive`、`external`。
- 批量修改、删除、外部网络等动作加入 `approval_required_for`。
- agent 内部必须再次校验路径与权限，不能只依赖 cosmos。
- 审批令牌单次有效，绑定任务与动作，过期按拒绝处理。

### 步骤 8：观测与告警

| 指标 | 用途 |
| --- | --- |
| 心跳延迟 | 判断 agent 存活与网络质量 |
| 任务成功率 | 卡片 24 小时成功率 |
| P95 耗时 | 发现性能退化 |
| 队列深度 | 判断并发是否不足 |
| 错误码分布 | 定位契约或业务问题 |
| 工件大小 | 防止异常大文件拖垮存储 |

日志写入 `core/logger` 上下文，至少包含 `agent_id`、`task_id`、`trace_id`。事件与日志都做脱敏，不记录完整文件内容与密钥。

### 步骤 9：灰度启用

1. 注册时 `visible=false`、`enabled=false`。
2. 用控制面直接调用执行 3 到 5 个真实任务，核对事件与工件。
3. 打开 `visible=true`，保持 `enabled=true`，观察 24 小时。
4. 出现异常时先禁用，保留注册与历史任务，修复后重新探测。
5. 确认稳定后写入清单默认值，纳入版本管理。

## 8.3 桌面新应用配置流程

从注册到出现在桌面，完整链路如下：

```mermaid
flowchart LR
    M["清单 desktop 字段"] --> V["注册校验"]
    V --> P["探测 health / describe"]
    P --> C["生成卡片"]
    C --> G["归入分组 / 排序"]
    G --> S["桌面快照 + agent.registered 事件"]
    S --> A["Aliya 看到新应用"]
```

| 阶段 | 检查点 |
| --- | --- |
| 清单 | `desktop.group`、`order`、`visible`、`size`、`icon` 完整 |
| 校验 | 分组存在或允许创建，图标资源存在，尺寸合法 |
| 探测 | `agent_id`、版本、能力与清单一致 |
| 卡片 | `status=idle`、`enabled=true`、`visible=true` |
| 布局 | 不与已有卡片顺序冲突，固定项不过多 |
| 通知 | 注册事件推送成功，Aliya 收到 `agent.registered` |

新增 agent 不修改 cosmos 代码，也不重启 cosmos。运行时注册即可生效；写入 `data/cosmos/agents/` 的清单在下次启动时自动加载。

## 8.4 常见问题

| 问题 | 后果 | 处理 |
| --- | --- | --- |
| 能力 id 频繁改名 | 路由失效、历史任务无法解释 | 能力 id 视为稳定 API，改名走新能力 |
| 心跳与进度混用 | 长时间无进度被误判离线 | 心跳独立上报，进度可不变 |
| 取消不收敛 | 任务悬挂、资源泄漏 | 把长任务切成可中断步骤，设置宽限期 |
| 事件无 `seq` | 乱序、重复、桌面抖动 | SDK 统一分配 `seq` 与 `event_id` |
| 终态重复 | 任务状态被覆盖 | 每个任务只允许一个终态，重复丢弃 |
| 错误信息带堆栈 | 泄露内部实现 | `message` 面向 Aliya，堆栈只进日志 |
| 工件不走上传 | 事件过大、存储失控 | 大内容上传后引用，事件只放元数据 |
| 权限申请过宽 | 越权风险 | 最小 scope，写入路径白名单 |
| 通知风暴 | Aliya 被淹没 | 按 `dedupe_key` 去重，进度节流 |
| 桌面分组膨胀 | 桌面难扫视 | 复用已有分组，控制在 5 到 7 个 |
| 版本不兼容 | 任务失败、探测失败 | 主版本不兼容直接拒绝，次版本向前兼容 |
| 直接回调 Aliya | 绕过 cosmos 状态与审计 | 所有状态只回 cosmos |

## 8.5 上线检查清单

- [ ] 能力 id 与边界已评审，命名稳定。
- [ ] `/health` 与 `/describe` 可用，返回版本与能力。
- [ ] 信封、结果、事件通过 schema 校验。
- [ ] `task.started` 在启动超时内到达。
- [ ] 心跳独立于进度，间隔符合清单。
- [ ] 每个任务恰好一个终态事件。
- [ ] 取消、输入、审批、工件四条路径均已测试。
- [ ] 重复与乱序事件可收敛，缺口可对账。
- [ ] 错误码稳定，`retryable` 语义准确。
- [ ] 路径白名单与权限校验在 agent 内部生效。
- [ ] 事件与日志已脱敏，不含密钥与完整文件内容。
- [ ] 桌面分组、顺序、图标、可见性已配置。
- [ ] 首次接入先隐藏，试跑通过后再显示。
- [ ] 指标与告警接入，离线与失败可被发现。
- [ ] 禁用与下线流程演练过，历史任务保留。

全部勾选后，agent 可以进入 `active` 并在桌面展示。后续扩展只需重复本文流程，cosmos 的调度、桌面与通知能力保持不变。
