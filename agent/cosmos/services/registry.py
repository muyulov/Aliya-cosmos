"""子 agent 注册表。

纯数据 + 纯检索：不调模型、不碰运行时。注册期一次性校验（fail fast），
查询期只读内存。`match()` 负责粗筛与兜底，真正的语义精排在 `DispatchService`。
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import ClassVar, override

from agent.cosmos.contracts import (
    AGENT_ID_PATTERN,
    CAPABILITY_PATTERN,
    SUMMARY_MAX_CHARS,
    AgentCard,
    AgentHandler,
    AgentMatch,
)
from agent.cosmos.errors import AgentNotFoundError, AgentRegistrationError, PresencePrivacyError
from agent.cosmos.registry.bootstrap import load_agents
from core.config import AgentSettings
from core.service import Service

#: 协议里声明的状态字段；越界字段一律拒绝
KNOWN_PRESENCE_FIELDS: frozenset[str] = frozenset(
    {"online", "activity", "typing", "mood", "task_context", "env"}
)

#: 中文/英文关键词 → 能力标签。注册表不调模型，靠这张小表把「改一下」这类
#: 说法映射到 `code_edit`，否则纯英文 capability 永远命中不了中文请求。
CAPABILITY_HINTS: dict[str, frozenset[str]] = {
    "改": frozenset({"code_edit"}),
    "编辑": frozenset({"code_edit"}),
    "写": frozenset({"code_edit", "file_write"}),
    "新增": frozenset({"file_write"}),
    "删": frozenset({"file_write"}),
    "重命名": frozenset({"file_rename"}),
    "文件": frozenset({"file_write", "code_edit"}),
    "测试": frozenset({"test_run"}),
    "跑": frozenset({"test_run"}),
    "搜": frozenset({"search"}),
    "查": frozenset({"search"}),
    "回显": frozenset({"echo"}),
    "echo": frozenset({"echo"}),
}

#: 命中分数（02 章 6 节的多路检索）
SCORE_CAPABILITY = 0.9
SCORE_SUMMARY = 0.6
SCORE_DESCRIPTION = 0.4
SCORE_EXAMPLE = 0.3

_WORD_SPLIT = re.compile(r"[^\w\u4e00-\u9fff]+")


def _bigrams(text: str) -> set[str]:
    """按二元组切词：中文没有空格，二元组足以做粗筛且不引分词依赖。"""
    cleaned = _WORD_SPLIT.sub("", text)
    return {cleaned[index : index + 2] for index in range(len(cleaned) - 1)}


def _overlap(left: str, right: str) -> float:
    """两段文本的二元组重叠率（Jaccard）。"""
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class AgentRegistryService(Service):
    """维护 `AgentCard` 与 handler 的绑定。"""

    name: ClassVar[str] = "cosmos.registry"

    def __init__(self, config: AgentSettings) -> None:
        super().__init__()
        self._config: AgentSettings = config
        self._cards: dict[str, AgentCard] = {}
        self._handlers: dict[str, AgentHandler] = {}
        self._enabled: dict[str, bool] = {}
        self._sources: dict[str, str] = {}
        #: 配置声明的分组；未声明的 group 落 general
        self._groups: set[str] = {group.key for group in config.cosmos.desktop.groups}
        self._groups.add("general")

    # ---------- 生命周期 ----------

    @override
    async def start(self) -> None:
        """启动时按配置装配子 agent；没有配置则什么都不做。"""
        if self._cards or not self._config.cosmos.agents:
            return
        load_agents(self, self._config.cosmos)

    # ---------- 注册与查询 ----------

    def register(self, card: AgentCard, handler: AgentHandler, *, source: str = "config") -> None:
        """注册一张卡片；任何校验失败都抛 `AgentRegistrationError`。"""
        if source == "runtime" and not self._config.cosmos.allow_runtime_registration:
            msg = "运行期注册未开启（allow_runtime_registration=false）"
            raise AgentRegistrationError(msg)
        self._validate_id(card.id)
        if card.id in self._cards:
            msg = f"agent id 重复：{card.id}（已有来源 {self._sources[card.id]}，新来源 {source}）"
            raise AgentRegistrationError(msg)
        self._validate_capabilities(card)
        self._validate_schema(card)
        self._validate_execution(card)
        self._validate_handler(card.id, handler)
        self._validate_presence_fields(card)
        self._warn_duplicate_name(card)

        normalized = self._normalize_card(card)
        self._cards[card.id] = normalized
        self._handlers[card.id] = handler
        self._enabled[card.id] = True
        self._sources[card.id] = source
        self.log.info(
            "agent 注册成功",
            agent_id=card.id,
            capabilities="、".join(normalized.capabilities),
            来源=source,
        )

    def unregister(self, agent_id: str) -> None:
        """注销：从桌面移除，历史任务保留（运行时不清理）。"""
        _ = self.get(agent_id)
        _ = self._cards.pop(agent_id)
        _ = self._handlers.pop(agent_id, None)
        _ = self._enabled.pop(agent_id, None)
        _ = self._sources.pop(agent_id, None)
        self.log.warning("agent 已注销", agent_id=agent_id)

    def enable(self, agent_id: str) -> None:
        """启用。"""
        _ = self.get(agent_id)
        self._enabled[agent_id] = True
        self.log.info("agent 已启用", agent_id=agent_id, 操作="enable")

    def disable(self, agent_id: str) -> None:
        """禁用；卡片仍在桌面上，点击返回 `AgentNotFoundError(reason="disabled")`。"""
        _ = self.get(agent_id)
        self._enabled[agent_id] = False
        self.log.info("agent 已禁用", agent_id=agent_id, 操作="disable")

    # ---------- 查询 ----------

    def get(self, agent_id: str) -> AgentCard:
        """按 id 取卡片；不存在抛错。禁用状态仍可查（桌面置灰语义）。"""
        card = self._cards.get(agent_id)
        if card is None:
            raise AgentNotFoundError(f"agent 不存在：{agent_id}")
        return card

    def handler(self, agent_id: str) -> AgentHandler:
        """取 handler；不存在抛错。"""
        handler = self._handlers.get(agent_id)
        if handler is None:
            raise AgentNotFoundError(f"agent 不存在：{agent_id}")
        return handler

    def list(self) -> list[AgentCard]:
        """按桌面顺序返回全部卡片：置顶 → order → id。"""
        return sorted(
            self._cards.values(),
            key=lambda card: (
                0 if card.desktop.pinned else 1,
                card.desktop.order,
                card.id,
            ),
        )

    def enabled(self, agent_id: str) -> bool:
        """是否允许被调度。"""
        return self._enabled.get(agent_id, False)

    def match(self, text: str, top_k: int = 5) -> list[AgentMatch]:
        """多路检索：能力标签 → 摘要 → 描述 → 示例，取最高分。"""
        if not text.strip():
            return []
        scores: list[AgentMatch] = []
        for card in self._cards.values():
            if not self._enabled.get(card.id, False):
                continue
            match = self._score(card, text)
            if match is not None:
                scores.append(match)
        scores.sort(key=lambda item: (-item.score, item.agent_id))
        return scores[:top_k]

    # ---------- 内部：校验 ----------

    @staticmethod
    def _validate_id(agent_id: str) -> None:
        if not re.match(AGENT_ID_PATTERN, agent_id):
            msg = f"agent id 非法：{agent_id!r}"
            raise AgentRegistrationError(msg)

    @staticmethod
    def _validate_capabilities(card: AgentCard) -> None:
        if not card.capabilities:
            msg = f"agent {card.id} 的 capabilities 不能为空"
            raise AgentRegistrationError(msg)
        pattern = re.compile(CAPABILITY_PATTERN)
        for capability in card.capabilities:
            if not pattern.match(capability):
                msg = f"agent {card.id} 的能力标签非法：{capability!r}"
                raise AgentRegistrationError(msg)

    @staticmethod
    def _validate_schema(card: AgentCard) -> None:
        if card.params_schema.get("type") != "object":
            msg = f"agent {card.id} 的 params_schema 必须是 object"
            raise AgentRegistrationError(msg)

    @staticmethod
    def _validate_execution(card: AgentCard) -> None:
        if card.execution.timeout_seconds <= 0:
            msg = f"agent {card.id} 的 timeout_seconds 必须为正"
            raise AgentRegistrationError(msg)

    @staticmethod
    def _validate_handler(agent_id: str, handler: object) -> None:
        if not callable(handler):
            msg = f"agent {agent_id} 的 handler 不是可调用对象"
            raise AgentRegistrationError(msg)
        if not inspect.iscoroutinefunction(handler):
            msg = f"agent {agent_id} 的 handler 必须是 async 函数"
            raise AgentRegistrationError(msg)

    @staticmethod
    def _validate_presence_fields(card: AgentCard) -> None:
        unknown = set(card.permissions.presence_fields) - KNOWN_PRESENCE_FIELDS
        if unknown:
            raise PresencePrivacyError(
                f"agent {card.id} 请求了未知状态字段：{'、'.join(sorted(unknown))}"
            )

    def _warn_duplicate_name(self, card: AgentCard) -> None:
        for existing in self._cards.values():
            if existing.name == card.name:
                self.log.warning(
                    "展示名重复（id 不同，展示时可区分即可）",
                    agent_id=card.id,
                    另一agent=existing.id,
                )
                return

    # ---------- 内部：归一化 ----------

    def _normalize_card(self, card: AgentCard) -> AgentCard:
        """展示约束不该阻塞启动：摘要超长截断告警，未声明分组回落 general。"""
        updates: dict[str, object] = {}
        if len(card.summary) > SUMMARY_MAX_CHARS:
            updates["summary"] = card.summary[:SUMMARY_MAX_CHARS]
            self.log.warning(
                "agent 摘要超长，已截断",
                agent_id=card.id,
                上限=SUMMARY_MAX_CHARS,
            )
        if card.desktop.group not in self._groups:
            self.log.warning(
                "agent 分组未声明，已回落 general",
                agent_id=card.id,
                分组=card.desktop.group,
            )
            updates["desktop"] = card.desktop.model_copy(update={"group": "general"})
        if not updates:
            return card
        return card.model_copy(update=updates)

    # ---------- 内部：检索 ----------

    def _score(self, card: AgentCard, text: str) -> AgentMatch | None:
        """返回该卡片的最佳命中；没有任何依据则不参与排序。"""
        lowered = text.lower()
        hints: set[str] = set()
        for keyword, capabilities in CAPABILITY_HINTS.items():
            if keyword in text or keyword in lowered:
                hints |= set(capabilities)

        for capability in card.capabilities:
            if capability in lowered or capability in hints:
                return AgentMatch(
                    agent_id=card.id,
                    score=SCORE_CAPABILITY,
                    reason=f"capabilities: {capability}",
                )

        if _overlap(card.summary, text) >= 0.15:
            return AgentMatch(agent_id=card.id, score=SCORE_SUMMARY, reason="summary 关键词")
        if _overlap(card.description, text) >= 0.12:
            return AgentMatch(
                agent_id=card.id, score=SCORE_DESCRIPTION, reason="description 关键词"
            )
        for example in card.examples:
            if _overlap(example.input, text) >= 0.2:
                return AgentMatch(agent_id=card.id, score=SCORE_EXAMPLE, reason="examples 相似")
        return None


#: 供 bootstrap 复用的类型别名
Loader = Callable[[str, str], object]
