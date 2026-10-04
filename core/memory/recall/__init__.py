"""记忆层召回面板：语义、结构化、组装三支。

- `semantic`：查询向量经 Kùzu 的 HNSW 取最近的原文；
- `structured`：按角色取当前状态；
- `assemble`：确定性模板渲染、预算裁剪、召回依据。

读路径只读图库，唯一副作用（命中条目的访问回写）由 `MemoryService` 编排、经 worker
串行落库，不在此面板里直写。
"""

from core.memory.recall.assemble import build_trace, fit_budget, render_block
from core.memory.recall.semantic import SemanticHit, recall_utterances
from core.memory.recall.structured import recall_states

__all__ = [
    "SemanticHit",
    "build_trace",
    "fit_budget",
    "recall_states",
    "recall_utterances",
    "render_block",
]
