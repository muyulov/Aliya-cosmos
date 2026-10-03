"""记忆层对外出口。

用法：
    from core.memory import Observation

    await service.observe(Observation(kind="dialogue", text="风起了", speaker="alice"))

观测从写入口进来，召回结构从读出口出去；错误树让调用方一次 except 兜住整层。
"""

from core.memory.errors import (
    MemoryConfigError,
    MemoryError,
    MemoryExtractError,
    MemoryInputError,
    MemoryStoreError,
)
from core.memory.models import (
    MemoryContext,
    MemoryItem,
    Observation,
    TraceStep,
)
from core.memory.service import MemoryService

__all__ = [
    "MemoryConfigError",
    "MemoryContext",
    "MemoryError",
    "MemoryExtractError",
    "MemoryInputError",
    "MemoryItem",
    "MemoryService",
    "MemoryStoreError",
    "Observation",
    "TraceStep",
]
