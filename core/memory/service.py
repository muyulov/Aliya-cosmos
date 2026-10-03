"""记忆服务：把「记住」与「想起」收成一个服务。

本模块是记忆层的**编排层**：生命周期、对外端口、队列与 worker 的接线都在这里，
具体语句归 `store/`，队列与重试归 `ingest/`。期一只有**写入口**（`observe`）；
读入口 `recall` 与定时循环（反思 / 衰减 / 合并）分别留到 Task 11 与期三。

## 为什么依赖里有两个「没有消费者」的成员

`_llm` 与 `_decision` 本期确实没有调用点，但它们是**容器契约**要求的：
`dependencies` 与构造器形参必须逐一对齐（`ServiceManager._validate_contract`
会当账），且期二的门控 / 抽取 / 归位都要用它们。为了「暂时没人用」而删掉，
等于把容器契约改坏，再补回来时还要重新对齐一遍——照写。

## 单协程串行的写者

worker 是一个协程，`while True` 地从队列取观测、串行地落库。这天然满足 Kùzu
的单写者要求（同一时刻只有一个协程在写），也免得并发写图库时互相踩。查询 / 落库
本身仍是同步的 Kùzu 调用，由 `MemorySession` 沉进单线程执行器（见 `store/session.py`）。

## 失败语义（设计文档 §6.5）

- **过载**（队列满）⇒ 丢弃该观测 + ERROR 告警，**不进死信**、不阻塞调用方；
- **故障**（瞬时 / 永久）⇒ 瞬时退避重试，用尽或不可重试则进死信，
  死信条带**完整观测载荷**——原文绝不因失败而消失；
- **单条失败不中断循环**：worker 只记日志继续跑，一条坏观测不该拖垮整条流水线。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import ClassVar, override

from pydantic import ValidationError

from core.config import MemorySettings
from core.decision import DecisionService
from core.embedding import EmbeddingService
from core.llm import LLMService
from core.memory.errors import MemoryInputError, MemoryStoreError
from core.memory.ingest.queue import ObservationQueue
from core.memory.ingest.retry import (
    STAGE_UTTERANCE,
    DeadLetter,
    DeadLetterBox,
    RetryPolicy,
    run_with_retry,
)
from core.memory.models import Observation, new_id
from core.memory.store.queries import (
    CHARACTER_COUNT,
    UPSERT_UTTERANCE,
    UTTERANCE_COUNT,
    escape_literal,
)
from core.memory.store.schema import ensure_schema
from core.memory.store.session import MemorySession
from core.service.base import HealthStatus, Service, not_running_detail


def _now_epoch() -> int:
    """当前时刻的 epoch 秒（死信条按它排序与对账）。"""
    return int(time.time())


class MemoryService(Service):
    """记忆服务：观察者形态的记忆层，自带常驻 worker。"""

    name: ClassVar[str] = "memory"

    dependencies: ClassVar[tuple[type[Service], ...]] = (
        EmbeddingService,
        LLMService,
        DecisionService,
    )

    def __init__(
        self,
        embedding: EmbeddingService,
        llm: LLMService,
        decision: DecisionService,
        config: MemorySettings,
    ) -> None:
        super().__init__()
        self._embedding: EmbeddingService = embedding
        # 本期没有消费者，但它们是容器契约要求的依赖（见模块 docstring）。
        self._llm: LLMService = llm
        self._decision: DecisionService = decision
        self._config: MemorySettings = config
        self._session: MemorySession | None = None
        self._queue: ObservationQueue | None = None
        self._deadletters: DeadLetterBox | None = None
        self._worker: asyncio.Task[None] | None = None

    # ---------- 生命周期 ----------

    @override
    async def start(self) -> None:
        """开库、建表、起 worker。幂等：会话已建就直接返回。

        幂等判据取**自己的资源**（`_session` 是否已建）而不是 `self.running`：状态
        由容器在装配后统一置位，直接调 `start()` 时它仍是 `created`，拿它当闸门
        会重复开库、换 worker。参照 `EmbeddingService.start()` 的同一做法。
        """
        if self._session is not None:
            return
        session = MemorySession(Path(self._config.db_path), dimensions=self._config.dimensions)
        await session.open()
        await ensure_schema(session)
        self._session = session
        self._queue = ObservationQueue(self._config.queue_size)
        self._deadletters = DeadLetterBox(Path(self._config.retry.deadletter_path))
        self._worker = asyncio.create_task(self._consume())

    @override
    async def stop(self) -> None:
        """停 worker、关库。幂等，且对从未启动过的实例安全。

        顺序是**硬要求**（见 `ObservationQueue.close()` 的契约）：先 `close()` 栅住
        入队（免得 stop 期间调用方吊死），再 `cancel()` 把阻塞在 `get()` 上的 worker
        拉出来，最后 `gather` 收殓它的取消。**绝不吞 `CancelledError`**——worker 里的
        取消必须原样上抛，否则 `gather` 等不到它、关库会与它在途的写撞车。
        """
        queue, self._queue = self._queue, None
        worker, self._worker = self._worker, None
        session, self._session = self._session, None
        if queue is not None:
            queue.close()
        if worker is not None:
            _ = worker.cancel()
            _ = await asyncio.gather(worker, return_exceptions=True)
        if session is not None:
            await session.close()

    @override
    async def health(self) -> HealthStatus:
        """看会话是否已开，并报出计数。

        extra 里的三类计数是「库在不在、里面有多少东西」的可观测面：角色数 /
        原文数直接查库，死信条数直接读文件——后者刻意不经 Kùzu，库里写不进去时
        它照样报得出来。未运行时不查库（会话可能是 None），套 `not_running_detail`。
        """
        extra: dict[str, object] = {
            "库路径": self._config.db_path,
            "角色数": 0,
            "原文数": 0,
            "死信": self._死信条数(),
        }
        session = self._session
        if session is None or not session.opened:
            detail = not_running_detail(self.state)
            return HealthStatus(
                name=self.label, healthy=False, state=self.state, detail=detail, extra=extra
            )
        行原文 = await session.run(UTTERANCE_COUNT)
        行角色 = await session.run(CHARACTER_COUNT)
        extra["原文数"] = 行原文[0][0]
        extra["角色数"] = 行角色[0][0]
        return HealthStatus(name=self.label, healthy=True, state=self.state, detail="", extra=extra)

    # ---------- 写入口 ----------

    async def observe(self, observation: Observation) -> str:
        """喂入一个原始回合，**入队即返回**，绝不等待落库或 LLM。

        返回该回合的 utterance_id。队列满时**丢弃**该回合、记 ERROR，但**照常返回
        一个 id、不抛错、不阻塞**——记忆层再忙也不能拖慢对话循环（设计文档 §6.2 第 3 条）。

        非法载荷（文本全空白这类「类型对但语义不可用」）在入队之前就抛
        `MemoryInputError`：传进来的是 pydantic 模型，逐字段复核一遍，不让坏数据进队列。
        """
        queue = self._require_queue()
        self._复核载荷(observation)
        utterance_id = new_id()
        if not await queue.put(observation):
            self.log.error(
                "观测队列已满，丢弃本回合",
                队列上限=self._config.queue_size,
                丢弃数=queue.dropped,
            )
        return utterance_id

    # ---------- worker ----------

    async def _consume(self) -> None:
        """常驻消费者：单协程串行取观测、落库。

        整条循环体包 `try/except Exception`，**只记日志、不中断**——一条坏观测不该
        拖垮整条流水线。`CancelledError` 是 `BaseException` 的子类，不会被上面的
        `except Exception` 接走，但显式再写一遍把意图钉死：取消必须原样上抛。
        """
        while True:
            queue = self._queue
            if queue is None:
                return
            try:
                observation = await queue.get()
                # 成败由 _ingest 自己记进死信；worker 只负责循环不断，不看返回值
                _ = await self._ingest(observation)
            except asyncio.CancelledError:
                raise
            except Exception as 故障:
                self.log_error("观测处理失败，已跳过", 故障)

    async def _ingest(self, observation: Observation) -> bool:
        """处理一条观测：嵌入 → 阶段 A（原文落库）。成功为真。

        嵌入与落库**同属阶段 A**：原文还没进库时，这一整段失败都意味着「原文可能丢」，
        所以它整体受重试与死信保护。嵌入是网络调用、最可能瞬时故障，把它排在重试
        操作内正是为了让它退避重试（设计文档 §6.5）。

        失败分流：
        - 瞬时故障 → `run_with_retry` 退避重试；
        - 用尽或不可重试（含维度不符的 `MemoryStoreError`）→ 进死信并**带完整载荷**。

        返回真假供重放判断成败；worker 不关心（它只负责不让循环断）。
        """
        utterance_id = new_id()
        计数: list[int] = [0]  # 实测尝试次数：run_with_retry 失败路径不回传，得自己记
        try:
            维度实测, 尝试数 = await self._阶段甲(utterance_id, observation, 计数)
        except Exception as 故障:
            self._write_deadletter(utterance_id, observation, 故障, attempts=计数[0])
            return False
        self.log.info(
            "原文已入库",
            回合=utterance_id,
            维度=维度实测,
            尝试=尝试数,
        )
        return True

    async def _尝试阶段甲(self, observation: Observation) -> bool:
        """重放用：跑一遍阶段甲，只看成败，不写死信（由调用方决定条目去留）。"""
        计数: list[int] = [0]
        try:
            _, _ = await self._阶段甲(new_id(), observation, 计数)
        except Exception as 故障:
            self.log_error("死信重放仍失败，保留待下一轮", 故障)
            return False
        return True

    async def _阶段甲(
        self, utterance_id: str, observation: Observation, 计数: list[int]
    ) -> tuple[int, int]:
        """阶段 A 的实体：嵌入 → 校验维度 → 落原文，受重试保护。

        返回 `(向量维度, 尝试次数)`；失败时上抛最后那次异常。`计数` 由调用方传入、
        在每次尝试时累加——`run_with_retry` 的失败路径不回传次数，写死信要的是实测值。
        """
        policy = RetryPolicy(
            max_attempts=self._config.retry.max_attempts,
            backoff=self._config.retry.backoff,
        )
        维度 = self._config.dimensions

        async def 一次尝试() -> int:
            计数[0] += 1
            vector = await self._embedding.embed(observation.text)
            if len(vector) != 维度:
                raise MemoryStoreError(
                    f"嵌入返回 {len(vector)} 维向量，与配置的 {维度} 维不符",
                    operation="embed",
                )
            await self._write_utterance(utterance_id, observation, vector)
            return len(vector)

        维度实测, 尝试数 = await run_with_retry(一次尝试, policy=policy)
        return 维度实测, 尝试数

    async def _write_utterance(
        self, utterance_id: str, observation: Observation, vector: list[float]
    ) -> None:
        """阶段 A：把原文与向量写进 `Utterance`（唯一接触 Cypher 的调用点在此）。"""
        session = self._require_session()
        语句 = UPSERT_UTTERANCE.format(
            id=escape_literal(utterance_id),
            text=escape_literal(observation.text),
            kind=escape_literal(observation.kind),
            world_time=observation.world_time if observation.world_time is not None else "null",
            embedding="[" + ", ".join(repr(分量) for 分量 in vector) + "]",
            importance=0.0,
            access_count=0,
            last_access="null",
        )
        _ = await session.run(语句)

    # ---------- 死信 ----------

    def _write_deadletter(
        self,
        utterance_id: str,
        observation: Observation,
        故障: BaseException,
        *,
        attempts: int,
    ) -> None:
        """把一条失败的阶段 A 写进死信：载荷是**完整观测**，原文绝不因失败而消失。

        `attempts` 是实际跑过的次数（不可重试的永久故障就是 1，用尽则是上限+1）——
        记实测值而不是拿配置上限冒充，重放与对账才看得出「是试到底了还是当场毙掉」。
        """
        箱 = self._require_deadletters()
        箱.append(
            DeadLetter(
                utterance_id=utterance_id,
                stage=STAGE_UTTERANCE,
                error_type=type(故障).__name__,
                error_message=str(故障),
                attempts=attempts,
                payload=observation.model_dump(),
                failed_at=_now_epoch(),
            )
        )
        self.log_error("原文落库失败，已进死信", 故障, 回合=utterance_id)

    async def replay_deadletters(self) -> int:
        """重放死信：逐条重试一次，成功的剔除、失败的与超限的留下。

        返回到剔除条数，便于调用方记日志与断言。**本期由显式调用与测试驱动**
        （期三才挂进定时循环）。重放失败只保留、不抛错——死信得自己收敛，
        不能因为一条重放不动就炸掉整轮。
        """
        箱 = self._require_deadletters()
        存活: list[DeadLetter] = []
        剔除 = 0
        for 条 in 箱.load():
            if await self._重放一条(条):
                剔除 += 1
            else:
                存活.append(条)
        if 剔除:
            箱.rewrite(存活)
        return 剔除

    async def _重放一条(self, 条: DeadLetter) -> bool:
        """重放一条死信；成功为真。

        只处理阶段 A（原文落库）的死信：它带完整载荷，重放时把原文的向量重新取一遍
        再落库。阶段 B 的死信期一还没产出，遇到也原样保留（不剔除），免得误删别的
        阶段待处理的东西。

        **不走 `_ingest`**：那条路失败会**新写**一条死信，与「把原条目留在原位」
        叠加就变成重复条目。重放只判成败，由 `replay_deadletters` 决定谁留谁走。
        """
        if 条.stage != STAGE_UTTERANCE or 条.payload is None:
            return False
        try:
            observation = Observation.model_validate(条.payload)
        except ValidationError as 故障:
            self.log_error("死信载荷不合法，保留待人工处理", 故障, 回合=条.utterance_id)
            return False
        return await self._尝试阶段甲(observation)

    # ---------- 内部 ----------

    @staticmethod
    def _复核载荷(observation: Observation) -> None:
        """入队前复核：`model_construct` 之类的旁路会绕开校验，这里补一道。

        把 pydantic 的 `ValidationError` 包成 `MemoryInputError`：调用方按记忆层的
        错误类型分支，不该被 pydantic 的内部异常类型漏出去。
        """
        try:
            _ = Observation.model_validate(observation.model_dump())
        except ValidationError as 故障:
            raise MemoryInputError(str(故障)) from 故障

    def _死信条数(self) -> int:
        """死信条数：文件不存在时是 0（一次失败都还没发生）。"""
        箱 = self._deadletters
        return 0 if 箱 is None else len(箱.load())

    def _require_queue(self) -> ObservationQueue:
        """取队列；未运行时抛存储错误而不是裸 AttributeError。"""
        if self._queue is None:
            raise MemoryStoreError("记忆服务尚未启动", operation="observe")
        return self._queue

    def _require_session(self) -> MemorySession:
        """取会话；未运行时抛存储错误。"""
        if self._session is None:
            raise MemoryStoreError("记忆服务尚未启动", operation="ingest")
        return self._session

    def _require_deadletters(self) -> DeadLetterBox:
        """取死信箱；未运行时抛存储错误。"""
        if self._deadletters is None:
            raise MemoryStoreError("记忆服务尚未启动", operation="deadletter")
        return self._deadletters
