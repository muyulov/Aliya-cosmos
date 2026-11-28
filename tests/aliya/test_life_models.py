"""生活层数据模型测试：序列化往返、别名与校验。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent.aliya.life.models import Activity, ActivityState, Move, PendingActivity

NOW = datetime(2026, 11, 28, 14, 0, tzinfo=UTC)


def test_活动序列化往返与剩余分钟() -> None:
    activity = Activity(
        id="act_1",
        title="把二舱压力阀拧到底",
        place="engine",
        kind="repair",
        started_at=NOW,
        expected_minutes=35,
    )
    restored = Activity.model_validate_json(activity.model_dump_json())
    assert restored == activity
    assert restored.minutes_left(NOW + timedelta(minutes=10)) == 25
    assert restored.minutes_left(NOW + timedelta(minutes=40)) == 0


def test_移动字段使用_from别名() -> None:
    move = Move.model_validate(
        {"from": "cabin", "to": "engine", "departed_at": NOW, "arrive_at": NOW}
    )
    assert move.from_ == "cabin"
    assert move.model_dump(by_alias=True)["from"] == "cabin"


def test_空状态默认值与越界校验() -> None:
    state = ActivityState(updated_at=NOW)
    assert state.version == 1
    assert state.place == "cabin"
    assert state.current is None and state.moving is None and state.pending == []
    with pytest.raises(ValidationError):
        PendingActivity(title="x", place="cabin", kind="repair", expected_minutes=0)
