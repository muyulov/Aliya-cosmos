"""世界推理的离线工具：轨迹读取、触发分类器与校准。

运行时不依赖本包；它只读 `world_trace.jsonl`，产出可回填配置的 JSON。
"""

from agent.cosmos.world.classifier import (
    FEATURE_NAMES,
    TriggerClassifier,
    event_features,
)
from agent.cosmos.world.trace import TraceRecord, read_traces

__all__ = [
    "FEATURE_NAMES",
    "TraceRecord",
    "TriggerClassifier",
    "event_features",
    "read_traces",
]
