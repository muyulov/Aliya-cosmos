"""触发门控：显著性打分、动态窗口与去重键。"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from agent.cosmos.services.world_trigger import TriggerKind, WorldTriggerPolicy
from agent.cosmos.world.classifier import TriggerClassifier
from core.config import Settings


def _policy(settings: Settings) -> WorldTriggerPolicy:
    return WorldTriggerPolicy(settings.agent.cosmos.world)


def test_在线翻转比普通事件更显著(settings: Settings) -> None:
    policy = _policy(settings)
    offline = policy.score("presence", {"state": "offline"})
    online = policy.score("presence", {"state": "online"})
    tick = policy.score("tick")
    assert offline is not None and online is not None
    assert offline.salience > online.salience
    assert tick is None  # 0.2 < min_salience 0.35


def test_静默阈值越高越显著(settings: Settings) -> None:
    policy = _policy(settings)
    first = policy.score("silence", {"minutes": 30})
    third = policy.score("silence", {"minutes": 360})
    assert first is not None and third is not None
    assert third.salience > first.salience


def test_任务失败优先于成功(settings: Settings) -> None:
    policy = _policy(settings)
    failed = policy.score("dispatch", {"status": "failed"})
    ok = policy.score("dispatch", {"status": "ok"})
    assert failed is not None and ok is not None
    assert failed.salience > ok.salience


def test_动态窗口高显著更短(settings: Settings) -> None:
    policy = _policy(settings)
    high = policy.window_seconds(0.95)
    low = policy.window_seconds(0.4)
    assert high < low
    assert high == settings.agent.cosmos.world.triggers.high_salience_window_seconds


def test_未知来源不打分(settings: Settings) -> None:
    policy = _policy(settings)
    assert policy.score("mood", {"delta": 0.7}) is not None
    assert policy.score(cast("TriggerKind", cast("object", "mystery"))) is None


def test_去重键带实体(settings: Settings) -> None:
    policy = _policy(settings)
    assert policy.dedupe_key("presence", {"entity": "online"}) == "presence:online"
    assert policy.dedupe_key("dispatch", {"agent_id": "echo"}) == "dispatch:echo"


def test_分类器权重接管打分(settings: Settings, tmp_path: Path) -> None:
    model = TriggerClassifier(
        weights={
            "bias": -3.0,
            "kind_presence": 2.0,
            "state_offline": 3.0,
            "kind_tick": -2.0,
        }
    )
    path = tmp_path / "trigger_model.json"
    model.save(path)
    settings.agent.cosmos.world.triggers.model_path = str(path)

    policy = _policy(settings)
    offline = policy.score("presence", {"state": "offline"})
    assert offline is not None
    assert offline.reason == "分类器"
    assert offline.salience > 0.5
    assert policy.score("tick") is None
    manual = policy.score("manual")
    assert manual is not None and manual.salience == 1.0


def test_分类器损坏时退回规则(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    _ = path.write_text("not json", encoding="utf-8")
    settings.agent.cosmos.world.triggers.model_path = str(path)

    policy = _policy(settings)
    offline = policy.score("presence", {"state": "offline"})
    assert offline is not None
    assert offline.reason != "分类器"
    assert offline.salience > 0.9
