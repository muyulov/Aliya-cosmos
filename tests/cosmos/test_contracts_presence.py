"""契约模型（presence）的数值约束与 round-trip。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agent.cosmos.contracts import (
    Availability,
    MoodInfo,
    PresenceChanged,
    PresenceFields,
    PresenceInput,
    PresenceSnapshot,
    ReadablePresence,
)


def make_snapshot() -> PresenceSnapshot:
    return PresenceSnapshot(
        captured_at=datetime.now(UTC),
        fields=PresenceFields(),
        availability={"online": Availability(available=True)},
        readable=ReadablePresence(summary="你在线"),
        revision=1,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"valence": 1.2},
        {"valence": -1.2},
        {"confidence": -0.1},
        {"energy": 1.1},
    ],
)
def test_情绪数值越界报错(payload: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        _ = MoodInfo.model_validate(payload)


def test_情绪合法边界通过() -> None:
    mood = MoodInfo(valence=-1, energy=0, confidence=1)
    assert mood.valence == -1


def test_可用性原因必须是闭集() -> None:
    with pytest.raises(ValidationError):
        _ = Availability.model_validate({"available": False, "reason": "nope"})


def test_快照_json_round_trip() -> None:
    snapshot = make_snapshot()
    restored = PresenceSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored == snapshot


def test_变化事件_json_round_trip() -> None:
    snapshot = make_snapshot()
    event = PresenceChanged(
        revision=snapshot.revision,
        changed=["typing"],
        snapshot=snapshot,
        readable=snapshot.readable,
        at=datetime.now(UTC),
    )
    restored = PresenceChanged.model_validate_json(event.model_dump_json())
    assert restored == event


def test_通道输入全部可选() -> None:
    assert PresenceInput().online is None
    assert PresenceInput(is_typing=True).is_typing is True
