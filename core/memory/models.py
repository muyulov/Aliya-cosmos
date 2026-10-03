"""记忆层公开模型。

约定：
- 对外只暴露观察（写入口）、召回结构（读出口），内部候选与落库结构不在此处。
- 观察与配置层的宽容相反：模型用 extra="forbid"。调用方传的是动作载荷，
  多写字段是调用方的错，应当立刻炸出来；YAML 配置要容忍拼错段名之外的多余键，
  那里是本仓既有的 extra="ignore"。两者场景不同，刻意不一致。
"""

from __future__ import annotations

from typing import ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 观测类型：对话、旁白、场景。
type ObservationKind = Literal["dialogue", "narration", "scene"]


def new_id() -> str:
    """给 Utterance / StateChange / Event 造主键。

    集中在这里是因为三处都要造主键，避免各写一份：形制一旦需要统一调整
    （换算法、加前缀、换长度），只有一个地方要改。

    用 `uuid4().hex`：标准库自带，零依赖、零发明；128 位随机、32 位十六进制串，
    随机性足够当主键，不必引 ULID / snowflake 这类额外依赖。
    """
    return uuid4().hex


#: 召回依据的来源标记：向量距离、图路径、结构化命中。
type TraceSource = Literal["vector", "graph", "structured"]

#: 记忆来源：对话原文、世界事件、反思产物。
type MemorySource = Literal["utterance", "event", "reflection"]


class Observation(BaseModel):
    """喂入记忆层的一个原始回合。

    只承载观测本身，不含抽取结果——抽取是模块自己的事。
    world_time 为 None 表示故事内时间未知，不伪造。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    kind: ObservationKind
    text: str
    speaker: str | None = None  # 角色 id 或名字
    participants: list[str] = []  # 可选：本回合的在场者
    world_time: int | None = None  # 故事内时间；缺省表示未知

    @field_validator("text")
    @classmethod
    def _reject_blank_text(cls, value: str) -> str:
        """空观测没有意义，且会让后续逐条校验白白跑一遍。"""
        if not value.strip():
            raise ValueError("观测文本不能为空白")
        return value


class MemoryItem(BaseModel):
    """一条结构化召回条目。

    kind 是记忆来源，取值域由设计文档 §7.1 定死为三类（对话原文 Utterance /
    事件 Event / 反思 Reflection）；它是公开返回类型，调用方会按 kind 分支，
    故收成闭合类型而非自由 str。期一只产出 utterance，event / reflection
    在期二、期三落地。

    source_id 指回本源节点（Utterance / Event / Reflection 的主键），
    供调用方顺链溯源；score 是召回分数；world_time 缺省表示未知。
    """

    kind: MemorySource
    text: str
    source_id: str
    score: float
    world_time: int | None = None


class TraceStep(BaseModel):
    """一条召回依据：为什么想起了这条。

    source 标明依据来自哪一路召回，detail 是具体取值（向量距离、图路径、命中时间段）。
    """

    source: TraceSource
    detail: str


class MemoryContext(BaseModel):
    """召回出口产物。

    block 只是 items 与 trace 的可读渲染；后两者无条件返回，
    调用方永远拿得到事实底座，出问题也能解释「为什么想起了这个」。
    三者都有合法默认值：记忆为空是合法状态，不报错。
    """

    block: str = ""
    items: list[MemoryItem] = Field(default_factory=list)
    trace: list[TraceStep] = Field(default_factory=list)
