"""生活状态读写：小 JSON 原子替换，流水只追加；两类文件各自加锁。"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from agent.aliya.life.models import ActivityState

STATE_FILE = "activity.json"
LOG_FILE = "activity.jsonl"


class ActivityStore:
    """`activity.json` 存当前状态，`activity.jsonl` 存已完成流水。"""

    def __init__(self, root: str | Path) -> None:
        self._dir = Path(root)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / STATE_FILE
        self._log_path = self._dir / LOG_FILE
        self._lock = asyncio.Lock()

    async def load(self) -> ActivityState:
        async with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> ActivityState:
        if not self._path.exists():
            return ActivityState(updated_at=datetime.now(UTC))
        try:
            raw = self._path.read_text(encoding="utf-8")
            return ActivityState.model_validate_json(raw)
        except (OSError, ValueError) as exc:
            logger.warning("活动状态损坏，备份后重建", 原因=str(exc))
            self._backup_broken()
            return ActivityState(updated_at=datetime.now(UTC))

    def _backup_broken(self) -> None:
        """把读不动的状态另存一份，避免下一轮再撞同一个坑。"""
        try:
            raw = self._path.read_text(encoding="utf-8", errors="replace")
            _ = self._path.with_suffix(".json.bad").write_text(raw, encoding="utf-8")
        except OSError as exc:
            logger.warning("活动状态备份失败，直接重建", 原因=str(exc))

    async def save(self, state: ActivityState) -> None:
        state.updated_at = datetime.now(UTC)
        payload = state.model_dump_json(indent=2, ensure_ascii=False)
        async with self._lock:
            tmp = self._path.with_suffix(".json.tmp")
            _ = tmp.write_text(payload, encoding="utf-8")
            await asyncio.to_thread(os.replace, tmp, self._path)

    async def append(
        self,
        state: ActivityState,
        *,
        ended_at: datetime,
        notes: str = "",
        files: list[str] | None = None,
        turns: int = 1,
    ) -> None:
        current = state.current
        if current is None:
            return
        record: dict[str, Any] = {
            "at": ended_at.isoformat(),
            "id": current.id,
            "title": current.title,
            "place": current.place,
            "started_at": current.started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "turns": turns,
            "files": files or [],
            "notes": notes,
        }
        async with self._lock:
            with self._log_path.open("a", encoding="utf-8") as handle:
                _ = handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """按时间正序返回最近若干条流水；坏行直接跳过。"""
        if not self._log_path.exists():
            return []
        lines = self._log_path.read_text(encoding="utf-8").splitlines()
        result: list[dict[str, Any]] = []
        for line in reversed(lines[-limit:]):
            try:
                result.append(json.loads(line))
            except ValueError:
                continue
        return list(reversed(result))
