"""日终反思测试：跨日只触发一次，产出写进事实。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.aliya.life.models import Activity, ActivityState
from agent.aliya.life.reflection import maybe_reflect
from agent.aliya.life.store import ActivityStore

NOW = datetime(2026, 11, 28, 23, 5, tzinfo=UTC)


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_structured(self, messages: Any, schema: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return schema.model_validate({"facts": ["阀门还是没拧动，明天换个撬法"]})


async def test_跨日首次触发一次并写入事实(tmp_path: Path) -> None:
    store = ActivityStore(tmp_path)
    state = ActivityState(updated_at=NOW)
    state.current = Activity(
        id="act_1", title="修阀门", place="engine", kind="repair", started_at=NOW
    )
    await store.append(state, ended_at=NOW, notes="阀门卡了半圈")
    llm = FakeLLM()
    facts = await maybe_reflect(llm, store, now=NOW, last_reflection_date="2026-11-27")
    assert facts == ["阀门还是没拧动，明天换个撬法"]
    assert llm.calls == 1


async def test_同一天不重复触发(tmp_path: Path) -> None:
    llm = FakeLLM()
    _ = await maybe_reflect(
        llm, ActivityStore(tmp_path), now=NOW, last_reflection_date="2026-11-28"
    )
    assert llm.calls == 0


async def test_当天已反思后夜间不再重复调模型(tmp_path: Path) -> None:
    store = ActivityStore(tmp_path)
    for index in range(5):
        state = ActivityState(updated_at=NOW)
        state.current = Activity(
            id=f"act_{index}",
            title="修阀门",
            place="engine",
            kind="repair",
            started_at=NOW,
        )
        await store.append(state, ended_at=NOW, notes="又卡住了")
    llm = FakeLLM()
    facts = await maybe_reflect(llm, store, now=NOW, last_reflection_date="2026-11-28")
    assert facts == []
    assert llm.calls == 0
