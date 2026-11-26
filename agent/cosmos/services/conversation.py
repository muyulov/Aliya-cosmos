"""对话服务：上下文、分段与脱敏。

三条硬约束（设计 06 章 1 节）：单一入口、单一出口、内容不改写。
本服务只做「分段 / 截断 / 脱敏扫描」，不合并短句、不补标点、不加前缀。

这一版在原有骨架上补了三层：

- 上下文：token 预算、批量摘要、pinned facts、会话 TTL；
- 分段：代码块 / URL / 行内代码原子保护，按字素安全切分；
- 脱敏：返回遮蔽后文本与命中位置，内部标识 / 密钥 / PII / 工程词分级。

意图分类已完全移除：本服务只负责上下文、分段与脱敏。
"""

from __future__ import annotations

import re
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import ClassVar, Literal

from agent.cosmos.contracts import (
    ConversationContext,
    ConversationTurn,
    MessageKind,
    PinnedFact,
    RedactionResult,
    RedactionSpan,
    TerminalMessage,
)
from core.config import AgentRedactionSettings, AgentSettings, AgentTerminalSettings
from core.llm.messages import user
from core.llm.service import LLMService
from core.service import ClockService, Service

#: 进入对话轮次的消息类型；progress / result / system 不进对话轮次
CONVERSATION_KINDS: frozenset[str] = frozenset({"chat", "task", "query", "mixed"})

#: 分段时认的句子边界
SENTENCE_END = re.compile(r"(?<=[。！？!?…])")

#: 原子块：代码围栏、行内代码、Markdown 链接/图片、URL
ATOMIC_PATTERN = re.compile(
    r"```[^\n]*\n[\s\S]*?```|`[^`\n]+`|!?\[[^\]]*\]\([^)\s]+\)|https?://[^\s<>()]+"
)


@dataclass(frozen=True, slots=True)
class _Rule:
    """一条脱敏规则。"""

    name: str
    pattern: re.Pattern[str]
    replacement: str
    action: Literal["mask", "flag"] = "mask"
    validator: Callable[[str], bool] | None = None


def _valid_id_card(value: str) -> bool:
    """18 位身份证校验位；只用于降低误报。"""
    if len(value) != 18:
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    checks = "10X98765432"
    try:
        total = sum(int(value[index]) * weights[index] for index in range(17))
    except ValueError:
        return False
    return checks[total % 11] == value[17].upper()


def _valid_luhn(value: str) -> bool:
    digits = [int(item) for item in value if item.isdigit()]
    if len(digits) < 13:
        return False
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _valid_ipv4(value: str) -> bool:
    return all(0 <= int(part) <= 255 for part in value.split("."))


#: 脱敏规则：L1 内部标识 → L2 密钥 → L3 PII → 工程词（只告警）
REDACTION_RULES: tuple[_Rule, ...] = (
    _Rule("task_id", re.compile(r"\btsk_[0-9A-Za-z]+"), "[任务标识]"),
    _Rule("dispatch_id", re.compile(r"\bdsp_[0-9A-Za-z]+"), "[调度标识]"),
    _Rule(
        "stack",
        re.compile(r"Traceback \(most recent call last\)[\s\S]*"),
        "[堆栈已隐藏]",
    ),
    _Rule(
        "error_type",
        re.compile(r"\b[A-Z]\w*(?:Error|Exception)\b"),
        "内部错误",
    ),
    _Rule(
        "secret_openai",
        re.compile(r"\b(?:sk|pk)-[A-Za-z0-9]{16,}\b"),
        "[密钥已隐藏]",
    ),
    _Rule(
        "secret_github",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        "[密钥已隐藏]",
    ),
    _Rule(
        "secret_aws",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[密钥已隐藏]",
    ),
    _Rule(
        "secret_bearer",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
        "[凭证已隐藏]",
    ),
    _Rule(
        "secret_assignment",
        re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[=:]\s*\S+"),
        "[凭证已隐藏]",
    ),
    _Rule(
        "pii_email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "[邮箱已隐藏]",
    ),
    _Rule(
        "pii_phone",
        re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
        "[手机号已隐藏]",
    ),
    _Rule(
        "pii_id_card",
        re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
        "[证件号已隐藏]",
        validator=_valid_id_card,
    ),
    _Rule(
        "pii_bank_card",
        re.compile(r"(?<!\d)\d{13,19}(?!\d)"),
        "[卡号已隐藏]",
        validator=_valid_luhn,
    ),
    _Rule(
        "pii_ip",
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        "[地址已隐藏]",
        validator=_valid_ipv4,
    ),
    _Rule(
        "engineering",
        re.compile(r"用户|玩家|agent|模型|调度|面板"),
        "",
        action="flag",
    ),
)

