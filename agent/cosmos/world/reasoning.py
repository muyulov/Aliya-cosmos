"""可运行的世界推理端口（规则基线）。

这是 `WorldReasoningPort` 的最小真实实现：不依赖外部模型，直接从聚合信号
生成 1–3 条假设与行动窗口，用来让世界推理链路在没有 LLM 端口时也能跑通。

它不是语义推理器，定位是**基线/兜底**：部署方可以按同一协议替换成自己的
LLM 端口，`WorldBridgeService` 不需要改。
"""

from __future__ import annotations

import uuid

from agent.cosmos.contracts import (
    ActionWindow,
    HistoryPattern,
    Hypothesis,
    PresenceSignal,
    WorldInference,
    WorldInput,
)


class RuleWorldReasoningPort:
    """基于显式规则的保守推理。

    这里的阈值是**保守先验**，不是校准后的最优值：宁可给出「先观察」，
    也不要在证据不足时建议打扰。桥接层仍会做温度校准与证据接地校验。
    """

    async def infer(self, payload: WorldInput) -> WorldInference:
        # 信号按「状态 → 任务 → 工具 → 历史 → 时间」收集；
        # 顺序只影响可读性，最终置信度取最强假设，不累加。
        signals: list[str] = []
        hypotheses: list[Hypothesis] = []
        windows: list[ActionWindow] = []
        presence = payload.presence

        if presence is not None:
            signals.append("presence")
            self._presence_hypotheses(presence, hypotheses, windows)
        if payload.tasks.running:
            signals.append("tasks")
            labels = "、".join(item.label for item in payload.tasks.running[:3])
            hypotheses.append(
                Hypothesis(
                    statement=f"你手头有任务在跑：{labels}",
                    confidence=0.72,
                    basis="运行中的任务队列",
                    basis_signals=["tasks"],
                )
            )
            windows.append(
                ActionWindow(
                    kind="wait",
                    window="while_running",
                    suggestion="先等任务有进展，不急着追问",
                    rationale="运行中任务会持续产生进度信号",
                )
            )
        if payload.agents.abnormal:
            signals.append("agents")
            names = "、".join(payload.agents.abnormal_agents[:3])
            hypotheses.append(
                Hypothesis(
                    statement=f"有工具状态异常：{names}",
                    confidence=0.68,
                    basis="工具异常计数",
                    basis_signals=["agents"],
                )
            )
            windows.append(
                ActionWindow(
                    kind="observe",
                    window="now",
                    suggestion="先观察异常是否自行恢复",
                    rationale="单次异常不足以判断需要提醒用户",
                )
            )
        if payload.history is not None and payload.history.sample_days >= 3:
            signals.append("history")
            if self._in_active_hours(payload.history, payload.time.now.hour):
                hypotheses.append(
                    Hypothesis(
                        statement="现在通常是你活跃的时段",
                        confidence=0.6,
                        basis="近期活跃小时分布",
                        basis_signals=["history"],
                    )
                )
        if payload.time.period != "unknown":
            signals.append("time")

        # 没有任何可用信号时也返回一条低置信结论：
        # WorldInference 要求至少一个假设，空结果会让桥接层当作失败。
        if not hypotheses:
            signals = signals or ["time"]
            hypotheses.append(
                Hypothesis(
                    statement="当前信号不足，先按最保守的方式处理",
                    confidence=0.3,
                    basis="没有足够的状态或任务信号",
                    basis_signals=[signals[0]],
                )
            )
            windows.append(
                ActionWindow(
                    kind="wait",
                    window="now",
                    suggestion="先不主动推进",
                    rationale="证据不足，等待下一次状态变化",
                )
            )

        # 去重但保留首次出现的顺序，便于 trace 回放时阅读。
        unique_signals = list(dict.fromkeys(signals))
        # 取最强假设而不是平均：一条高置信异常不应被多条弱信号稀释。
        confidence = max(item.confidence for item in hypotheses[:3])
        return WorldInference(
            inference_id=f"inf_rule_{uuid.uuid4().hex[:12]}",
            generated_at=payload.generated_at,
            ttl_seconds=600,
            snapshot=self._snapshot(payload),
            hypotheses=hypotheses[:3],
            action_windows=_dedupe_windows(windows)[:3],
            signals_used=unique_signals,
            confidence=confidence,
        )

    @staticmethod
    def _presence_hypotheses(
        presence: PresenceSignal,
        hypotheses: list[Hypothesis],
        windows: list[ActionWindow],
    ) -> None:
        # 15 分钟 / 30 分钟是保守边界：前者表示「正在聊」，
        # 后者才值得试探性确认；更长静默交给触发层，不在这里猜。
        state = presence.online_state
        if state == "offline":
            hypotheses.append(
                Hypothesis(
                    statement="你可能离线了，先不主动打扰",
                    confidence=0.78,
                    basis="在线状态为 offline",
                    basis_signals=["presence"],
                )
            )
            windows.append(
                ActionWindow(
                    kind="wait",
                    window="later",
                    suggestion="等你重新出现再推进",
                    rationale="离线时主动消息只会变成噪音",
                )
            )
        elif state == "busy":
            hypotheses.append(
                Hypothesis(
                    statement="你可能在忙，先不打断",
                    confidence=0.7,
                    basis="在线状态为 busy",
                    basis_signals=["presence"],
                )
            )
            windows.append(
                ActionWindow(
                    kind="wait",
                    window="later",
                    suggestion="等忙完再问",
                    rationale="busy 状态下的高打扰行动收益低",
                )
            )
        elif state == "away":
            hypotheses.append(
                Hypothesis(
                    statement="你可能暂时离开了",
                    confidence=0.62,
                    basis="在线状态为 away",
                    basis_signals=["presence"],
                )
            )
            windows.append(
                ActionWindow(
                    kind="observe",
                    window="later",
                    suggestion="先观察是否回来",
                    rationale="away 可能是短暂离开",
                )
            )
        elif state == "online":
            if presence.messages_last_15m > 0 or presence.is_typing:
                hypotheses.append(
                    Hypothesis(
                        statement="你正在对话中，适合继续当前话题",
                        confidence=0.75,
                        basis="最近 15 分钟有消息或正在输入",
                        basis_signals=["presence"],
                    )
                )
                windows.append(
                    ActionWindow(
                        kind="observe",
                        window="now",
                        suggestion="顺着当前话题继续",
                        rationale="用户正在输入时不宜切换话题",
                    )
                )
            elif presence.silence_minutes is not None and presence.silence_minutes >= 30:
                hypotheses.append(
                    Hypothesis(
                        statement="你在线但安静了一段时间",
                        confidence=0.58,
                        basis="在线且静默超过 30 分钟",
                        basis_signals=["presence"],
                    )
                )
                windows.append(
                    ActionWindow(
                        kind="check_in",
                        window="later",
                        suggestion="可以轻轻确认一下是否方便",
                        rationale="在线静默可能是专注或走神",
                    )
                )
            else:
                hypotheses.append(
                    Hypothesis(
                        statement="你在线，状态相对平稳",
                        confidence=0.65,
                        basis="在线状态与近期活动",
                        basis_signals=["presence"],
                    )
                )
                windows.append(
                    ActionWindow(
                        kind="observe",
                        window="now",
                        suggestion="保持当前节奏",
                        rationale="没有明显异常信号",
                    )
                )

        if presence.awaiting_result:
            hypotheses.append(
                Hypothesis(
                    statement="你在等一个任务结果",
                    confidence=0.7,
                    basis="任务上下文标记 awaiting_result",
                    basis_signals=["presence"],
                )
            )
        if presence.pending_count > 0:
            hypotheses.append(
                Hypothesis(
                    statement=f"你还有 {presence.pending_count} 件事没处理完",
                    confidence=0.66,
                    basis="待处理任务计数",
                    basis_signals=["presence"],
                )
            )

    @staticmethod
    def _in_active_hours(history: HistoryPattern, hour: int) -> bool:
        return hour in history.active_hours

    @staticmethod
    def _snapshot(payload: WorldInput) -> str:
        parts: list[str] = []
        if payload.presence is not None:
            parts.append(f"在线={payload.presence.online_state}")
            if payload.presence.silence_minutes is not None:
                parts.append(f"静默={payload.presence.silence_minutes}分")
        parts.append(f"运行任务={len(payload.tasks.running)}")
        if payload.agents.abnormal:
            parts.append(f"异常工具={payload.agents.abnormal}")
        return "，".join(parts)[:120]


def _dedupe_windows(windows: list[ActionWindow]) -> list[ActionWindow]:
    seen: set[tuple[str, str]] = set()
    result: list[ActionWindow] = []
    for window in windows:
        key = (window.kind, window.window)
        if key in seen:
            continue
        seen.add(key)
        result.append(window)
    return result


def build() -> RuleWorldReasoningPort:
    """WorldBridgeService 的工厂入口。"""
    return RuleWorldReasoningPort()


__all__ = ["RuleWorldReasoningPort", "build"]
