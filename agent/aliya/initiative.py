"""自主行动三层门控：配置 → 链路/状态 → 间隔与日限额。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from agent.cosmos.contracts import LinkState, ReadablePresence
from core.config.settings import AgentAliyaInitiativeSettings

#: 用户安静超过两小时才考虑主动开口。
LONG_GAP_SECONDS = 2 * 3600


@dataclass(frozen=True, slots=True)
class InitiativeDecision:
    should_speak: bool
    reason: str


class Initiative:
    def __init__(self, config: AgentAliyaInitiativeSettings) -> None:
        self._config = config
        self._last_proactive_at: datetime | None = None
        self._daily_count = 0
        self._day: date | None = None

    def should_speak(
        self,
        *,
        now: datetime,
        presence: ReadablePresence | None,
        link: LinkState,
        last_user_at: datetime | None,
    ) -> InitiativeDecision:
        if not self._config.enabled:
            return InitiativeDecision(False, "disabled")
        if link != "up":
            return InitiativeDecision(False, "link_down")
        if presence is not None and presence.tone in {"away", "waiting"}:
            return InitiativeDecision(False, "presence")
        if self._last_proactive_at is not None:
            elapsed = (now - self._last_proactive_at).total_seconds()
            if elapsed < self._config.min_gap_seconds:
                return InitiativeDecision(False, "min_gap")
        if self._daily_count >= self._config.daily_soft_limit:
            return InitiativeDecision(False, "daily_limit")
        if last_user_at is None or (now - last_user_at).total_seconds() >= LONG_GAP_SECONDS:
            return InitiativeDecision(True, "long_gap")
        return InitiativeDecision(False, "default_wait")

    def record_proactive(self, now: datetime) -> None:
        """主动消息真正发出后调用；跨天重置日计数。"""
        if self._day != now.date():
            self._day = now.date()
            self._daily_count = 0
        self._daily_count += 1
        self._last_proactive_at = now

    def record_user_reply(self) -> None:
        """用户回话后重置间隔，下一次长安静可以重新考虑开口。"""
        self._last_proactive_at = None
