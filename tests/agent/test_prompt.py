"""系统提示拼装测试。"""

from __future__ import annotations

import pytest

from core.agent import PromptError, system_prompt


def test_跳过空段并用空行连接() -> None:
    message = system_prompt("  你是助手。  ", None, "", "   ", "答案只给结论。")

    assert message == {"role": "system", "content": "你是助手。\n\n答案只给结论。"}


def test_全部为空报错() -> None:
    with pytest.raises(PromptError):
        _ = system_prompt(None, "  ")
