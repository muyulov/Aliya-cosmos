"""离线校准：温度、显著性阈值与触发分类器。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from agent.cosmos.world.calibration import (
    calibrate,
    fit_min_salience,
    fit_temperature,
    main,
    train_classifier,
)
from agent.cosmos.world.classifier import TriggerClassifier, event_features
from agent.cosmos.world.trace import TraceRecord, read_traces


def _write_synthetic(path: Path) -> None:
    start = datetime.now(tz=UTC) - timedelta(days=2)
    lines: list[dict[str, object]] = []
    for index in range(20):
        high_at = start + timedelta(minutes=index * 10)
        low_at = high_at + timedelta(minutes=5)
        lines.append(
            {
                "at": high_at.isoformat(),
                "inference_id": f"inf_high_{index}",
                "trigger": "presence",
                "salience": 0.8,
                "confidence": 0.9,
                "prediction_error": 0.6,
                "trigger_features": event_features("presence", {"state": "offline"}),
            }
        )
        lines.append(
            {
                "at": low_at.isoformat(),
                "inference_id": f"inf_low_{index}",
                "trigger": "tick",
                "salience": 0.2,
                "confidence": 0.2,
                "prediction_error": 0.1,
                "trigger_features": event_features("tick"),
            }
        )
    _ = path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )


def test_读取轨迹跳过坏行_并支持窗口(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    now = datetime(2026, 11, 8, 9, tzinfo=UTC)
    rows = [
        {"at": (now - timedelta(days=1)).isoformat(), "inference_id": "keep"},
        {"at": (now - timedelta(days=30)).isoformat(), "inference_id": "old"},
        {"at": "not-a-time", "inference_id": "bad"},
    ]
    _ = path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\nnot-json\n", encoding="utf-8"
    )
    records = read_traces(path, window_days=14, now=now)
    assert [record.inference_id for record in records] == ["keep"]


def test_空轨迹返回默认校准(tmp_path: Path) -> None:
    report = calibrate(tmp_path / "missing.jsonl")
    assert report.records == 0
    assert report.label_source == "none"
    assert report.temperature == 1.5
    assert report.min_salience == 0.35


def test_温度与显著性阈值可拟合(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    _write_synthetic(path)
    records = read_traces(path)
    pairs = [
        (
            previous,
            1.0
            if following.prediction_error is not None and following.prediction_error <= 0.25
            else 0.0,
        )
        for previous, following in pairwise(records)
    ]
    temperature = fit_temperature(pairs)
    threshold = fit_min_salience(pairs)
    assert 0.5 <= temperature <= 5.0
    assert 0.2 < threshold <= 0.8


def test_校准产出分类器并偏向离线事件(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    _write_synthetic(path)
    report = calibrate(path, min_samples=10)
    assert report.label_source == "next_inference"
    assert report.labeled >= 10
    offline = event_features("presence", {"state": "offline"})
    tick = event_features("tick")
    assert report.classifier.probability(offline) > report.classifier.probability(tick)
    assert "presence" in report.per_trigger


def test_分类器权重可写盘并重读(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    _write_synthetic(path)
    report = calibrate(path, min_samples=10)
    model_path = tmp_path / "model.json"
    report.classifier.save(model_path)
    loaded = TriggerClassifier.from_path(model_path)
    offline = event_features("presence", {"state": "offline"})
    assert loaded.probability(offline) == report.classifier.probability(offline)


def test_命令行产出报告与模型(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_synthetic(trace)
    out = tmp_path / "calibration.json"
    model = tmp_path / "model.json"
    assert (
        main(
            [
                "--trace",
                str(trace),
                "--out",
                str(out),
                "--model-out",
                str(model),
                "--min-samples",
                "10",
            ]
        )
        == 0
    )
    assert out.exists()
    assert model.exists()


def test_单类别训练标记退化() -> None:
    record = TraceRecord.from_dict(
        {
            "at": datetime.now(tz=UTC).isoformat(),
            "inference_id": "inf_single",
            "trigger": "presence",
            "salience": 0.8,
            "trigger_features": event_features("presence", {"state": "offline"}),
        }
    )
    model = train_classifier([(record, 1.0)])
    assert model.degenerate is True
