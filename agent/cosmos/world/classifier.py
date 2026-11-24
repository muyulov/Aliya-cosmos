"""轻量触发分类器：只判断「这次事件值不值得推演」。

刻意不用 sklearn / torch：特征只有十几个布尔与归一化数值，逻辑回归足够，
权重是可读的 JSON，部署时不需要额外依赖，也可以人工审查与手改。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

#: 固定特征顺序；新增特征只能追加，旧权重文件缺字段按 0 处理
FEATURE_NAMES: tuple[str, ...] = (
    "bias",
    "kind_presence",
    "kind_dispatch",
    "kind_silence",
    "kind_link",
    "kind_user_message",
    "kind_mood",
    "kind_tick",
    "kind_manual",
    "state_offline",
    "state_online",
    "state_away",
    "state_busy",
    "state_unknown",
    "status_failed",
    "status_partial",
    "status_ok",
    "status_cancelled",
    "minutes",
    "delta",
)


def event_features(kind: str, payload: dict[str, object] | None = None) -> dict[str, float]:
    """把事件压成固定长度的数值特征。

    训练与运行必须走同一个函数：否则线下训练出的权重会与线上特征错位，
    表现为「模型看起来正常、分数却完全不对」。
    """
    features: dict[str, float] = {name: 0.0 for name in FEATURE_NAMES}
    features["bias"] = 1.0
    kind_key = f"kind_{kind}"
    if kind_key in features:
        features[kind_key] = 1.0
    data = payload or {}
    state = str(data.get("state") or "")
    state_key = f"state_{state}"
    if state_key in features:
        features[state_key] = 1.0
    status = str(data.get("status") or "")
    status_key = f"status_{status}"
    if status_key in features:
        features[status_key] = 1.0
    # 静默分钟用 6 小时做归一化：超过这个尺度都视为「很久」，避免大数压过其他特征。
    features["minutes"] = min(1.0, max(0.0, _as_float(data.get("minutes")) / 360.0))
    # 情绪变化只取绝对值：0.8 与 -0.8 都值得推，方向交给推理侧判断。
    features["delta"] = min(1.0, abs(_as_float(data.get("delta"))))
    return features


@dataclass(frozen=True, slots=True)
class TriggerClassifier:
    """逻辑回归触发器：`sigmoid(bias + Σ w·x)`。"""

    weights: dict[str, float] = field(default_factory=dict)
    threshold: float = 0.5
    trained_at: str | None = None
    samples: int = 0
    #: 训练样本只有单一类别时为 True，运行时应退回规则
    degenerate: bool = False

    def probability(self, features: dict[str, float]) -> float:
        # 缺失特征按 0 处理；z 截断到 ±30，避免 exp 溢出。
        z = sum(self.weights.get(name, 0.0) * value for name, value in features.items())
        z = max(-30.0, min(30.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "threshold": self.threshold,
            "trained_at": self.trained_at,
            "samples": self.samples,
            "degenerate": self.degenerate,
            "weights": dict(self.weights),
        }

    @classmethod
    def from_path(cls, path: str | Path) -> TriggerClassifier:
        raw: object = cast("object", json.loads(Path(path).read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise ValueError("分类器权重不是 JSON 对象")
        data = cast("dict[str, object]", raw)
        raw_weights = data.get("weights")
        if not isinstance(raw_weights, dict):
            raise ValueError("分类器权重缺少 weights")
        weights = {
            str(key): _as_float(value)
            for key, value in cast("dict[str, object]", raw_weights).items()
        }
        # 旧权重文件缺字段时按 0 处理；threshold 缺省回退 0.5。
        return cls(
            weights=weights,
            threshold=_as_float(data.get("threshold") or 0.5),
            trained_at=str(data["trained_at"]) if data.get("trained_at") else None,
            samples=int(_as_float(data.get("samples"))),
            degenerate=bool(data.get("degenerate", False)),
        )

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


__all__ = ["FEATURE_NAMES", "TriggerClassifier", "event_features"]
