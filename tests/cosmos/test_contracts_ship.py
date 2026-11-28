"""船图契约测试：解析、邻接、最短耗时与非法输入报错。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.cosmos.contracts.ship import ShipGraph, load_ship


def _valid() -> dict[str, object]:
    return {
        "version": 1,
        "ship": "海獭号",
        "start": "cabin",
        "places": [
            {"id": "bridge", "name": "驾驶舱", "signal": "strong", "activities": ["校对航向"]},
            {"id": "cabin", "name": "寝室", "signal": "weak", "activities": ["躺着"]},
            {"id": "engine", "name": "动力室", "signal": "none", "activities": ["修阀门"]},
        ],
        "edges": [
            {"a": "bridge", "b": "cabin", "seconds": 60},
            {"a": "cabin", "b": "engine", "seconds": 110},
        ],
    }


def test_解析船图并支持邻接与最短耗时() -> None:
    graph = ShipGraph.model_validate(_valid())
    assert graph.neighbors("cabin") == ["bridge", "engine"]
    assert graph.travel_seconds("bridge", "engine") == 170
    assert graph.travel_seconds("bridge", "unknown") is None
    assert graph.signal("engine") == "none"
    assert graph.activities_at("cabin") == ["躺着"]


def test_缺失或损坏时直接报错(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _ = load_ship(tmp_path / "nope.json")

    bad = tmp_path / "ship.json"
    _ = bad.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        _ = load_ship(bad)

    _ = bad.write_text(
        '{"version":1,"start":"nowhere","places":[{"id":"cabin","name":"寝室"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="起点不存在"):
        _ = load_ship(bad)


def test_仓库船图可加载() -> None:
    graph = load_ship()
    assert graph.ship == "海獭号"
    assert "incubator" in graph.neighbors("galley")
    assert graph.travel_seconds("bridge", "hangar") is not None
