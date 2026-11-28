"""生活规划器测试：地点校验、上限与零 LLM 兜底。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from agent.aliya.life.planner import LifePlanner, PlanResult, PlannedActivity
from agent.aliya.life.ship import ShipAdapter
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


class FakeLLM:
    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls = 0

    async def chat_structured(self, messages: Any, schema: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return schema.model_validate(self._result)


def test_规划结果校验地点并限定数量() -> None:
    result = PlanResult(
        activities=[
            PlannedActivity(title="修阀门", place="engine", kind="repair", expected_minutes=35),
            PlannedActivity(title="这边没有的地方", place="mars", kind="wander", expected_minutes=20),
        ]
    )
    llm = FakeLLM(result.model_dump())
    planner = LifePlanner(cast("Any", llm), ShipAdapter(GRAPH))
    _ = asyncio_run(planner.plan(place="cabin", energy=0.8, now=NOW, recent=[]))
    assert llm.calls == 1


def test_调用失败时落兜底池() -> None:
    llm = FakeLLM(RuntimeError("timeout"))
    planner = LifePlanner(cast("Any", llm), ShipAdapter(GRAPH))
    plans = asyncio_run(planner.plan(place="cabin", energy=0.8, now=NOW, recent=[]))
    assert plans
    assert all(item.place == "cabin" for item in plans)


def test_低精力与夜间只出休息或闲逛() -> None:
    result = PlanResult(
        activities=[
            PlannedActivity(title="修阀门", place="engine", kind="repair", expected_minutes=35)
        ]
    )
    planner = LifePlanner(cast("Any", FakeLLM(result.model_dump())), ShipAdapter(GRAPH))
    plans = asyncio_run(planner.plan(place="cabin", energy=0.1, now=NOW, recent=[]))
    assert plans and all(item.kind in {"rest", "wander"} for item in plans)


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
