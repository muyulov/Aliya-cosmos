# data/state

本目录保存 cosmos 的**本地运行时状态**，默认不进入版本库；只有本说明文件被跟踪。

路径由 `agent.cosmos.contract.state_dir` 配置，默认就是 `data/state`。写盘统一采用「临时文件 + replace」；进程异常时可能残留 `*.tmp`，可以安全删除。

## 文件说明

| 文件 | 写入方 | 内容 | 是否可删 |
| --- | --- | --- | --- |
| `desktop.json` | `DesktopService` | 桌面布局、置顶、分组覆盖 | 可删，重建默认布局 |
| `terminal_stats.json` | `TerminalService` | 按天聚合的消息数、活跃小时、响应延迟、主动消息发送/回复 | 可删，历史画像归零 |
| `proactive.json` | `TerminalService` | 当日主动消息计数与未回应降频状态 | 可删，节流状态重置 |
| `world_trace.jsonl` | `WorldBridgeService` | 每次世界推演一行：触发、显著性、置信度、信念、行动选择 | 可删，校准样本丢失 |
| `world_trace.jsonl.1` | `WorldBridgeService` | 轨迹超过 `trace_max_bytes` 后的轮转文件 | 可删 |
| `world_calibration.json` | 校准 CLI | 温度、阈值、触发统计报告 | 可删，可重新生成 |
| `world_trigger_model.json` | 校准 CLI | 轻量触发分类器权重 | 可删；删除后回退规则打分 |

## 生成校准报告

```bash
python -m agent.cosmos.world.calibration \
  --trace data/state/world_trace.jsonl \
  --out data/state/world_calibration.json \
  --model-out data/state/world_trigger_model.json
```

## 隐私边界

这里只存聚合计数与推理结论，不存用户消息原文；`world_trace.jsonl` 中的 `snapshot` 是截断后的派生描述，不是原始对话。
