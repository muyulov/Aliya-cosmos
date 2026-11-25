"""出站检查：命中规则先重写一次，仍不通过就遮蔽后发送。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_RULES = {
    "narration": re.compile(r"[（(][^）)]{1,80}[）)]|\*[^*]{1,80}\*"),
    "engineering_terms": re.compile(r"玩家|用户|agent|模型|提示词|调度|面板|节点"),
    "internal_ids": re.compile(r"\b(?:tsk|dsp|evt|ses|req)_[A-Za-z0-9]+\b"),
    "presence_leak": re.compile(r"检测到|系统显示|状态显示|readable"),
}


@dataclass(frozen=True, slots=True)
class CheckResult:
    text: str
    rules: tuple[str, ...]


def check(text: str) -> CheckResult:
    if not text.strip():
        return CheckResult(text="", rules=("empty",))
    rules = tuple(name for name, pattern in _RULES.items() if pattern.search(text))
    masked = _RULES["internal_ids"].sub("那件事", text)
    return CheckResult(text=masked, rules=rules)
