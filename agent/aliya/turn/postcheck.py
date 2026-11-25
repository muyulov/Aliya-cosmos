"""出站检查：命中规则先重写一次，仍不通过就遮蔽后发送。"""

from __future__ import annotations

import re
from collections.abc import Sequence
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


def check(text: str, rules: Sequence[str] | None = None) -> CheckResult:
    """按配置选择检查项；`rules=None` 表示启用全部规则。"""
    if not text.strip():
        return CheckResult(text="", rules=("empty",))
    selected = set(_RULES) if rules is None else {name for name in rules if name in _RULES}
    hits = tuple(
        name
        for name, pattern in _RULES.items()
        if name in selected and pattern.search(text)
    )
    masked = text
    if "internal_ids" in selected:
        masked = _RULES["internal_ids"].sub("那件事", masked)
    for name in ("narration", "presence_leak", "engineering_terms"):
        if name in selected:
            masked = _RULES[name].sub("", masked)
    masked = re.sub(r"[ \t]{2,}", " ", masked).strip()
    return CheckResult(text=masked, rules=hits)
