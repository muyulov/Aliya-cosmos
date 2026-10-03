"""记忆层摄取面板：观测入队与后续处理流水线的落点。

期一只立有界队列（背压），抽取、门控、落库在后续任务里补。
"""

from core.memory.ingest.queue import ObservationQueue

__all__ = ["ObservationQueue"]
