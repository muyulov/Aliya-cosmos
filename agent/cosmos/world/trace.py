"""推演轨迹的读取模型。

轨迹是**追加写、单行 JSON**，字段只增不减；读取端对缺字段一律给默认值，
这样旧轨迹也能参与校准。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """一行推演轨迹。"""

    at: datetime
    inference_id: str
    revision: int = 0
    producer: str = "deep"
    trigger: str = "tick"
    salience: float = 0.0
    fingerprint: str = ""
    confidence: float = 0.0
    prediction_error: float | None = None
    urgency: float = 0.0
    interruptibility: float | None = None
    expected_value: float | None = None
    #: 触发特征，供分类器训练；旧轨迹没有这一列时为空
    features: dict[str, float] = field(default_factory=dict)
    #: 原始行，保留未知字段，便于后续字段只增不减地兼容
    raw: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TraceRecord:
        at = _as_datetime(data.get("at"))
        if at is None:
            raise ValueError("轨迹缺少合法时间")
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        return cls(
            at=at,
            inference_id=str(data.get("inference_id", "")),
            revision=_as_int(data.get("revision")),
            producer=str(data.get("producer", "deep")),
            trigger=str(data.get("trigger", "tick")),
            salience=_as_float(data.get("salience")),
            fingerprint=str(data.get("fingerprint", "")),
            confidence=_as_float(data.get("confidence")),
            prediction_error=_optional_float(data.get("prediction_error")),
            urgency=_as_float(data.get("urgency")),
            interruptibility=_optional_float(data.get("interruptibility")),
            expected_value=_optional_float(data.get("expected_value")),
            features=_as_features(data.get("trigger_features")),
            raw=data,
        )


def read_traces(
    path: str | Path,
    *,
    window_days: int = 14,
    now: datetime | None = None,
) -> list[TraceRecord]:
    """读轨迹并按时间窗口过滤；坏行跳过，不阻断整体校准。"""
    trace_path = Path(path)
    if not trace_path.exists():
        return []
    current = now or datetime.now(tz=UTC)
    # 先按时间窗口粗过滤，再排序；窗口外的轨迹不参与本轮校准，
    # 避免旧阈值/旧模型的行为污染当前拟合。
    cutoff = current - timedelta(days=window_days)
    records: list[TraceRecord] = []
    with trace_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                raw: object = cast("object", json.loads(text))
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            try:
                record = TraceRecord.from_dict(cast("dict[str, object]", raw))
            except ValueError:
                continue
            if record.at >= cutoff:
                records.append(record)
    records.sort(key=lambda item: item.at)
    return records


def _as_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_features(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _as_float(item) for key, item in cast("dict[str, object]", value).items()}


__all__ = ["TraceRecord", "read_traces"]
