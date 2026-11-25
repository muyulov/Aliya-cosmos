from agent.aliya.prompt.loader import load_charter, load_persona


def test_charter_visible_zone() -> None:
    text = load_charter("data/prompt/Aliya.charter.md")
    assert "你可以" in text
    assert "实现约束" not in text


def test_persona_loads() -> None:
    text = load_persona("data/prompt/Aliya.md")
    assert "Aliya" in text
