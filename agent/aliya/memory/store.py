"""Aliya 的长期记忆：JSONL 回合 + 原子写的 JSON 小文件。"""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from core.logger import log

#: 记忆类型：决定落到哪个 JSON 文件，同时也是 remember 工具的枚举约束。
MemoryKind = Literal["anchor", "open_topic", "unspoken", "fact"]

#: 可查询的记忆范围：四种长期记忆 + 滚动摘要 + 未来意图。
MemoryScope = Literal["anchor", "open_topic", "unspoken", "fact", "summary", "intent"]

#: 意图列表的硬上限：去重后仍异常时按写入顺序丢弃最早的。
_MAX_INTENTS = 50

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


@dataclass(frozen=True, slots=True)
class Intent:
    """一条未来要主动做的事：Aliya 自己排的跟进。"""

    text: str
    due_at: datetime

    def is_due(self, now: datetime) -> bool:
        return self.due_at <= now


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

    def save_fact(self, fact: str) -> None:
        """追加一条长期事实；只保留最近 50 条，避免无限增长。"""
        text = fact.strip()
        if not text:
            return
        facts = self.load_facts()
        facts.append(text)
        self.save_facts(facts[-50:])

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
        """按 kind 落盘；未知类型与疑似敏感信息直接拒绝。

        kind 在工具层已由 Literal 约束，这里再做一次运行时兜底：
        把未知类型静默写进 facts 会让错误的分类长期污染记忆。
        """
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
        elif kind == "fact":
            values = self.load_facts()
            values.append(note)
            self.save_facts(values)
        else:
            log.warning("未知的记忆类型，已拒绝落盘", 类型=kind, 字数=len(note))
            return False
        log.debug("记忆已写入", 类型=kind, 字数=len(note), 同类条目数=len(values))
        return True

    def resolve_open_topic(self, topic: str) -> bool:
        """收尾一条未收尾话题；按原文或片段做保守匹配。"""
        target = topic.strip()
        if len(target) < 2:
            return False
        topics = self.load_open_topics()
        for index, item in enumerate(topics):
            matched = item == target or target in item or (len(item) >= 2 and item in target)
            if not matched:
                continue
            del topics[index]
            self.save_open_topics(topics)
            log.debug("未收尾话题已收尾", 话题=item, 剩余=len(topics))
            return True
        return False

    # ---------- 未来意图 ----------

    def add_intent(self, text: str, *, due_at: datetime) -> bool:
        """写入一条到期后由 ticker 触发的主动意图。"""
        note = text.strip()
        if not note:
            return False
        if any(pattern.search(note) for pattern in _PII):
            log.warning("意图内容疑似包含敏感信息，已拒绝落盘", 字数=len(note))
            return False
        records = self._read_json("intents.json", [])
        items = list(cast("list[object]", records)) if isinstance(records, list) else []
        entry = {
            "text": note,
            "due_at": due_at.isoformat(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        # 同一句话重复安排视为改期，避免重复叮嘱把意图堆满。
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if str(cast("dict[str, object]", item).get("text", "")).strip() == note:
                items[index] = entry
                break
        else:
            items.append(entry)
        # 极端情况下也只保留最近的若干条，防止文件无限增长。
        if len(items) > _MAX_INTENTS:
            items = items[-_MAX_INTENTS:]
        self._write_json("intents.json", items)
        log.debug("未来意图已写入", 到期=due_at.isoformat(), 字数=len(note))
        return True

    def load_intents(self) -> list[Intent]:
        records = self._read_json("intents.json", [])
        if not isinstance(records, list):
            return []
        intents: list[Intent] = []
        for item in cast("list[object]", records):
            if not isinstance(item, dict):
                continue
            record = cast("dict[str, object]", item)
            text = str(record.get("text", "")).strip()
            raw = record.get("due_at")
            if not text or not isinstance(raw, str):
                continue
            try:
                due_at = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=UTC)
            intents.append(Intent(text=text, due_at=due_at))
        intents.sort(key=lambda intent: intent.due_at)
        return intents

    def due_intents(self, now: datetime, limit: int = 3) -> list[Intent]:
        """到期的意图按时间先后返回；只读取，不删除。"""
        if limit <= 0:
            return []
        due = [intent for intent in self.load_intents() if intent.is_due(now)]
        due.sort(key=lambda intent: intent.due_at)
        return due[:limit]

    def cancel_intent(self, text: str) -> bool:
        """按原文或片段取消一条未来意图；用户说「不用提醒了」时用。"""
        target = text.strip()
        if len(target) < 2:
            return False
        records = self._read_json("intents.json", [])
        if not isinstance(records, list):
            return False
        items = list(cast("list[object]", records))
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            note = str(cast("dict[str, object]", item).get("text", "")).strip()
            matched = note == target or target in note or (len(note) >= 2 and note in target)
            if not matched:
                continue
            del items[index]
            self._write_json("intents.json", items)
            log.debug("未来意图已取消", 内容=note, 剩余=len(items))
            return True
        return False

    def pop_intent(self, text: str) -> bool:
        """按正文移除一条意图，避免到期触发后重复执行。"""
        target = text.strip()
        records = self._read_json("intents.json", [])
        if not isinstance(records, list):
            return False
        items = list(cast("list[object]", records))
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if str(cast("dict[str, object]", item).get("text", "")).strip() != target:
                continue
            del items[index]
            self._write_json("intents.json", items)
            return True
        return False

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
