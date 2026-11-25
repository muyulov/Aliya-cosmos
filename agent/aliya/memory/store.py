"""Aliya 的长期记忆：JSONL 回合 + 原子写的 JSON 小文件。"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_LOG = logging.getLogger(__name__)

#: 落盘前拒绝的敏感信息：API key、GitHub token、身份证、银行卡。
_PII = (
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\b\d{17}[\dXx]\b"),
    re.compile(r"\b\d{16,19}\b"),
)


@dataclass(frozen=True, slots=True)
class Turn:
    message_id: str
    role: str
    text: str
    at: str = ""


class MemoryStore:
    def __init__(self, root: str | Path) -> None:
        self._dir = Path(root)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ---------- 回合 ----------

    def append_turn(self, message_id: str, role: str, text: str) -> None:
        record = {
            "message_id": message_id,
            "role": role,
            "text": text,
            "at": datetime.now(UTC).isoformat(),
        }
        with (self._dir / "turns.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def recent_turns(self, limit: int = 20) -> list[Turn]:
        if limit <= 0:
            return []
        path = self._dir / "turns.jsonl"
        if not path.exists():
            return []
        turns: list[Turn] = []
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            turns.append(
                Turn(
                    message_id=str(data.get("message_id", "")),
                    role=str(data.get("role", "")),
                    text=str(data.get("text", "")),
                    at=str(data.get("at", "")),
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
        return str(data.get("summary", "")) if isinstance(data, dict) else ""

    def save_cursor(self, sequence: int) -> None:
        self._write_json("cursor.json", {"sequence": sequence})

    def load_cursor(self) -> int:
        data = self._read_json("cursor.json", {})
        if not isinstance(data, dict):
            return 0
        try:
            return int(data.get("sequence", 0))
        except (TypeError, ValueError):
            return 0

    def remember(self, note: str, kind: str) -> bool:
        """按 kind 落盘；疑似敏感信息直接拒绝。"""
        if not note.strip():
            return False
        if any(pattern.search(note) for pattern in _PII):
            _LOG.warning("记忆内容疑似包含敏感信息，已拒绝落盘")
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
        return True

    # ---------- 原子文件 ----------

    def _write_json(self, name: str, value: object) -> None:
        path = self._dir / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _read_json(self, name: str, default: object) -> object:
        path = self._dir / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            bad = path.with_name(path.name + ".bad")
            with contextlib.suppress(OSError):
                os.replace(path, bad)
            _LOG.warning("记忆文件损坏，已备份：%s", path)
            return default

    def _load_list(self, name: str) -> list[str]:
        data = self._read_json(name, [])
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, str)]
