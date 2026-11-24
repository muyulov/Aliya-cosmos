"""有限时域 EFE 行动选择。

完整 POMDP 需要转移模型、观测模型与目标分布；当前数据只够支撑一个
**小动作集上的有限时域前瞻**。因此这里把问题拆成：

    immediate(a) = 实用(a) + 认识(a) - 成本(a)
    total(a)     = immediate(a) + γ × max_b immediate(b | T(s, a))

- 实用价值：行动窗口自带的 `expected_value` × 行动类型权重；
- 认识价值：信念不确定性 × 该行动的信息增益（观察/确认最高）；
- 打扰成本：不可打扰程度 × 行动侵入性，离线/忙碌时额外加罚；
- `T(s, a)`：显式的轻量信念转移（观察降不确定性、行动升准备度）。

这是**两步前瞻**，不是多步 POMDP 求解；转移函数是手写先验，等有足够
轨迹后可以用 `world_trace` 学习。输出仍是建议，不替 Aliya 做决定。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.cosmos.contracts import (
    ActionChoice,
    ActionKind,
    ActionWindow,
    BeliefState,
    PresenceSignal,
    WorldInference,
)

#: 行动类型的实用权重。数值是手写先验，只用于相对排序，不是学习结果。
ACTION_UTILITY: dict[ActionKind, float] = {
    "wait": 0.10,
    "observe": 0.25,
    "check_in": 0.50,
    "offer_help": 0.60,
    "notify": 0.70,
    "act": 0.80,
}

#: 行动类型的信息增益。观察最高：它直接减少不确定性；行动最低：它假设已经知道该做什么。
INFO_GAIN: dict[ActionKind, float] = {
    "wait": 0.05,
    "observe": 0.80,
    "check_in": 0.65,
    "offer_help": 0.20,
    "notify": 0.15,
    "act": 0.15,
}

#: 行动类型的打扰成本。只与「是否打断」有关，与行动本身的价值无关。
INTERRUPTION_COST: dict[ActionKind, float] = {
    "wait": 0.02,
    "observe": 0.05,
    "check_in": 0.35,
    "offer_help": 0.30,
    "notify": 0.45,
    "act": 0.40,
}

#: 高侵入行动：离线/忙碌时加罚
INTRUSIVE: frozenset[ActionKind] = frozenset({"check_in", "offer_help", "notify", "act"})

#: 同分时优先低打扰行动，避免 EFE 在小数误差下选到更冒进的行动。
TIE_PRIORITY: dict[ActionKind, int] = {
    "wait": 0,
    "observe": 1,
    "check_in": 2,
    "offer_help": 3,
    "notify": 4,
    "act": 5,
}


@dataclass(frozen=True, slots=True)
class _Candidate:
    action: ActionKind
    window: str | None
    expected_value: float


class WorldActionSelector:
    """在候选行动上算有限时域 EFE，返回排序最高的一项。"""

    def __init__(self, *, horizon: int = 2, discount: float = 0.8) -> None:
        if horizon < 1:
            raise ValueError("horizon 至少为 1")
        self._horizon: int = horizon
        self._discount: float = max(0.0, min(1.0, discount))

    def select(
        self,
        inference: WorldInference,
        belief: BeliefState | None,
        presence: PresenceSignal | None,
        confidence: float,
        urgency: float,
        interruptibility: float,
    ) -> ActionChoice:
        candidates = self._candidates(
            inference, belief, presence, confidence, urgency, interruptibility
        )
        scored: list[tuple[float, int, ActionChoice]] = []
        for candidate in candidates:
            pragmatic, epistemic, cost, immediate = self._score(
                candidate, belief, presence, confidence, interruptibility
            )
            belief_after = self._transition(belief, candidate.action)
            lookahead = self._future(
                inference,
                belief_after,
                presence,
                confidence,
                urgency,
                interruptibility,
                depth=self._horizon - 1,
            )
            total = max(-1.0, min(1.0, immediate + self._discount * lookahead))
            choice = ActionChoice(
                action=candidate.action,
                window=candidate.window,
                expected_value=round(candidate.expected_value, 3),
                pragmatic=round(pragmatic, 3),
                epistemic=round(epistemic, 3),
                cost=round(cost, 3),
                efe=round(immediate, 3),
                lookahead=round(lookahead, 3),
                total_efe=round(total, 3),
                rationale=(
                    f"一步 {immediate:.2f} + {self._discount:.2f}×前瞻 "
                    f"{lookahead:.2f} = {total:.2f}"
                ),
            )
            # 第二排序键取负优先级：total 相同时，TIE_PRIORITY 越小越靠前。
            scored.append((total, -TIE_PRIORITY[candidate.action], choice))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return scored[0][2]

    def _future(
        self,
        inference: WorldInference,
        belief: BeliefState | None,
        presence: PresenceSignal | None,
        confidence: float,
        urgency: float,
        interruptibility: float,
        *,
        depth: int,
    ) -> float:
        if depth <= 0:
            # 到达前瞻深度：不再估值，剩余收益交给折扣截断。
            return 0.0
        candidates = self._candidates(
            inference, belief, presence, confidence, urgency, interruptibility
        )
        best = -1.0
        for candidate in candidates:
            _, _, _, immediate = self._score(
                candidate, belief, presence, confidence, interruptibility
            )
            belief_after = self._transition(belief, candidate.action)
            value = immediate + self._discount * self._future(
                inference,
                belief_after,
                presence,
                confidence,
                urgency,
                interruptibility,
                depth=depth - 1,
            )
            best = max(best, value)
        return max(-1.0, min(1.0, best))

    def _score(
        self,
        candidate: _Candidate,
        belief: BeliefState | None,
        presence: PresenceSignal | None,
        confidence: float,
        interruptibility: float,
    ) -> tuple[float, float, float, float]:
        pragmatic = min(
            1.0,
            candidate.expected_value * ACTION_UTILITY[candidate.action]
            + (confidence * 0.10 if candidate.action in {"offer_help", "act"} else 0.0),
        )
        epistemic = min(
            1.0,
            (belief.uncertainty if belief is not None else 0.5) * INFO_GAIN[candidate.action],
        )
        cost = self._cost(candidate.action, presence, interruptibility)
        immediate = max(-1.0, min(1.0, pragmatic + epistemic - cost))
        return pragmatic, epistemic, cost, immediate

    def _candidates(
        self,
        inference: WorldInference,
        belief: BeliefState | None,
        presence: PresenceSignal | None,
        confidence: float,
        urgency: float,
        interruptibility: float,
    ) -> list[_Candidate]:
        # 没有显式 expected_value 时，用「置信度 × 紧迫度 × 可打扰度」做保守基线。
        # 两个 max(..., 0.1) 防止任一维度为 0 时把候选价值直接抹平。
        base_value = confidence * max(urgency, 0.1) * max(interruptibility, 0.1)
        by_action: dict[ActionKind, _Candidate] = {}
        for window in inference.action_windows:
            action = self._kind_of(window)
            value = window.expected_value if window.expected_value is not None else base_value
            current = by_action.get(action)
            if current is None or value > current.expected_value:
                by_action[action] = _Candidate(action, window.window, value)

        uncertainty = belief.uncertainty if belief is not None else 0.5
        readiness = belief.readiness if belief is not None else 0.4
        online = presence is not None and presence.online_state in {"online", "away"}

        # 补默认候选时同时做门槛判断：不确定性不够高就不建议确认，
        # 用户不在线就不建议主动帮忙/打扰，避免候选集本身诱导冒进。
        defaults: list[tuple[ActionKind, float, bool]] = [
            ("wait", max(0.05, base_value * 0.4), True),
            ("observe", 0.20, uncertainty >= 0.35),
            ("check_in", 0.35, uncertainty >= 0.35 and online),
            ("offer_help", 0.45, readiness >= 0.5 and online),
            ("notify", 0.50, urgency >= 0.5 and interruptibility >= 0.3),
            ("act", 0.55, readiness >= 0.65 and interruptibility >= 0.4),
        ]
        for action, value, allowed in defaults:
            if not allowed or action in by_action:
                continue
            by_action[action] = _Candidate(action, None, min(1.0, value))
        if not by_action:
            by_action["wait"] = _Candidate("wait", None, 0.1)
        return list(by_action.values())

    @staticmethod
    def _transition(belief: BeliefState | None, action: ActionKind) -> BeliefState | None:
        """手写轻量转移：观察降不确定性，行动升准备度。

        没有观测模型，所以这里只更新信念的连续分量，不更新在线概率。
        """
        if belief is None:
            return None
        # 转移量是粗粒度先验：观察/确认显著降不确定性，行动升准备度，
        # 等待则让不确定性缓慢回升。没有观测模型，所以不更新 online_prob。
        uncertainty = belief.uncertainty
        readiness = belief.readiness
        if action == "wait":
            uncertainty += 0.03
            readiness -= 0.03
        elif action == "observe":
            uncertainty -= 0.10
        elif action == "check_in":
            uncertainty -= 0.20
            readiness += 0.05
        elif action == "offer_help":
            uncertainty -= 0.05
            readiness += 0.15
        elif action == "notify":
            uncertainty -= 0.02
            readiness += 0.05
        elif action == "act":
            uncertainty += 0.05
            readiness += 0.20
        return belief.model_copy(
            update={
                "uncertainty": _clamp(uncertainty),
                "readiness": _clamp(readiness),
            }
        )

    @staticmethod
    def _kind_of(window: ActionWindow) -> ActionKind:
        if window.kind != "wait":
            return window.kind
        text = f"{window.suggestion} {window.rationale}"
        if any(keyword in text for keyword in ("确认", "问一下", "询问", "检查", "看看")):
            return "check_in"
        if any(keyword in text for keyword in ("提醒", "通知", "告诉")):
            return "notify"
        if any(keyword in text for keyword in ("帮", "建议", "提供")):
            return "offer_help"
        if any(keyword in text for keyword in ("观察", "留意", "等")):
            return "observe"
        if any(keyword in text for keyword in ("执行", "处理", "开始", "推进")):
            return "act"
        return "wait"

    @staticmethod
    def _cost(
        action: ActionKind,
        presence: PresenceSignal | None,
        interruptibility: float,
    ) -> float:
        cost = (1.0 - interruptibility) * INTERRUPTION_COST[action]
        if (
            presence is not None
            and presence.online_state in {"offline", "busy"}
            and action in INTRUSIVE
        ):
            # 离线/忙碌时，高侵入行动不只按比例加价，再额外加一笔固定罚分。
            cost += 0.20
        return min(1.0, max(0.0, cost))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


__all__ = ["ACTION_UTILITY", "WorldActionSelector"]
