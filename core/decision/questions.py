"""三原语构造器。

约定：
- 调用方只碰这里的构造器，不手拼协议字典。
- 每个构造器返回**具体的 TypedDict**（不是 `Question` 联合体）：
  联合体会放过拼错的键，具体类型才能在构造处报错。
- 构造器只做「填好字面量」，不做上限校验：选项数（Jev 255 / Laya 100）、
  score 级数（2–10）由端点裁决，本地按任一家的规则拦都会误伤另一家。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, Required, TypedDict


class ChoiceQuestion(TypedDict):
    """从离散选项里挑一个。criteria 的值是选项的评分说明，`None` 表示不需要说明。"""

    type: Literal["choice"]
    instructions: str
    criteria: dict[str, str | None]


class ScoreQuestion(TypedDict):
    """在有序档位上打分：criteria 的顺序就是级序。"""

    type: Literal["score"]
    instructions: str
    criteria: list[str]


class NoulCriteria(TypedDict):
    """是非题的两个语义槽：只能用 true / false 这两个键。"""

    true: str
    false: str


class NoulQuestion(TypedDict, total=False):
    """是非题：返回 P(true)。`criteria` 可选，给了就必须两个槽都填。"""

    type: Required[Literal["noul"]]
    instructions: Required[str]
    criteria: NoulCriteria


#: 一次请求里 questions 的值：三原语可以混在同一趟里问完
type Question = ChoiceQuestion | ScoreQuestion | NoulQuestion


def choice(instructions: str, criteria: Mapping[str, str | None]) -> ChoiceQuestion:
    """选一个：criteria 的键就是候选选项。"""
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": dict(criteria),
    }


def score(instructions: str, criteria: Sequence[str]) -> ScoreQuestion:
    """打分：criteria 的顺序即级序，最低档在前。"""
    return {"type": "score", "instructions": instructions, "criteria": list(criteria)}


def noul(instructions: str, *, true: str | None = None, false: str | None = None) -> NoulQuestion:
    """是非题：两个语义槽都给才带 criteria，只给一个等于给一半。"""
    question = NoulQuestion(type="noul", instructions=instructions)
    if true is not None and false is not None:
        question["criteria"] = NoulCriteria(true=true, false=false)
    return question
