"""从 `world_trace.jsonl` 离线校准触发阈值、温度与轻量分类器。

标签来自「下一轮推演的预测误差」：如果下一轮的 `prediction_error` 很低，
说明上一轮结论对后续观测解释得好。这是无需用户标注的代理标签；等终端把
主动消息的「回复/忽略」写回轨迹后，可以替换成真实反馈。

用法：

    python -m agent.cosmos.world.calibration \
        --trace data/state/world_trace.jsonl \
        --out data/state/world_calibration.json \
        --model-out data/state/world_trigger_model.json
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import cast

from agent.cosmos.world.classifier import FEATURE_NAMES, TriggerClassifier
from agent.cosmos.world.trace import TraceRecord, read_traces

#: 相邻两轮超过这个间隔就不当作同一段因果
MAX_PAIR_GAP_SECONDS = 3600.0
#: 预测误差低于该值视为「上一轮结论成立」
DEFAULT_LABEL_ERROR = 0.25
#: 温度网格：0.5–5.0，步长 0.05；先粗后细的网格足够，不引入优化器依赖
TEMPERATURE_GRID: tuple[float, ...] = tuple(0.5 + 0.05 * i for i in range(91))
#: 显著性网格：0.05–0.95；低于 0.05 等于全部放行，高于 0.95 过于苛刻
SALIENCE_GRID: tuple[float, ...] = tuple(0.05 * i for i in range(1, 20))


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """一次离线校准的可回填结果。"""

    generated_at: str
    trace_path: str
    window_days: int
    records: int
    labeled: int
    label_source: str
    temperature: float
    min_salience: float
    per_trigger: dict[str, dict[str, float]]
    classifier: TriggerClassifier

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "generated_at": self.generated_at,
            "trace_path": self.trace_path,
            "window_days": self.window_days,
            "records": self.records,
            "labeled": self.labeled,
            "label_source": self.label_source,
            "temperature": self.temperature,
            "min_salience": self.min_salience,
            "per_trigger": self.per_trigger,
            "classifier": self.classifier.to_dict(),
        }


def calibrate(
    trace_path: str | Path,
    *,
    window_days: int = 14,
    min_samples: int = 20,
    label_error: float = DEFAULT_LABEL_ERROR,
    now: datetime | None = None,
) -> CalibrationReport:
    """读轨迹并产出温度、阈值、触发统计与分类器权重。"""
    records = read_traces(trace_path, window_days=window_days, now=now)
    pairs, source = _label_pairs(records, label_error, min_samples)
    current = now or datetime.now(tz=UTC)
    if not pairs:
        return CalibrationReport(
            generated_at=current.isoformat(),
            trace_path=str(trace_path),
            window_days=window_days,
            records=len(records),
            labeled=0,
            label_source="none",
            temperature=1.5,
            min_salience=0.35,
            per_trigger={},
            classifier=TriggerClassifier(),
        )

    temperature = fit_temperature(pairs)
    min_salience = fit_min_salience(pairs)
    per_trigger = summarize_triggers(pairs)
    classifier = train_classifier(pairs, trained_at=current.isoformat())
    return CalibrationReport(
        generated_at=current.isoformat(),
        trace_path=str(trace_path),
        window_days=window_days,
        records=len(records),
        labeled=len(pairs),
        label_source=source,
        temperature=temperature,
        min_salience=min_salience,
        per_trigger=per_trigger,
        classifier=classifier,
    )


def fit_temperature(pairs: Sequence[tuple[TraceRecord, float]]) -> float:
    """网格搜索温度，最小化置信度的对数损失。"""
    labels = {label for _, label in pairs}
    if len(labels) < 2:
        return 1.5
    best_temperature = 1.5
    best_loss = math.inf
    for temperature in TEMPERATURE_GRID:
        loss = 0.0
        for record, label in pairs:
            probability = _temperature_scale(record.confidence, temperature)
            loss -= label * math.log(max(probability, 1e-9)) + (1.0 - label) * math.log(
                max(1.0 - probability, 1e-9)
            )
        if loss < best_loss:
            best_loss = loss
            best_temperature = temperature
    return round(best_temperature, 2)


def fit_min_salience(pairs: Sequence[tuple[TraceRecord, float]]) -> float:
    """在显著性网格上最大化 F1；全正样本时保持低阈值。"""
    usable = [(record, label) for record, label in pairs if record.salience > 0]
    if usable:
        pairs = usable
    positives = sum(1 for _, label in pairs if label >= 0.5)
    if positives == 0 or positives == len(pairs):
        return 0.05 if positives == len(pairs) else 0.35
    best_threshold = 0.35
    best_f1 = -1.0
    for threshold in SALIENCE_GRID:
        tp = fp = fn = 0
        for record, label in pairs:
            predicted = record.salience >= threshold
            actual = label >= 0.5
            if predicted and actual:
                tp += 1
            elif predicted and not actual:
                fp += 1
            elif not predicted and actual:
                fn += 1
        if tp == 0:
            continue
        precision = tp / (tp + fp)
        recall = tp / (tp + fn)
        f1 = 2 * precision * recall / max(precision + recall, 1e-9)
        if f1 > best_f1:
            best_f1 = f1
            best_threshold = threshold
    return round(best_threshold, 2)


def summarize_triggers(pairs: Sequence[tuple[TraceRecord, float]]) -> dict[str, dict[str, float]]:
    """按触发来源汇总样本量与标签率，用于决定是否停用某类触发。"""
    buckets: dict[str, list[tuple[TraceRecord, float]]] = {}
    for record, label in pairs:
        buckets.setdefault(record.trigger, []).append((record, label))
    summary: dict[str, dict[str, float]] = {}
    for trigger, items in buckets.items():
        errors = [
            record.prediction_error for record, _ in items if record.prediction_error is not None
        ]
        summary[trigger] = {
            "samples": float(len(items)),
            "positive_rate": sum(label for _, label in items) / len(items),
            "mean_salience": sum(record.salience for record, _ in items) / len(items),
            "mean_error": (sum(errors) / len(errors)) if errors else 0.0,
        }
    return summary


def train_classifier(
    pairs: Sequence[tuple[TraceRecord, float]],
    *,
    epochs: int = 400,
    learning_rate: float = 0.15,
    l2: float = 0.01,
    trained_at: str | None = None,
) -> TriggerClassifier:
    """全批量逻辑回归；特征只有十几个，训练在毫秒级。"""
    weights: dict[str, float] = {name: 0.0 for name in FEATURE_NAMES}
    # 手动刷新没有事件特征，只更新偏置会稀释真实信号，训练时直接跳过
    usable = [(record, label) for record, label in pairs if record.features]
    if usable:
        pairs = usable
    if not pairs:
        return TriggerClassifier(weights=weights, trained_at=trained_at)
    labels = {label for _, label in pairs}
    if len(labels) < 2:
        # 单一类别无法学习；给一个保守偏置，并标记退化，运行时退回规则。
        weights["bias"] = -1.0 if labels == {0.0} else 1.0
        return TriggerClassifier(
            weights=weights,
            trained_at=trained_at,
            samples=len(pairs),
            degenerate=True,
        )

    # 全批量梯度下降：样本量小、特征少，无需 mini-batch 或优化器。
    for _ in range(epochs):
        gradients: dict[str, float] = {name: 0.0 for name in FEATURE_NAMES}
        for record, label in pairs:
            features = record.features
            z = sum(weights.get(name, 0.0) * features.get(name, 0.0) for name in FEATURE_NAMES)
            probability = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            error = probability - label
            for name in FEATURE_NAMES:
                gradients[name] += error * features.get(name, 0.0)
        count = float(len(pairs))
        for name in FEATURE_NAMES:
            weights[name] -= learning_rate * (gradients[name] / count + l2 * weights[name])
    return TriggerClassifier(
        weights={name: round(value, 4) for name, value in weights.items()},
        trained_at=trained_at,
        samples=len(pairs),
    )


def _label_pairs(
    records: Sequence[TraceRecord],
    label_error: float,
    min_samples: int,
) -> tuple[list[tuple[TraceRecord, float]], str]:
    """优先用下一轮误差做标签；样本不足时退回本轮误差。"""
    pairs: list[tuple[TraceRecord, float]] = []
    for previous, following in pairwise(records):
        if following.prediction_error is None:
            continue
        # 间隔太远就不构成「上一轮预测 → 下一轮观测」的因果对，
        # 否则会把几天后的状态当成即时验证。
        gap = (following.at - previous.at).total_seconds()
        if gap < 0 or gap > MAX_PAIR_GAP_SECONDS:
            continue
        label = 1.0 if following.prediction_error <= label_error else 0.0
        pairs.append((previous, label))
    # 样本足够时用下一轮误差做代理标签；不足时退回本轮自误差，
    # 但会在报告里标成 self_error，提醒不要直接采信。
    if len(pairs) >= min_samples:
        return pairs, "next_inference"

    fallback = [
        (
            record,
            1.0 if record.prediction_error <= label_error else 0.0,
        )
        for record in records
        if record.prediction_error is not None
    ]
    return fallback, "self_error"


def _temperature_scale(probability: float, temperature: float) -> float:
    bounded = min(1.0 - 1e-6, max(1e-6, probability))
    logit = math.log(bounded / (1.0 - bounded))
    return 1.0 / (1.0 + math.exp(-logit / max(1e-6, temperature)))


def _write_report(path: str | Path, report: CalibrationReport) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _ = target.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：产出校准报告，可选同时写分类器权重。"""
    parser = argparse.ArgumentParser(description="用 world_trace.jsonl 校准世界推理")
    _ = parser.add_argument("--trace", default="data/state/world_trace.jsonl")
    _ = parser.add_argument("--out", default="data/state/world_calibration.json")
    _ = parser.add_argument("--model-out", default=None)
    _ = parser.add_argument("--window-days", type=int, default=14)
    _ = parser.add_argument("--min-samples", type=int, default=20)
    _ = parser.add_argument("--label-error", type=float, default=DEFAULT_LABEL_ERROR)
    args = parser.parse_args(argv)
    trace = cast("str", args.trace)
    out = cast("str", args.out)
    model_out = cast("str | None", args.model_out)
    window_days = cast("int", args.window_days)
    min_samples = cast("int", args.min_samples)
    label_error = cast("float", args.label_error)

    report = calibrate(
        trace,
        window_days=window_days,
        min_samples=min_samples,
        label_error=label_error,
    )
    _write_report(out, report)
    if model_out:
        report.classifier.save(model_out)
    summary = (
        f"校准完成：records={report.records} labeled={report.labeled} "
        f"temperature={report.temperature} min_salience={report.min_salience} "
        f"source={report.label_source}"
    )
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CalibrationReport",
    "calibrate",
    "fit_min_salience",
    "fit_temperature",
    "main",
    "summarize_triggers",
    "train_classifier",
]
