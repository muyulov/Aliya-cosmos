"""判断层对外出口。

用法：
    from core.decision import DecisionService, choice, noul, score

    result = await svc.predict("这条消息是否要求退款？", {"退款": noul("是否要求退款？")})

两家的协议都是同一套 /systemone，laya 与 jev 只是两个端点，用 backend= 选。
"""

from core.decision.questions import (
    ChoiceQuestion,
    NoulCriteria,
    NoulQuestion,
    Question,
    ScoreQuestion,
    choice,
    noul,
    score,
)

__all__ = [
    "ChoiceQuestion",
    "NoulCriteria",
    "NoulQuestion",
    "Question",
    "ScoreQuestion",
    "choice",
    "noul",
    "score",
]
