"""日志模块对外出口。

用法：
    from core.logger import log, setup_logging

    setup_logging()
    log.info("用户回合已入队", 参与者="qq:6329133635628374381", 已取消旧计划=0)

导入顺序说明：子模块统一用 `import core.logger.faces as faces_module` 这类绝对
子模块导入（而非 `from core.logger import faces`），避免 `__init__ → setup →
formatters → __init__` 的导入环。本文件按"无依赖 → 有依赖"的顺序导出：
context / faces 先行，formatters 次之，setup 最后。

注意：经包路径 `from core.logger import context` 导入会触发本文件执行，从而
连带加载 loguru 等全部依赖。若需在极轻量场景单独使用上下文，直接
`from core.logger.context import ...` 仍会初始化本包，这是包结构的固有限制。
"""

from __future__ import annotations

from core.logger import context, faces
from core.logger.formatters import colorize, format_exception, format_json, format_tree
from core.logger.setup import Log, log, setup_logging

__all__ = [
    "Log",
    "colorize",
    "context",
    "faces",
    "format_exception",
    "format_json",
    "format_tree",
    "log",
    "setup_logging",
]
