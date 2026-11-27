# 隐私边界

隐私约束贯穿用户状态采集、Aliya 的表述与本地运行时数据。原则是：**没有采集就没有泄露**。

## 1. 采集范围白名单

| 允许 | 禁止 |
| --- | --- |
| 消息时间戳、条数 | 消息内容本身（已在对话上下文里，不重复入状态） |
| 在线 / 离开 / 离线 | 精确地理位置、IP 归属地 |
| 是否正在输入 | 输入了什么、删改了几次 |
| 消息语气派生的情绪倾向 | 通讯录、社交关系、第三方账号 |
| 时区与设备类型 | 剪贴板、屏幕内容、进程列表、其他应用使用情况 |
| 是否有未完成任务 | 任务内容以外的工作习惯画像 |

禁止项不做「以后可能有用」的预留。

## 2. 保留期

| 数据 | 保留 | 存储 |
| --- | --- | --- |
| 原始快照 | 24 小时 | 内存环形缓冲 |
| 聚合指标（日均活跃时段等） | 30 天 | 本地 JSON / SQLite |
| 情绪数值 | 不落盘 | 内存，进程退出即消失 |
| 日志 | 不记录任何状态值 | —— |

## 3. 谁能看

| 主体 | 权限 |
| --- | --- |
| Aliya | 看 `PresenceSnapshot`（情绪默认不主动呈现，可要求关闭任一字段） |
| 世界推理 agent | 看 `WorldInput.presence`（字段子集） |
| 子 agent | **默认看不到**；需 `permissions.read_presence=true` 且只给 `presence_fields` 白名单 |
| 用户 | 可在设置里查看「采集了什么」并逐项关闭 |
| 日志 / 外部系统 | 看不到 |

子 agent 拿到用户状态的风险很高（编辑 agent 为什么要知道用户情绪？），所以默认全拒，且即使打开也只给白名单字段的**原始值**，不给 `readable`（避免下游二次解读）。

## 4. 永不回显

以下行为一律禁止：

| 禁止 | 原因 |
| --- | --- |
| 在给用户的消息里出现「我注意到你最近…」式的状态引用 | 监视感 |
| 把 `readable` 原文透给用户 | 内部表述是给 Aliya 的工作语言 |
| 用状态数据推断用户身份、职业、健康状况 | 越界画像 |
| 在错误信息、日志、任务结果里附带状态 | 泄露面扩大 |

Aliya 可以在行为上体现感知（比如用户明显疲惫时少说两句），但不能把感知说破。

## 5. 对等隐私

Aliya 自己要求关掉心率监测（HRM），理由是「哪怕执行任务时被讨论心跳都有被窥探的感觉」。因此 cosmos 给她的用户状态**不能是监控面板**，只能是通信中自然可见的线索；她可以要求关掉任何一项。

**一个要求关掉自己心率的人，不会心安理得地去看别人的情绪曲线。** 具体呈现规则见[用户状态线索](../agents/cosmos/presence.md)，人设约束见 [Agent 总览](../agents/index.md)。

## 6. 本地运行时数据

`data/state` 保存 cosmos 的本地运行时状态，默认不进入版本库；只有说明文件被跟踪。路径由 `agent.cosmos.contract.state_dir` 配置，默认就是 `data/state`。写盘统一采用「临时文件 + replace」；进程异常时可能残留 `*.tmp`，可以安全删除。

### 文件说明

| 文件 | 写入方 | 内容 | 是否可删 |
| --- | --- | --- | --- |
| `desktop.json` | `DesktopService` | 桌面布局、置顶、分组覆盖 | 可删，重建默认布局 |
| `terminal_stats.json` | `TerminalService` | 按天聚合的消息数、活跃小时、响应延迟、主动消息发送/回复 | 可删，历史画像归零 |
| `proactive.json` | `TerminalService` | 当日主动消息计数与未回应降频状态 | 可删，节流状态重置 |
| `dead_letters.jsonl` | `TerminalService` | 出站投递重试耗尽的消息，按行 JSON 追加 | 可删；删前先确认是否需要补发 |
| `world_trace.jsonl` | `WorldBridgeService` | 每次世界推演一行：触发、显著性、置信度、信念、行动选择 | 可删，校准样本丢失 |
| `world_trace.jsonl.1` | `WorldBridgeService` | 轨迹超过 `trace_max_bytes` 后的轮转文件 | 可删 |
| `world_calibration.json` | 校准 CLI | 温度、阈值、触发统计报告 | 可删，可重新生成 |
| `world_trigger_model.json` | 校准 CLI | 轻量触发分类器权重 | 可删；删除后回退规则打分 |

这里只存聚合计数与推理结论，不存用户消息原文；`world_trace.jsonl` 中的 `snapshot` 是截断后的派生描述，不是原始对话。

生成校准报告：

```bash
python -m agent.cosmos.world.calibration \
  --trace data/state/world_trace.jsonl \
  --out data/state/world_calibration.json \
  --model-out data/state/world_trigger_model.json
```
