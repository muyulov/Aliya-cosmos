"""世界推理的触发门控。

借鉴 TGL 的思路：把「何时唤醒」从深度推理路径上移走。这里先用规则打分，
不引入可训练模型；分数只决定「现在推不推、多久后再推」，不决定推什么。

规则是显式的、可回放的：每条都对应一个可观测事件，阈值放在配置里，
后续可以用 `world_trace.jsonl` 离线调参，或替换成轻量分类器。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent.cosmos.world.classifier import TriggerClassifier, event_features
from core.config import AgentWorldSettings
from core.logger import log

#: 触发来源
type TriggerKind = Literal[
    "tick", "presence", "dispatch", "silence", "manual", "user_message", "mood", "link"
]


@dataclass(frozen=True, slots=True)
class TriggerScore:
    """一次触发的显著性。"""

    salience: float
    reason: str


class WorldTriggerPolicy:
    """把事件映射成显著性分数与动态合并窗口。"""

    def __init__(self, settings: AgentWorldSettings) -> None:
        self._settings: AgentWorldSettings = settings
        self._classifier: TriggerClassifier | None = self._load_classifier()

    def _load_classifier(self) -> TriggerClassifier | None:
        """有训练好的权重就用分类器；文件坏了退回规则，不阻断启动。"""
        path = self._settings.triggers.model_path
        if not path:
            return None
        try:
            return TriggerClassifier.from_path(Path(path))
        except (OSError, ValueError, KeyError) as exc:
            log.warning("触发分类器加载失败，退回规则打分", 路径=path, 原因=str(exc))
            return None

    @staticmethod
    def features(kind: TriggerKind, payload: dict[str, object] | None = None) -> dict[str, float]:
        """事件特征；训练与运行时共用同一套定义。"""
        return event_features(kind, payload)

    def score(
        self, kind: TriggerKind, payload: dict[str, object] | None = None
    ) -> TriggerScore | None:
        """返回显著性；低于阈值返回 None，表示不值得推演。

        有分类器权重时用 `sigmoid(w·x)` 代替规则分数；手动触发永远放行。
        """
        if kind == "manual":
            return TriggerScore(1.0, "手动刷新")
        if kind == "tick":
            # 定时对账永远走低显著路径，避免分类器把兜底变成主触发
            raw_tick = self._raw("tick", payload or {})
            if raw_tick is None or raw_tick.salience < self._settings.triggers.min_salience:
                return None
            return raw_tick
        if self._classifier is not None and not self._classifier.degenerate:
            probability = self._classifier.probability(self.features(kind, payload))
            if probability < self._settings.triggers.min_salience:
                return None
            return TriggerScore(probability, "分类器")
        raw = self._raw(kind, payload or {})
        if raw is None:
            return None
        if raw.salience < self._settings.triggers.min_salience:
            return None
        return raw

    def window_seconds(self, salience: float) -> float:
        """高显著事件用短窗口，低显著事件用长窗口。

        在 `[min_salience, 0.8]` 之间线性插值：越接近 0.8 越接近短窗口；
        超过 0.8 直接取短窗口，避免高显著事件被长窗口压掉。
        """
        high = self._settings.triggers.high_salience_window_seconds
        low = self._settings.triggers.low_salience_window_seconds
        floor = self._settings.triggers.min_salience
        if salience >= 0.8 or floor >= 0.8:
            return high
        ratio = max(0.0, min(1.0, (salience - floor) / (0.8 - floor)))
        return low - (low - high) * ratio

    def dedupe_key(self, kind: TriggerKind, payload: dict[str, object] | None = None) -> str:
        """同一实体在短时间内的重复事件只算一次。"""
        # 只按「事件类型 + 实体」去重，不把状态值放进 key：
        # 在线→离线→在线这种真实翻转不应被当成重复事件吞掉。
        entity = ""
        if payload:
            entity = str(payload.get("entity") or payload.get("agent_id") or "")
        return f"{kind}:{entity}"

    @staticmethod
    def _raw(kind: TriggerKind, payload: dict[str, object]) -> TriggerScore | None:
        if kind == "manual":
            return TriggerScore(1.0, "手动刷新")
        if kind == "presence":
            state = str(payload.get("state") or "unknown")
            # 离线最高：直接改变「能不能说话」；busy 反而低于 online，
            # 因为忙碌仍可能愿意收一条低打扰消息。
            mapping = {
                "offline": 0.95,
                "away": 0.8,
                "online": 0.85,
                "busy": 0.6,
                "unknown": 0.3,
            }
            return TriggerScore(mapping.get(state, 0.3), f"在线状态变为 {state}")
        if kind == "silence":
            minutes = _as_int(payload.get("minutes"))
            # 30 分钟起步 0.73，静默越久越接近 1；900 秒是归一化尺度，
            # 让 30/120/360 分钟落在可比较的区间内。
            salience = min(1.0, 0.7 + minutes / 900)
            return TriggerScore(salience, f"静默跨过 {minutes} 分钟")
        if kind == "dispatch":
            status = str(payload.get("status") or "ok")
            # 失败最值得复盘；成功也需要一次轻量收尾，所以 0.5 不是 0。
            mapping = {"failed": 0.9, "partial": 0.75, "cancelled": 0.6, "ok": 0.5}
            return TriggerScore(mapping.get(status, 0.5), f"任务结束：{status}")
        if kind == "link":
            state = str(payload.get("state") or "up")
            # 恢复（up）比断开（down）更值得推：断开时做什么都不可达，
            # 恢复后要判断积压的消息现在要不要补发。
            mapping = {"up": 0.75, "unstable": 0.5, "down": 0.4}
            return TriggerScore(mapping.get(state, 0.4), f"链路变为 {state}")
        if kind == "user_message":
            return TriggerScore(0.7, "用户发来消息")
        if kind == "mood":
            delta = _as_float(payload.get("delta"))
            return TriggerScore(min(1.0, 0.6 + abs(delta)), f"情绪变化 {delta:.2f}")
        if kind == "tick":
            return TriggerScore(0.2, "定时对账")
        return None  # pyright: ignore[reportUnreachable]


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


__all__ = ["TriggerKind", "TriggerScore", "WorldTriggerPolicy"]
