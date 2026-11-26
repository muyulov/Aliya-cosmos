"""自主行动三层门控：配置 → 链路/状态 → 语义时间 → 间隔与日限额。"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast

from agent.cosmos.contracts import LinkState, ReadablePresence
from core.config.settings import AgentAliyaInitiativeSettings

#: 用户安静超过两小时才考虑主动开口。
LONG_GAP_SECONDS = 2 * 3600

_HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:个)?小时")
_INSOMNIA_WORDS = ("失眠", "睡不着", "熬夜")
_OPEN_TOPIC_WORDS = ("未收尾", "没说完", "没说出口", "话题")
_REPAIR_WORDS = ("修好", "修完", "刚修", "被夸", "搞定")
_LATE_NIGHT_WORDS = ("深夜", "刷信息", "刷手机", "刷到")
_REPAIR_SIGNALS = ("修好", "修完", "搞定", "完成", "成功", "恢复")
_BROWSING_SIGNALS = ("刷", "信息", "新闻", "视频", "浏览", "动态")
#: 这些词只用于组句，不参与自定义条件的字面匹配。
_STOP_TERMS = {
    "对方",
    "现在",
    "可以",
    "说话",
    "超过",
    "小时",
    "间隔",
    "如果",
    "已经",
    "时候",
    "东西",
    "想被",
    "想被夸",
    "刷到",
}


@dataclass(frozen=True, slots=True)
class InitiativeDecision:
    should_speak: bool
    reason: str


@dataclass(frozen=True, slots=True)
class TimeSignals:
    """时间层可用的语义信号；文本均来自运行时已整理过的上下文。"""

    now: datetime
    last_user_at: datetime | None = None
    open_topics: Sequence[str] = ()
    world: str = ""
    desktop: str = ""
    presence: str = ""
    world_at: datetime | None = None
    desktop_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PreferenceDecision:
    matched: bool
    condition: str = ""
    reason: str = ""


class PreferenceGate:
    """把自由文本 `prefer_when` 落到可解释的语义信号上。

    优先识别人设里已有的四类动机：失眠、未收尾话题、刚修好东西、深夜刷信息；
    未识别的自定义条件退化为与当前上下文的关键词匹配，避免配置了却永远不生效。
    """

    def __init__(self, conditions: Sequence[str]) -> None:
        self._conditions: list[str] = [item.strip() for item in conditions if item.strip()]

    def match(self, signals: TimeSignals) -> PreferenceDecision:
        if not self._conditions:
            return PreferenceDecision(True, reason="no_prefer_when")
        context = self._context(signals)
        gap_hours = _hours_since(signals.now, signals.last_user_at)
        for condition in self._conditions:
            reason = self._match_one(condition, context, gap_hours, signals)
            if reason is not None:
                return PreferenceDecision(True, condition, reason)
        return PreferenceDecision(False, reason="no_prefer_when")

    def _match_one(
        self,
        condition: str,
        context: str,
        gap_hours: float,
        signals: TimeSignals,
    ) -> str | None:
        recognized = False
        if _has_any(condition, _OPEN_TOPIC_WORDS):
            recognized = True
            threshold = _hours_from(condition, default=0.0)
            if signals.open_topics and gap_hours >= threshold:
                return "open_topic"
        if _has_any(condition, _REPAIR_WORDS):
            recognized = True
            recent = _is_recent_context(signals.now, signals.world_at, signals.desktop_at)
            if recent and _has_any(context, _REPAIR_SIGNALS):
                return "repair"
        if _has_any(condition, _INSOMNIA_WORDS):
            recognized = True
            threshold = _hours_from(condition, default=0.0)
            if gap_hours >= threshold and (
                _is_night(signals.now) or _has_any(context, _INSOMNIA_WORDS)
            ):
                return "insomnia"
        if _has_any(condition, _LATE_NIGHT_WORDS):
            recognized = True
            if _is_night(signals.now) and (
                _has_any(context, _BROWSING_SIGNALS) or "刷信息" in condition
            ):
                return "late_night"
        # 已识别为已知动机但信号不满足时，不再退化成字面匹配，避免“刚修好”这类词自证。
        if recognized:
            return None
        if _lexical_match(condition, context):
            return "lexical"
        return None

    @staticmethod
    def _context(signals: TimeSignals) -> str:
        return "\n".join(
            item
            for item in (
                signals.world,
                signals.desktop,
                signals.presence,
                *signals.open_topics,
            )
            if item.strip()
        )


def _has_any(text: str, words: Sequence[str]) -> bool:
    return any(word in text for word in words)


def _hours_from(text: str, *, default: float) -> float:
    matched = _HOURS.search(text)
    return float(matched.group(1)) if matched is not None else default


def _hours_since(now: datetime, at: datetime | None) -> float:
    if at is None:
        return math.inf
    try:
        return max(0.0, (now - at).total_seconds() / 3600)
    except TypeError:
        return math.inf


def _is_recent_context(now: datetime, *ats: datetime | None, hours: float = 2.0) -> bool:
    """没有时间戳时以当前上下文为准；有旧时间戳才明确拒绝。"""
    timestamps = [at for at in ats if at is not None]
    return not timestamps or any(_hours_since(now, at) <= hours for at in timestamps)


def _is_night(now: datetime) -> bool:
    return now.hour >= 23 or now.hour < 5


def _lexical_match(condition: str, context: str) -> bool:
    """自定义条件的兜底：用 2–4 字中文片段做保守匹配。"""
    for chunk in cast("list[str]", re.findall(r"[\u4e00-\u9fff]{2,}", condition)):
        for size in range(2, min(4, len(chunk)) + 1):
            for start in range(0, len(chunk) - size + 1):
                term = chunk[start : start + size]
                if term not in _STOP_TERMS and term in context:
                    return True
    return any(
        word.lower() in context.lower()
        for word in cast("list[str]", re.findall(r"[A-Za-z]{3,}", condition))
    )


class Initiative:
    def __init__(self, config: AgentAliyaInitiativeSettings) -> None:
        self._config: AgentAliyaInitiativeSettings = config
        self._last_proactive_at: datetime | None = None
        self._daily_count: int = 0
        self._day: date | None = None
        self._unanswered: int = 0
        self._awaiting_reply: bool = False

    def allows(
        self,
        *,
        now: datetime,
        presence: ReadablePresence | None,
        link: LinkState,
    ) -> InitiativeDecision:
        """只检查「现在能不能打扰」：配置、链路、状态、间隔与日限额。

        不含长期沉默与默认动作：到期跟进要绕过时间条件，但仍受打扰预算约束。
        """
        if not self._config.enabled:
            return InitiativeDecision(False, "disabled")
        if self._config.respect_link_state and link != "up":
            return InitiativeDecision(False, "link_down")
        if (
            self._config.respect_presence_hint
            and presence is not None
            and presence.tone in {"away", "waiting"}
        ):
            return InitiativeDecision(False, "presence")
        if self._last_proactive_at is not None:
            elapsed = (now - self._last_proactive_at).total_seconds()
            multiplier = 2 if self._unanswered >= 2 else 1
            if elapsed < self._config.min_gap_seconds * multiplier:
                return InitiativeDecision(False, "min_gap")
        if self._daily_count >= self._config.daily_soft_limit:
            return InitiativeDecision(False, "daily_limit")
        return InitiativeDecision(True, "allowed")

    def should_speak(
        self,
        *,
        now: datetime,
        presence: ReadablePresence | None,
        link: LinkState,
        last_user_at: datetime | None,
    ) -> InitiativeDecision:
        allowed = self.allows(now=now, presence=presence, link=link)
        if not allowed.should_speak:
            return allowed
        if last_user_at is None or (now - last_user_at).total_seconds() >= LONG_GAP_SECONDS:
            return InitiativeDecision(True, "long_gap")
        if self._config.default_action == "speak":
            return InitiativeDecision(True, "default_speak")
        return InitiativeDecision(False, "default_wait")

    def record_proactive(self, now: datetime) -> None:
        """主动消息真正发出后调用；跨天重置日计数。"""
        if self._awaiting_reply:
            self._unanswered += 1
        self._awaiting_reply = True
        if self._day != now.date():
            self._day = now.date()
            self._daily_count = 0
        self._daily_count += 1
        self._last_proactive_at = now

    def record_user_reply(self) -> None:
        """用户回话后重置间隔与连续未回应计数。"""
        self._last_proactive_at = None
        self._unanswered = 0
        self._awaiting_reply = False
