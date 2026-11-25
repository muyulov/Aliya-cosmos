"""Aliya 的长期记忆：JSONL 回合 + 原子写的 JSON 小文件。"""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from core.logger import log

#: 落盘前拒绝的敏感信息：API key、GitHub token、身份证、银行卡。
_PII = (
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"\b\d{16,19}\b"),
)


def _json_load(text: str) -> object:
    """把 `json.loads` 的 `Any` 收窄为 `object`，避免 Any 向调用方扩散。"""
    return cast("object", json.loads(text))


@dataclass(frozen=True, slots=True)
class Turn:
    message_id: str
    role: str
    text: str
    at: str = ""
    session_id: str = ""


class MemoryStore:
    def __init__(self, root: str | Path) -> None:
        self._dir: Path = Path(root)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._user_ids: set[str] | None = None
        log.debug("记忆存储已就绪", 目录=str(self._dir))

    # ---------- 回合 ----------

    def append_turn(
        self,
        message_id: str,
        role: str,
        text: str,
        *,
        session_id: str = "",
        at: str = "",
    ) -> None:
        record = {
            "message_id": message_id,
            "role": role,
            "text": text,
            "session_id": session_id,
            "at": at or datetime.now(UTC).isoformat(),
        }
        with (self._dir / "turns.jsonl").open("a", encoding="utf-8") as handle:
            _ = handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if role == "user" and self._user_ids is not None:
            self._user_ids.add(message_id)

    def record_inbound(
        self,
        message_id: str,
        text: str,
        *,
        session_id: str = "",
        at: str = "",
    ) -> bool:
        """回执前幂等落盘；重复重放返回 False。"""
        if self._user_ids is None:
            self._user_ids = {turn.message_id for turn in self._read_turns() if turn.role == "user"}
        if message_id in self._user_ids:
            return False
        self.append_turn(message_id, "user", text, session_id=session_id, at=at)
        self._user_ids.add(message_id)
        return True

    def unanswered_turns(self, limit: int = 5) -> list[Turn]:
        """找出没有对应回复的用户回合，用于启动恢复。"""
        if limit <= 0:
            return []
        turns = self._read_turns()
        replied = {
            turn.message_id.removesuffix(":reply")
            for turn in turns
            if turn.role == "aliya" and turn.message_id.endswith(":reply")
        }
        pending = [turn for turn in turns if turn.role == "user" and turn.message_id not in replied]
        return pending[-limit:]

    def recent_turns(self, limit: int = 20) -> list[Turn]:
        if limit <= 0:
            return []
        return self._read_turns()[-limit:]

    def prune_turns(self, max_turns: int) -> int:
        """只保留最近 `max_turns` 条；返回删除条数。"""
        if max_turns <= 0:
            return 0
        turns = self._read_turns()
        if len(turns) <= max_turns:
            return 0
        kept = turns[-max_turns:]
        path = self._dir / "turns.jsonl"
        tmp = path.with_name(path.name + ".tmp")
        _ = tmp.write_text(
            "".join(
                json.dumps(
                    {
                        "message_id": turn.message_id,
                        "role": turn.role,
                        "text": turn.text,
                        "session_id": turn.session_id,
                        "at": turn.at,
                    },
                    ensure_ascii=False,
                )
                + "\n"
                for turn in kept
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        self._user_ids = None
        removed = len(turns) - len(kept)
        log.debug("记忆轮次已裁剪", 删除条数=removed, 保留条数=len(kept))
        return removed

    def _read_turns(self) -> list[Turn]:
        path = self._dir / "turns.jsonl"
        if not path.exists():
            return []
        turns: list[Turn] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                data = _json_load(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            record = cast("dict[str, object]", data)
            turns.append(
                Turn(
                    message_id=str(record.get("message_id", "")),
                    role=str(record.get("role", "")),
                    text=str(record.get("text", "")),
                    at=str(record.get("at", "")),
                    session_id=str(record.get("session_id", "")),
                )
            )
        return turns

    # ---------- 记忆条目 ----------

    def save_anchors(self, anchors: list[str]) -> None:
        self._write_json("anchors.json", anchors)

    def load_anchors(self) -> list[str]:
        return self._load_list("anchors.json")

    def save_open_topics(self, topics: list[str]) -> None:
        self._write_json("open_topics.json", topics)

    def load_open_topics(self) -> list[str]:
        return self._load_list("open_topics.json")

    def save_unspoken(self, items: list[str]) -> None:
        self._write_json("unspoken.json", items)

    def load_unspoken(self) -> list[str]:
        return self._load_list("unspoken.json")

    def save_facts(self, facts: list[str]) -> None:
        self._write_json("facts.json", facts)

    def load_facts(self) -> list[str]:
        return self._load_list("facts.json")

    def save_summary(self, summary: str) -> None:
        self._write_json("summary.json", {"summary": summary})

    def load_summary(self) -> str:
        data = self._read_json("summary.json", {})
        if not isinstance(data, dict):
            return ""
        return str(cast("dict[str, object]", data).get("summary", ""))

    def save_cursor(self, sequence: int, *, topic: str = "") -> None:
        cursors = self.load_cursors()
        cursors[topic or "*"] = sequence
        self._write_json("cursor.json", cursors)

    def load_cursor(self, *, topic: str = "") -> int:
        return self.load_cursors().get(topic or "*", 0)

    def load_cursors(self) -> dict[str, int]:
        data = self._read_json("cursor.json", {})
        if isinstance(data, int):
            return {"*": data}
        if not isinstance(data, dict):
            return {}
        cursors: dict[str, int] = {}
        for topic, sequence in cast("dict[str, object]", data).items():
            if not isinstance(sequence, int | float | str):
                continue
            try:
                cursors[str(topic)] = int(sequence)
            except (TypeError, ValueError):
                continue
        return cursors

    def remember(self, note: str, kind: str) -> bool:
        """按 kind 落盘；疑似敏感信息直接拒绝。"""
        if not note.strip():
            return False
        if any(pattern.search(note) for pattern in _PII):
            log.warning("记忆内容疑似包含敏感信息，已拒绝落盘", 类型=kind, 字数=len(note))
            return False
        if kind == "anchor":
            values = self.load_anchors()
            values.append(note)
            self.save_anchors(values)
        elif kind == "open_topic":
            values = self.load_open_topics()
            values.append(note)
            self.save_open_topics(values)
        elif kind == "unspoken":
            values = self.load_unspoken()
            values.append(note)
            self.save_unspoken(values)
        else:
            values = self.load_facts()
            values.append(note)
            self.save_facts(values)
        log.debug("记忆已写入", 类型=kind, 字数=len(note), 同类条目数=len(values))
        return True

    # ---------- 原子文件 ----------

    def _write_json(self, name: str, value: object) -> None:
        path = self._dir / name
        tmp = path.with_name(path.name + ".tmp")
        _ = tmp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _read_json(self, name: str, default: object) -> object:
        path = self._dir / name
        if not path.exists():
            return default
        try:
            return _json_load(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            bad = path.with_name(path.name + ".bad")
            with contextlib.suppress(OSError):
                os.replace(path, bad)
            log.warning("记忆文件损坏，已备份", 文件=str(path), 备份=str(bad))
            return default

    def _load_list(self, name: str) -> list[str]:
        data = self._read_json(name, [])
        if not isinstance(data, list):
            return []
        return [item for item in cast("list[object]", data) if isinstance(item, str)]
