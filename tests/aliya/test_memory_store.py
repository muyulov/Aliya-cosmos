from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.aliya.memory.store import MemoryStore


def test_append_and_reload(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.append_turn("u1", "user", "在吗")
    store.append_turn("a1", "aliya", "在。")
    assert [turn.text for turn in store.recent_turns()] == ["在吗", "在。"]


def test_atomic_write_keeps_old_on_crash(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.save_anchors(["风信子"])
    assert store.load_anchors() == ["风信子"]


def test_corrupt_json_is_backed_up(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    _ = (tmp_path / "anchors.json").write_text("{not json", encoding="utf-8")
    assert store.load_anchors() == []
    assert (tmp_path / "anchors.json.bad").exists()


def test_recent_turns_honours_limit(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    for index in range(5):
        store.append_turn(f"m{index}", "user", str(index))
    assert [turn.text for turn in store.recent_turns(limit=2)] == ["3", "4"]


def test_remember_rejects_pii(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    assert store.remember("sk-abcdefgh12345678", "fact") is False
    assert store.load_facts() == []


def test_remember_routes_by_kind(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    assert store.remember("她喜欢风信子", "anchor") is True
    assert store.load_anchors() == ["她喜欢风信子"]


def test_remember_rejects_unknown_kind(tmp_path: Path) -> None:
    """未知类型不能静默落到 facts，否则错误分类会长期污染记忆。"""
    store = MemoryStore(tmp_path)
    assert store.remember("随便记点什么", "misc") is False
    assert store.load_facts() == []


def test_record_inbound_is_idempotent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    assert store.record_inbound("u1", "在吗", session_id="s1") is True
    assert store.record_inbound("u1", "在吗", session_id="s1") is False
    assert [turn.text for turn in store.recent_turns()] == ["在吗"]


def test_unanswered_turns_only_returns_unreplied(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    _ = store.record_inbound("u1", "第一句")
    store.append_turn("u1:reply", "aliya", "回复")
    _ = store.record_inbound("u2", "第二句")
    assert [turn.message_id for turn in store.unanswered_turns()] == ["u2"]


def test_cursor_is_per_topic(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.save_cursor(3, topic="agent.result")
    store.save_cursor(1, topic="presence.changed")
    assert store.load_cursor(topic="agent.result") == 3
    assert store.load_cursors() == {"agent.result": 3, "presence.changed": 1}


def test_prune_turns_keeps_latest(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    for index in range(5):
        store.append_turn(f"m{index}", "user", str(index))
    assert store.prune_turns(2) == 3
    assert [turn.text for turn in store.recent_turns()] == ["3", "4"]


def test_intent_roundtrip_and_due(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    now = datetime.now(UTC)
    assert store.add_intent("问她面试怎么样", due_at=now + timedelta(minutes=30)) is True
    assert store.add_intent("提醒她吃药", due_at=now - timedelta(minutes=1)) is True
    assert [item.text for item in store.due_intents(now)] == ["提醒她吃药"]
    assert store.pop_intent("提醒她吃药") is True
    assert [item.text for item in store.load_intents()] == ["问她面试怎么样"]


def test_resolve_open_topic_by_fragment(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.save_open_topics(["海边的约定", "旧照片"])
    assert store.resolve_open_topic("海边") is True
    assert store.load_open_topics() == ["旧照片"]
    assert store.resolve_open_topic("不存在的") is False


def test_intent_dedupes_and_sorts_by_due(tmp_path: Path) -> None:
    """同一句话再次安排应当改期，而不是堆出两条。"""
    store = MemoryStore(tmp_path)
    now = datetime.now(UTC)
    assert store.add_intent("晚点问她面试", due_at=now + timedelta(hours=2))
    assert store.add_intent("提醒她吃药", due_at=now + timedelta(hours=1))
    assert store.add_intent("晚点问她面试", due_at=now + timedelta(hours=3))
    intents = store.load_intents()
    assert [item.text for item in intents] == ["提醒她吃药", "晚点问她面试"]
    assert intents[1].due_at == now + timedelta(hours=3)


def test_cancel_intent_by_fragment(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    assert store.add_intent("问她面试怎么样", due_at=datetime.now(UTC) + timedelta(hours=1))
    assert store.cancel_intent("面试") is True
    assert store.load_intents() == []
    assert store.cancel_intent("不存在") is False
