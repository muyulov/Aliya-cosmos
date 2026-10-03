"""记忆层摄取面板：观测入队、失败补偿与后续处理流水线的落点。

期一已立两件：有界队列（背压，过载丢弃）与重试/死信（故障补偿）。
抽取、门控、落库在后续任务里补。
"""

from core.memory.ingest.queue import ObservationQueue, PendingObservation
from core.memory.ingest.retry import (
    STAGE_STRUCTURED,
    STAGE_UTTERANCE,
    DeadLetter,
    DeadLetterBox,
    RetryPolicy,
    is_retryable,
    run_with_retry,
)

__all__ = [
    "STAGE_STRUCTURED",
    "STAGE_UTTERANCE",
    "DeadLetter",
    "DeadLetterBox",
    "ObservationQueue",
    "PendingObservation",
    "RetryPolicy",
    "is_retryable",
    "run_with_retry",
]
