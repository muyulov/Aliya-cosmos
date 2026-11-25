from agent.aliya.turn.postcheck import check


def test_masks_internal_id() -> None:
    result = check("任务 tsk_abc 完成了")
    assert "tsk_abc" not in result.text
    assert "internal_ids" in result.rules


def test_flags_narration() -> None:
    result = check("（笑）好啊")
    assert "narration" in result.rules


def test_keeps_punctuation() -> None:
    result = check("在吗？")
    assert result.text == "在吗？"


def test_flags_engineering_terms() -> None:
    result = check("我这个 agent 收到了")
    assert "engineering_terms" in result.rules


def test_empty_message_is_dropped() -> None:
    result = check("   ")
    assert result.text == ""
    assert "empty" in result.rules
