"""Aliya 侧船图适配测试：邻接、可达活动与通信条件取值。"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.aliya.life.models import Move
from agent.aliya.life.ship import ShipAdapter
from agent.cosmos.contracts.ship import ShipGraph

GRAPH = ShipGraph.model_validate(
    {
        "start": "cabin",
        "places": [
            {"id": "cabin", "name": "寝室", "signal": "weak", "activities": ["躺着"]},
            {"id": "engine", "name": "动力室", "signal": "none", "activities": ["修阀门"]},
            {"id": "galley", "name": "食堂", "signal": "strong", "activities": ["找点甜的"]},
        ],
        "edges": [
            {"a": "cabin", "b": "engine", "seconds": 110},
            {"a": "cabin", "b": "galley", "seconds": 50},
        ],
    }
)


def test_适配邻接与可达活动() -> None:
    adapter = ShipAdapter(GRAPH)
    assert set(adapter.neighbors("cabin")) == {"engine", "galley"}
    assert adapter.travel_seconds("cabin", "engine") == 110
    assert adapter.activities_at("engine") == ["修阀门"]
    assert adapter.name_of("galley") == "食堂"


def test_移动中取两端较差信号() -> None:
    adapter = ShipAdapter(GRAPH)
    now = datetime(2026, 11, 28, 14, 0, tzinfo=UTC)
    move = Move(**{"from": "engine", "to": "galley", "departed_at": now, "arrive_at": now})
    assert adapter.comms_at("engine", moving=move) == "none"
    assert adapter.comms_at("galley", moving=None) == "strong"
