"""生活状态存储测试：往返、损坏降级、流水追加与并发。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from agent.aliya.life.models import Activity, ActivityState
from agent.aliya.life.store import ActivityStore

NOW = datetime(2026, 11, 28, 14, 0, tzinfo=UTC)


async def test_保存后重载并保持往返(tmp_path: Path) -> None:
    store = ActivityStore(tmp_path)
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
    assert (await store.load()).current == state.current
    assert (tmp_path / "activity.json").exists()


async def test_损坏状态改名备份后重建(tmp_path: Path) -> None:
    path = tmp_path / "activity.json"
    _ = path.write_text("{bad", encoding="utf-8")
    store = ActivityStore(tmp_path)
    state = await store.load()
    assert state.current is None
    assert (tmp_path / "activity.json.bad").exists()


async def test_追加流水并读取最近条目(tmp_path: Path) -> None:
    store = ActivityStore(tmp_path)
    state = ActivityState(updated_at=NOW)
    state.current = Activity(
        id="act_1", title="修阀门", place="engine", kind="repair", started_at=NOW
    )
    await store.append(
        state, ended_at=NOW, notes="阀门卡了半圈", files=["maintenance/2026-11-28.md"]
    )
    await store.append(state, ended_at=NOW, notes="记了一行观测")
    recent = store.recent(limit=1)
    assert len(recent) == 1
    assert recent[0]["notes"] == "记了一行观测"
    assert recent[0]["id"] == "act_1"


async def test_并发保存不交错(tmp_path: Path) -> None:
    store = ActivityStore(tmp_path)
    await asyncio.gather(
        *(store.save(ActivityState(place=f"p{i}", updated_at=NOW)) for i in range(20))
    )
    assert (await store.load()).version == 1


async def test_只读取流水尾部足够行数(tmp_path: Path) -> None:
    path = tmp_path / "activity.jsonl"
    lines = [f'{{"id": "act_{index}", "notes": "第{index}条"}}' for index in range(200)]
    _ = path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    store = ActivityStore(tmp_path)
    recent = store.recent(limit=3)
    assert [item["id"] for item in recent] == ["act_197", "act_198", "act_199"]
