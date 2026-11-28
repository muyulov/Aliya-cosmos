"""生活状态机测试：到期结束、跨舱移动与到达后开工。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from agent.aliya.life.loop import LifeLoop
from agent.aliya.life.models import Activity, ActivityState, Move, PendingActivity
from agent.aliya.life.ship import ShipAdapter
from agent.aliya.life.store import ActivityStore
from agent.cosmos.contracts.ship import ShipGraph

GRAPH = ShipGraph.model_validate(
    {
        "start": "cabin",
        "places": [
            {"id": "cabin", "name": "寝室", "signal": "weak", "activities": ["躺着"]},
            {"id": "engine", "name": "动力室", "signal": "none", "activities": ["修阀门"]},
        ],
        "edges": [{"a": "cabin", "b": "engine", "seconds": 110}],
    }
)
NOW = datetime(2026, 11, 28, 14, 0, tzinfo=UTC)


class StubPlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, **kwargs: Any) -> list[PendingActivity]:
        self.calls += 1
        return [PendingActivity(title="修阀门", place="engine", kind="repair", expected_minutes=30)]


def build(tmp_path: Path) -> tuple[LifeLoop, StubPlanner, ActivityStore]:
    planner = StubPlanner()
    store = ActivityStore(tmp_path)
    loop = LifeLoop(store, cast("Any", planner), ShipAdapter(GRAPH), plan_minutes=40)
    return loop, planner, store


async def test_活动到期后落流水并清空(tmp_path: Path) -> None:
    loop, _, store = build(tmp_path)
    state = ActivityState(place="engine", updated_at=NOW)
    state.current = Activity(
        id="act_1",
        title="修阀门",
        place="engine",
        kind="repair",
        started_at=NOW,
        expected_minutes=30,
    )
    await store.save(state)
    decision = await loop.advance(NOW + timedelta(minutes=31))
    assert decision.kind == "wait"
    assert (await store.load()).current is None
    assert store.recent()[0]["id"] == "act_1"


async def test_跨舱室先移动后到达再开活动(tmp_path: Path) -> None:
    loop, _, store = build(tmp_path)
    await store.save(ActivityState(updated_at=NOW))
    moving = await loop.advance(NOW)
    assert moving.kind == "move" and moving.destination == "engine"
    state = await store.load()
    assert state.moving is not None and state.current is None
    assert state.moving.arrive_at - state.moving.departed_at == timedelta(seconds=110)

    waiting = await loop.advance(NOW + timedelta(seconds=10))
    assert waiting.kind == "wait"

    arrived = await loop.advance(NOW + timedelta(seconds=111))
    assert arrived.kind == "move"
    assert (await store.load()).place == "engine"

    started = await loop.advance(NOW + timedelta(seconds=112))
    assert started.kind == "start" and started.activity is not None
    assert started.activity.title == "修阀门"


async def test_移动中不接新活且到期前保持当前活动(tmp_path: Path) -> None:
    loop, _, store = build(tmp_path)
    state = ActivityState(updated_at=NOW)
    state.current = Activity(
        id="act_1",
        title="盯着培养皿",
        place="cabin",
        kind="observe",
        started_at=NOW,
        expected_minutes=30,
    )
    await store.save(state)
    assert (await loop.advance(NOW + timedelta(minutes=1))).kind == "wait"
    assert (await store.load()).current is not None


async def test_到达后上报新地点且可重新规划(tmp_path: Path) -> None:
    loop, _, store = build(tmp_path)
    state = ActivityState(updated_at=NOW)
    state.moving = Move.model_validate(
        {
            "from": "cabin",
            "to": "engine",
            "departed_at": NOW - timedelta(seconds=120),
            "arrive_at": NOW - timedelta(seconds=10),
        }
    )
    await store.save(state)
    decision = await loop.advance(NOW)
    assert decision.kind == "move" and decision.destination == "engine"
    assert (await store.load()).place == "engine"
    assert (await store.load()).moving is None


async def test_休息回升精力而修理消耗(tmp_path: Path) -> None:
    loop, _, store = build(tmp_path)
    state = ActivityState(place="cabin", energy=0.2, updated_at=NOW)
    state.current = Activity(
        id="act_rest", title="躺一会儿", place="cabin", kind="rest",
        started_at=NOW, expected_minutes=30,
    )
    await store.save(state)
    _ = await loop.advance(NOW + timedelta(minutes=31))
    assert (await store.load()).energy > 0.2

    state = ActivityState(place="cabin", energy=0.8, updated_at=NOW)
    state.current = Activity(
        id="act_fix", title="修阀门", place="cabin", kind="repair",
        started_at=NOW, expected_minutes=30,
    )
    await store.save(state)
    _ = await loop.advance(NOW + timedelta(minutes=31))
    assert (await store.load()).energy < 0.8


async def test_不可达地点被丢弃(tmp_path: Path) -> None:
    loop, _, store = build(tmp_path)
    state = ActivityState(updated_at=NOW)
    state.pending = [PendingActivity(title="去火星", place="mars", kind="wander")]
    await store.save(state)
    decision = await loop.advance(NOW)
    assert decision.kind == "wait"
    assert decision.reason == "活动地点不可达"
    assert (await store.load()).pending == []


async def test_规划按最小间隔节流(tmp_path: Path) -> None:
    class EmptyPlanner:
        def __init__(self) -> None:
            self.calls = 0

        async def plan(self, **kwargs: Any) -> list[PendingActivity]:
            self.calls += 1
            return []

    planner = EmptyPlanner()
    store = ActivityStore(tmp_path)
    loop = LifeLoop(store, cast("Any", planner), ShipAdapter(GRAPH), plan_minutes=40)
    assert (await loop.advance(NOW)).reason == "没有可做的事"
    assert planner.calls == 1
    _ = await loop.advance(NOW + timedelta(minutes=1))
    assert planner.calls == 1
    _ = await loop.advance(NOW + timedelta(minutes=41))
    assert planner.calls == 2