#: 简单的事实抽取：只保留明确偏好 / 约束 / 称呼，避免把闲聊固化
PIN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("preference", re.compile(r"(?:我|俺|人家)(?:更|最|比较)?喜欢([^，。！？\n]{1,30})")),
    ("dislike", re.compile(r"(?:我|俺|人家)(?:不|讨厌)(?:喜欢|吃|用|看)([^，。！？\n]{1,30})")),
    ("constraint", re.compile(r"(?:以后|之后|记住|下次)(?:都|要|别|不要)?([^，。！？\n]{1,40})")),
    ("address", re.compile(r"(?:叫我|称呼我)([^，。！？\n]{1,12})")),
)


@dataclass
class _Session:
    """单个会话的对话上下文。"""

    turns: deque[ConversationTurn] = field(default_factory=deque)
    summary: str | None = None
    pending_dropped: list[ConversationTurn] = field(default_factory=list)
    pinned_facts: dict[str, PinnedFact] = field(default_factory=dict)
    topic: str | None = None
    last_user_message_at: datetime | None = None
    last_aliya_message_at: datetime | None = None
    last_active_at: datetime | None = None
    unanswered_count: int = 0
    summary_degraded: bool = False


class ConversationService(Service):
    """上下文、分段与脱敏。"""

    name: ClassVar[str] = "cosmos.conversation"
    dependencies: ClassVar[tuple[type[Service], ...]] = (LLMService, ClockService)

    def __init__(self, llm: LLMService, clock: ClockService, config: AgentSettings) -> None:
        super().__init__()
        self._llm: LLMService = llm
        self._clock: ClockService = clock
        self._config: AgentSettings = config
        self._terminal: AgentTerminalSettings = config.cosmos.terminal
        self._sessions: dict[str, _Session] = {}

    # ---------- 上下文 ----------

    def _session(self, session_id: str) -> _Session:
        now = self._clock.now()
        self._prune_sessions(now)
        session = self._sessions.get(session_id)
        if session is None:
            session = _Session(
                turns=deque(maxlen=self._terminal.conversation_turns),
                last_active_at=now,
            )
            self._sessions[session_id] = session
        session.last_active_at = now
        return session

    def _prune_sessions(self, now: datetime) -> None:
        ttl = timedelta(hours=self._terminal.session_ttl_hours)
        expired = [
            key
            for key, session in self._sessions.items()
            if session.last_active_at is not None and now - session.last_active_at > ttl
        ]
        for key in expired:
            _ = self._sessions.pop(key, None)
        if len(self._sessions) < self._terminal.max_sessions:
            return
        ordered = sorted(
            self._sessions.items(),
            key=lambda item: item[1].last_active_at or now,
        )
        for key, _ in ordered[: len(self._sessions) - self._terminal.max_sessions + 1]:
            _ = self._sessions.pop(key, None)

    async def build_context(
        self, session_id: str, *, exclude_message_id: str | None = None
    ) -> ConversationContext:
        """组装发给 Aliya 的上下文；超出预算时先摘要，再裁剪最旧轮次。

        `exclude_message_id` 用于排除当前这条消息：它已由 `InboundEnvelope.message`
        单独携带，若同时留在 turns 里，提示词会把它当成两句话重复发给模型。
        """
        session = self._session(session_id)
        await self._maybe_compress(session)
        turns = list(session.turns)
        if exclude_message_id:
            turns = [turn for turn in turns if turn.message_id != exclude_message_id]
        truncated = False
        while (
            self._estimate_tokens(turns, session.summary) > self._terminal.context_max_tokens
            and len(turns) > 1
        ):
            session.pending_dropped.append(turns.pop(0))
            truncated = True
            await self._compress(session)

        if (
            self._estimate_tokens(turns, session.summary) > self._terminal.context_max_tokens
            and session.summary
        ):
            budget = max(
                0,
                self._terminal.context_max_tokens * 2 - sum(len(turn.text) for turn in turns),
            )
            session.summary = session.summary[:budget] if budget else None
            truncated = True

        now = self._clock.now()
        self._prune_facts(session, now)
        topic = session.topic or next(
            (turn.text for turn in reversed(turns) if turn.role == "user"),
            None,
        )
        return ConversationContext(
            session_id=session_id,
            turns=turns,
            summary=session.summary,
            last_user_message_at=session.last_user_message_at,
            last_aliya_message_at=session.last_aliya_message_at,
            unanswered_count=(session.unanswered_count if self._terminal.unanswered_notice else 0),
            topic_hint=topic[:40] if topic else None,
            pinned_facts=list(session.pinned_facts.values()),
            token_estimate=self._estimate_tokens(turns, session.summary),
            truncated=truncated,
            summary_degraded=session.summary_degraded,
        )

    async def record_user(self, message: TerminalMessage) -> None:
        """写入一条用户消息；旧轮次先缓存，达到批量阈值再压缩。"""
        session = self._session(message.session_id)
        session.last_user_message_at = message.at or self._clock.now()
        session.unanswered_count += 1
        session.topic = message.text[:40] if message.text else session.topic
        self._push_turn(
            session,
            ConversationTurn(
                message_id=message.message_id,
                role="user",
                text=message.text,
                at=session.last_user_message_at,
            ),
        )
        self._extract_facts(session, message)

    def recent_user_texts(self, session_id: str, *, limit: int = 5) -> list[str]:
        """取最近几条用户文本，供情绪打分使用；不返回 Aliya 的消息。"""
        session = self._sessions.get(session_id)
        if session is None or limit <= 0:
            return []
        texts = [turn.text for turn in reversed(session.turns) if turn.role == "user"]
        return list(reversed(texts[:limit]))

    async def record_aliya(self, message: TerminalMessage) -> None:
        """写入一条 Aliya 消息；progress / result / system 不进对话轮次。"""
        if message.kind not in CONVERSATION_KINDS:
            return
        session = self._session(message.session_id)
        session.last_aliya_message_at = message.at or self._clock.now()
        session.unanswered_count = 0
        self._push_turn(
            session,
            ConversationTurn(
                message_id=message.message_id,
                role="aliya",
                text=message.text,
                at=session.last_aliya_message_at,
            ),
        )

    @staticmethod
    def _push_turn(session: _Session, turn: ConversationTurn) -> None:
        """塞入一轮；deque 满时把最旧轮次放进待压缩队列。"""
        if session.turns.maxlen is not None and len(session.turns) >= session.turns.maxlen:
            session.pending_dropped.append(session.turns[0])
        session.turns.append(turn)

    async def _maybe_compress(self, session: _Session) -> None:
        if not session.pending_dropped:
            return
        if (
            len(session.pending_dropped) < self._terminal.summary_batch_turns
            and self._estimate_tokens(list(session.turns), session.summary)
            <= self._terminal.context_max_tokens
        ):
            return
        await self._compress(session)

    async def _compress(self, session: _Session) -> None:
        """把旧内容压成摘要；失败则截断并 WARNING。"""
        if not session.pending_dropped:
            return
        dropped = list(session.pending_dropped)
        combined = "\n".join(
            item for item in [session.summary or "", *(turn.text for turn in dropped)] if item
        )
        limit = self._terminal.summary_chars
        try:
            summary = await self._llm.chat(
                [
                    user(
                        "把下面的对话历史压成一段不超过 "
                        + f"{limit} 字的摘要，只保留事实与未收尾的话题：\n{combined}"
                    )
                ]
            )
            session.summary = summary[:limit]
            session.summary_degraded = False
        except Exception as exc:
            session.summary = combined[:limit]
            session.summary_degraded = True
            self.log.warning(
                "上下文压缩失败，已截断",
                错误类型=type(exc).__name__,
                上限=limit,
            )
        session.pending_dropped.clear()

    @staticmethod
    def _estimate_tokens(turns: list[ConversationTurn], summary: str | None) -> int:
        chars = sum(len(turn.text) for turn in turns) + (len(summary) if summary else 0)
        return max(1, chars // 2)

    def _extract_facts(self, session: _Session, message: TerminalMessage) -> None:
        for key, pattern in PIN_PATTERNS:
            match = pattern.search(message.text or "")
            if match is None:
                continue
            value = match.group(1).strip()
            if not value:
                continue
            session.pinned_facts[key] = PinnedFact(
                key=key,
                value=value,
                source_message_id=message.message_id,
                confidence=0.8,
                expires_at=(message.at or self._clock.now())
                + timedelta(days=self._terminal.pinned_fact_ttl_days),
            )

    @staticmethod
    def _prune_facts(session: _Session, now: datetime) -> None:
        session.pinned_facts = {
            key: fact
            for key, fact in session.pinned_facts.items()
            if fact.expires_at is None or fact.expires_at > now
        }

    # ---------- 回传格式 ----------

    async def format(self, text: str, *, kind: MessageKind) -> list[str]:
        """按段落分段，原子块不拆；单段超限再按句子/子句切。"""
        del kind
        max_chars = self._config.cosmos.desktop.max_content_chars
        parts = self._split_with_atomic(text, max_chars)
        if not parts:
            return [text]
        if len(parts) > self._terminal.max_parts:
            # 分段上限只是建议值：宁可多发几条，也不能把回复尾部丢掉
            self.log.warning(
                "分段数超过建议上限，仍完整投递",
                段数=len(parts),
                建议上限=self._terminal.max_parts,
            )
        return parts

    def _split_with_atomic(self, text: str, max_chars: int) -> list[str]:
        parts: list[str] = []
        cursor = 0
        for match in ATOMIC_PATTERN.finditer(text):
            before = text[cursor : match.start()]
            if before.strip():
                parts.extend(self._split_text(before, max_chars))
            parts.extend(self._split_atomic(match.group(0), max_chars))
            cursor = match.end()
        tail = text[cursor:]
        if tail.strip():
            parts.extend(self._split_text(tail, max_chars))
        return parts

    def _split_text(self, text: str, max_chars: int) -> list[str]:
        paragraphs = [part for part in re.split(r"\n\s*\n", text) if part]
        if not paragraphs:
            return [text]
        parts: list[str] = []
        for paragraph in paragraphs:
            parts.extend(self._split_paragraph(paragraph, max_chars))
        return parts

    def _split_paragraph(self, paragraph: str, max_chars: int) -> list[str]:
        if len(paragraph) <= max_chars:
            return [paragraph]
        parts: list[str] = []
        current = ""
        for sentence in SENTENCE_END.split(paragraph):
            if not sentence:
                continue
            while len(sentence) > max_chars:
                if current:
                    parts.append(current)
                    current = ""
                head, sentence = _cut_grapheme(sentence, max_chars)
                parts.append(head)
            if len(current) + len(sentence) > max_chars:
                parts.append(current)
                current = sentence
            else:
                current += sentence
        if current:
            parts.append(current)
        return parts

    def _split_atomic(self, block: str, max_chars: int) -> list[str]:
        """代码块/URL/行内代码优先整体保留；超过硬上限才继续切。"""
        if len(block) <= max_chars:
            return [block]
        hard_cap = max(max_chars, self._terminal.segment_hard_max_chars)
        if not block.startswith("```"):
            if len(block) <= hard_cap:
                return [block]
            return self._hard_split(block, max_chars)
        if len(block) <= hard_cap:
            return [block]

        lines = block.splitlines()
        if len(lines) < 2:
            return self._hard_split(block, max_chars)
        opening = f"{lines[0]}\n"
        closing = "\n```"
        body = "\n".join(lines[1:-1])
        available = max_chars - len(opening) - len(closing)
        if available <= 0:
            return self._hard_split(block, max_chars)
        chunks: list[str] = []
        while body:
            piece, body = _cut_prefer_newline(body, available)
            chunks.append(f"{opening}{piece}{closing}")
        return chunks

    def _hard_split(self, text: str, max_chars: int) -> list[str]:
        parts: list[str] = []
        remainder = text
        while remainder:
            head, remainder = _cut_grapheme(remainder, max_chars)
            parts.append(head)
        return parts

    # ---------- 脱敏 ----------

    async def redact(self, text: str) -> RedactionResult:
        """扫描并遮蔽内部标识、密钥与 PII；工程词只告警。"""
        switches = self._terminal.redaction
        rules = list(REDACTION_RULES)
        if switches.mask_agent_ids:
            agent_rule = self._agent_id_rule()
            if agent_rule is not None:
                rules.insert(
                    2,
                    _Rule("agent_id", re.compile(agent_rule), "[工具]"),
                )

        matches: list[tuple[int, int, _Rule]] = []
        for rule in rules:
            if not self._rule_enabled(rule, switches):
                continue
            for match in rule.pattern.finditer(text):
                value = match.group(0)
                if rule.validator is not None and not rule.validator(value):
                    continue
                matches.append((match.start(), match.end(), rule))
        if not matches:
            return RedactionResult(hit=False, rules=[], text=text, spans=[])

        matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        selected: list[tuple[int, int, _Rule]] = []
        last_end = -1
        for start, end, rule in matches:
            if start < last_end:
                continue
            selected.append((start, end, rule))
            last_end = end

        spans = [
            RedactionSpan(
                rule=rule.name,
                start=start,
                end=end,
                action=rule.action,
                replacement=rule.replacement,
            )
            for start, end, rule in selected
        ]
        masked = text
        for start, end, rule in reversed(selected):
            if rule.action != "mask":
                continue
            masked = f"{masked[:start]}{rule.replacement}{masked[end:]}"

        names = sorted({rule.name for _, _, rule in selected})
        if switches.warn_on_hit:
            self.log.warning("回传文本命中脱敏规则", 规则="、".join(names))
        return RedactionResult(hit=True, rules=names, text=masked, spans=spans)

    @staticmethod
    def _rule_enabled(rule: _Rule, switches: AgentRedactionSettings) -> bool:
        """按配置开关过滤规则；未知规则默认启用。"""
        if rule.name == "task_id":
            return switches.mask_task_ids
        if rule.name == "dispatch_id":
            return switches.mask_dispatch_ids
        if rule.name == "agent_id":
            return switches.mask_agent_ids
        if rule.name == "stack":
            return switches.mask_stack
        if rule.name == "error_type":
            return switches.mask_error_types
        if rule.name.startswith("secret"):
            return switches.mask_secrets
        if rule.name.startswith("pii"):
            return switches.mask_pii
        return True

    def _agent_id_rule(self) -> str | None:
        """按已配置的 agent id 生成匹配式；没有配置则只认通用标识。"""
        ids = [entry.id for entry in self._config.cosmos.agents if entry.id]
        if not ids:
            return None
        return r"\b(?:" + "|".join(re.escape(item) for item in ids) + r")\b"


def _cut_grapheme(text: str, limit: int) -> tuple[str, str]:
    """按字素边界切分；单个字素超过上限时整体保留，避免拆坏 emoji。"""
    if len(text) <= limit:
        return text, ""
    head: list[str] = []
    size = 0
    for cluster in _grapheme_clusters(text):
        if head and size + len(cluster) > limit:
            break
        if not head and len(cluster) > limit:
            head.append(cluster)
            size += len(cluster)
            break
        head.append(cluster)
        size += len(cluster)
    cut = max(1, min(size, len(text)))
    return text[:cut], text[cut:]


def _grapheme_clusters(text: str) -> list[str]:
    clusters: list[str] = []
    current = ""
    for char in text:
        if not current:
            current = char
            continue
        if _joins_grapheme(current[-1], char):
            current += char
        else:
            clusters.append(current)
            current = char
    if current:
        clusters.append(current)
    return clusters


def _joins_grapheme(previous: str, following: str) -> bool:
    if not previous or not following:
        return False
    code = ord(following)
    if following in {"\u200d", "\ufe0f"} or previous == "\u200d":
        return True
    if unicodedata.combining(following):
        return True
    if 0x1F3FB <= code <= 0x1F3FF:
        return True
    return 0x1F1E6 <= ord(previous) <= 0x1F1FF and 0x1F1E6 <= code <= 0x1F1FF


def _cut_prefer_newline(text: str, limit: int) -> tuple[str, str]:
    if len(text) <= limit:
        return text, ""
    boundary = text.rfind("\n", 0, limit + 1)
    if boundary > 0:
        return text[: boundary + 1], text[boundary + 1 :]
    return _cut_grapheme(text, limit)
