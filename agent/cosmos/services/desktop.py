"""Agent 桌面：布局、排序、可解读状态与事件刷新。

三层刷新（设计 03 章 6 节）：

- 事件驱动：`agent.state.changed` / `agent.result` 立即刷新；
- 节流合并：`agent.progress` 按 `progress_throttle_ms` 做尾沿节流；
- 全量对账：`reconcile_seconds` 定时器，只在真有差异时发事件。

布局偏好持久化到 `contract.state_dir/desktop.json`，写盘用「临时文件 + replace」。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import ClassVar, cast, override

from agent.cosmos.contracts import (
    AgentCard,
    AgentDetail,
    AgentStatus,
    AgentTaskView,
    DesktopEntry,
    DesktopGroup,
    DesktopLayoutPatch,
    DesktopSnapshot,
    ReadableState,
    RuntimeStats,
)
from agent.cosmos.services.registry import AgentRegistryService
from agent.cosmos.services.runtime import AgentRuntimeService
from agent.cosmos.transport import BusService
from core.config import AgentSettings
from core.logger import log
from core.service import ClockService, Service

#: 空桌面的整体摘要
EMPTY_SUMMARY = "还没有可用的工具"
#: 「刚完成」的判定窗口（03 章 4.1）
RECENT_SECONDS = 600
#: 偏慢的阈值：本次耗时 / 历史 P50
SLOW_RATIO = 2.0


def _relative(seconds: float) -> str:
    """把秒数翻成「刚刚 / N 分钟前」这类相对时间。"""
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} 分钟前"
    if seconds < 86400:
        return f"{max(1, int(seconds // 3600))} 小时前"
    return f"{max(1, int(seconds // 86400))} 天前"


class DesktopService(Service):
    """维护 agent 桌面快照与布局。"""

    name: ClassVar[str] = "cosmos.desktop"
    dependencies: ClassVar[tuple[type[Service], ...]] = (
        AgentRegistryService,
        AgentRuntimeService,
        BusService,
        ClockService,
    )

    def __init__(
        self,
        registry: AgentRegistryService,
        runtime: AgentRuntimeService,
        bus: BusService,
        clock: ClockService,
        config: AgentSettings,
    ) -> None:
        super().__init__()
        self._registry: AgentRegistryService = registry
        self._runtime: AgentRuntimeService = runtime
        self._bus: BusService = bus
        self._clock: ClockService = clock
        self._config: AgentSettings = config
        self._state_dir: Path = Path(config.cosmos.contract.state_dir)
        self._layout_path: Path = self._state_dir / "desktop.json"
        self._revision: int = 0
        self._order: list[str] = []
        self._move: dict[str, str] = {}
        self._pin: dict[str, bool] = {}
        self._collapse: dict[str, bool] = {}
        self._active: bool = False
        self._events_task: asyncio.Task[None] | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
        self._refresh_handle: asyncio.TimerHandle | None = None
        self._pending_refresh: bool = False
        self._background: set[asyncio.Task[object]] = set()
        self._last_fingerprint: tuple[tuple[str, str, float | None], ...] = ()

    # ---------- 生命周期 ----------

    @override
    async def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._load_layout()
        self._events_task = asyncio.create_task(self._consume_events())
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    @override
    async def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        if self._refresh_handle is not None:
            self._refresh_handle.cancel()
            self._refresh_handle = None
        for task in (self._events_task, self._reconcile_task):
            if task is not None:
                _ = task.cancel()
        for task in list(self._background):
            _ = task.cancel()
        tasks = [
            task
            for task in (self._events_task, self._reconcile_task, *self._background)
            if task is not None
        ]
        if tasks:
            _ = await asyncio.gather(*tasks, return_exceptions=True)
        self._events_task = None
        self._reconcile_task = None
        self._background.clear()

    # ---------- 查询 ----------

    async def describe(self) -> DesktopSnapshot:
        """全量快照：分组 + 按展示顺序排列的条目。"""
        entries = [self._entry(card) for card in self._registry.list()]
        entries.sort(key=self._sort_key)
        groups = self._groups()
        return DesktopSnapshot(
            revision=self._revision,
            generated_at=self._clock.now(),
            groups=groups,
            entries=entries,
            summary=self._summary(entries),
        )

    async def get(self, agent_id: str) -> AgentDetail:
        """单卡详情：卡片 + 运行态 + 最近任务。"""
        card = self._registry.get(agent_id)
        status = self._status(card)
        return AgentDetail(
            card=card,
            status=status,
            task=self._runtime.task_view(agent_id),
            readable=self.readable(agent_id),
            recent_tasks=await self._runtime.list_tasks(
                agent_id, limit=self._config.cosmos.desktop.history_limit
            ),
        )

    async def patch_layout(self, patch: DesktopLayoutPatch) -> DesktopSnapshot:
        """应用布局补丁；内容不变时不涨 revision、不发事件。"""
        before = self._layout_dict()
        if patch.reorder is not None:
            self._order = list(patch.reorder)
        if patch.move is not None:
            self._move.update(patch.move)
        if patch.pin is not None:
            self._pin.update(patch.pin)
        if patch.collapse is not None:
            self._collapse.update(patch.collapse)
        after = self._layout_dict()

        if after != before:
            self._revision += 1
            self._persist_layout()
            snapshot = await self.describe()
            _ = await self._bus.events.publish("desktop.layout.changed", snapshot)
            return snapshot
        return await self.describe()

    def readable(self, agent_id: str) -> ReadableState:
        """懒派生可解读状态：按设计 03 章 4.1 的优先级表取第一条。"""
        card = self._registry.get(agent_id)
        if not self._registry.enabled(agent_id):
            return ReadableState(summary="已停用", tone="muted", hint="需要在配置里启用")

        state = self._runtime.agent_state(agent_id)
        stats = self._runtime.stats(agent_id)
        task = self._runtime.task_view(agent_id)
        step = (task.step or "处理") if task is not None else "处理"
        error = state.last_error

        if state.status == "abnormal" and stats.error_streak >= 2:
            return ReadableState(
                summary=f"连续 {stats.error_streak} 次失败",
                tone="alarming",
                hint="建议先看错误详情再重试",
            )
        if state.status == "abnormal" and error is not None and error.code == "timeout":
            timeout = card.execution.timeout_seconds
            return ReadableState(
                summary=f"上次任务超时（超过 {timeout:g} 秒）",
                tone="alarming",
                hint="可以拆小任务后重试",
            )
        if state.status == "abnormal":
            reason = error.code if error is not None else "未知原因"
            return ReadableState(summary=f"上次任务失败：{reason}", tone="alarming", hint="可重试")
        if state.status == "stalled":
            return ReadableState(
                summary=f"{self._stalled_seconds(agent_id)} 秒没有心跳，可能卡住",
                tone="strained",
                hint="可取消后重新提交",
            )
        if state.status == "running":
            ratio = self._duration_ratio(stats, task)
            if ratio is not None and ratio > SLOW_RATIO:
                return ReadableState(
                    summary=f"正在{step}，比平时慢",
                    tone="strained",
                    hint="暂无建议，可继续等待",
                )
            if task is not None and task.progress is not None:
                return ReadableState(
                    summary=f"正在{step}，进度 {task.progress:.0f}%",
                    tone="busy",
                )
            return ReadableState(summary=f"正在{step}", tone="busy")
        if state.status == "queued":
            return ReadableState(summary=f"有 {state.queued} 个任务排队", tone="busy")
        if state.last_used_at is None:
            return ReadableState(summary="空闲，还没用过", tone="calm")
        elapsed = (self._clock.now() - state.last_used_at).total_seconds()
        if elapsed < RECENT_SECONDS:
            label = task.label if task is not None else "任务"
            return ReadableState(summary=f"空闲，刚完成一次{label}", tone="calm")
        return ReadableState(summary=f"空闲，最近使用在 {_relative(elapsed)}", tone="calm")

    # ---------- 内部：条目 ----------

    def _entry(self, card: AgentCard) -> DesktopEntry:
        return DesktopEntry(
            card=card,
            status=self._status(card),
            task=self._runtime.task_view(card.id),
            readable=self.readable(card.id),
            badge=card.desktop.badge,
        )

    def _status(self, card: AgentCard) -> AgentStatus:
        if not self._registry.enabled(card.id):
            return "disabled"
        return self._runtime.agent_state(card.id).status

    def _group_of(self, card: AgentCard) -> str:
        return self._move.get(card.id, card.desktop.group)

    def _pinned(self, card: AgentCard) -> bool:
        return self._pin.get(card.id, card.desktop.pinned)

    def _order_of(self, card: AgentCard) -> int:
        if card.id in self._order:
            return self._order.index(card.id)
        return card.desktop.order

    def _sort_key(self, entry: DesktopEntry) -> tuple[int, int, int, str]:
        card = entry.card
        group_order = {group.key: group.order for group in self._config.cosmos.desktop.groups}
        return (
            group_order.get(self._group_of(card), 100),
            0 if self._pinned(card) else 1,
            self._order_of(card),
            card.id,
        )

    def _groups(self) -> list[DesktopGroup]:
        declared = sorted(self._config.cosmos.desktop.groups, key=lambda group: group.order)
        groups = [
            DesktopGroup(
                key=group.key,
                title=group.title,
                order=group.order,
                collapsed=self._collapse.get(group.key, False),
            )
            for group in declared
        ]
        known = {group.key for group in groups}
        # 运行期注册可能带来未声明的分组：补一个，不让条目消失
        for card in self._registry.list():
            key = self._group_of(card)
            if key not in known:
                groups.append(
                    DesktopGroup(
                        key=key,
                        title=key,
                        order=100,
                        collapsed=self._collapse.get(key, False),
                    )
                )
                known.add(key)
        return groups

    def _summary(self, entries: list[DesktopEntry]) -> str:
        if not entries:
            return EMPTY_SUMMARY
        idle = sum(1 for entry in entries if entry.status == "idle")
        busy = sum(1 for entry in entries if entry.status in {"running", "queued", "stalled"})
        abnormal = sum(1 for entry in entries if entry.status == "abnormal")
        parts = [f"{len(entries)} 个工具"]
        if idle:
            parts.append(f"{idle} 个空闲")
        if busy:
            parts.append(f"{busy} 个正在忙")
        if abnormal:
            parts.append(f"{abnormal} 个出过问题")
        return "，".join(parts)

    def _stalled_seconds(self, agent_id: str) -> int:
        state = self._runtime.agent_state(agent_id)
        if state.last_heartbeat_at is None:
            return 0
        return max(0, int((self._clock.now() - state.last_heartbeat_at).total_seconds()))

    @staticmethod
    def _duration_ratio(stats: RuntimeStats, task: AgentTaskView | None) -> float | None:
        if stats.p50_seconds is None or stats.p50_seconds <= 0 or task is None:
            return None
        return task.elapsed_seconds / stats.p50_seconds

    # ---------- 内部：事件与刷新 ----------

    async def _consume_events(self) -> None:
        topics = ["agent.state.changed", "agent.progress", "agent.result"]
        async for event in self._bus.events.subscribe(topics):
            if event.topic == "agent.progress":
                self._schedule_throttled_refresh()
            else:
                self._spawn(self._publish_snapshot())

    def _schedule_throttled_refresh(self) -> None:
        """尾沿节流：窗口内的多次进度只发一次。"""
        self._pending_refresh = True
        if self._refresh_handle is not None:
            return
        delay = self._config.cosmos.desktop.progress_throttle_ms / 1000
        loop = asyncio.get_running_loop()
        self._refresh_handle = loop.call_later(delay, self._flush_refresh)

    def _flush_refresh(self) -> None:
        self._refresh_handle = None
        pending, self._pending_refresh = self._pending_refresh, False
        if pending:
            self._spawn(self._publish_snapshot())

    async def _publish_snapshot(self) -> None:
        snapshot = await self.describe()
        self._last_fingerprint = self._fingerprint(snapshot)
        _ = await self._bus.events.publish("desktop.layout.changed", snapshot)

    @staticmethod
    def _fingerprint(snapshot: DesktopSnapshot) -> tuple[tuple[str, str, float | None], ...]:
        """判断「面板有没有变」的最小依据：只比状态与进度。"""
        return tuple(
            (entry.card.id, entry.status, entry.task.progress if entry.task is not None else None)
            for entry in snapshot.entries
        )

    async def _reconcile_loop(self) -> None:
        """定时对账：只有真出现差异才发事件。"""
        while True:
            await asyncio.sleep(self._config.cosmos.desktop.reconcile_seconds)
            snapshot = await self.describe()
            fingerprint = self._fingerprint(snapshot)
            if fingerprint != self._last_fingerprint:
                self._last_fingerprint = fingerprint
                _ = await self._bus.events.publish("desktop.layout.changed", snapshot)

    def _spawn(self, coro: Coroutine[object, object, object]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ---------- 内部：持久化 ----------

    def _layout_dict(self) -> dict[str, object]:
        return {
            "revision": self._revision,
            "order": list(self._order),
            "move": dict(self._move),
            "pin": dict(self._pin),
            "collapse": dict(self._collapse),
        }

    def _load_layout(self) -> None:
        try:
            raw = self._layout_path.read_text(encoding="utf-8")
            data = cast("object", json.loads(raw))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("桌面布局读取失败，使用空布局", 路径=str(self._layout_path), 原因=str(exc))
            return
        if not isinstance(data, dict):
            log.warning("桌面布局格式不合法，使用空布局", 路径=str(self._layout_path))
            return
        payload = cast("dict[str, object]", data)
        revision = payload.get("revision")
        self._revision = revision if isinstance(revision, int) else 0
        self._order = _str_list(payload.get("order"))
        self._move = _str_map(payload.get("move"))
        self._pin = _bool_map(payload.get("pin"))
        self._collapse = _bool_map(payload.get("collapse"))

    def _persist_layout(self) -> None:
        """原子写：先写临时文件，再 replace。"""
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._layout_path.with_suffix(".tmp")
            _ = tmp.write_text(
                json.dumps(self._layout_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._layout_path)
        except OSError as exc:
            log.warning("桌面布局写盘失败", 路径=str(self._layout_path), 原因=str(exc))


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in cast("list[object]", value) if isinstance(item, str)]


def _str_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in cast("dict[str, object]", value).items():
        if isinstance(item, str):
            result[key] = item
    return result


def _bool_map(value: object) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, bool] = {}
    for key, item in cast("dict[str, object]", value).items():
        if isinstance(item, bool):
            result[key] = item
    return result
