"""日志模块对外出口。

用法：
    from core.logger import log, setup_logging

    setup_logging()
    log.info("用户回合已入队", 参与者="qq:6329133635628374381", 已取消旧计划=0)

导入顺序说明：formatters 会 `from core.logger import faces`，若本文件在顶层
再导入 setup，就会形成 `__init__ → setup → formatters → __init__` 的导入环。
因此这里先导入 faces（供子模块使用），再导入 setup，且不使用包内互相回导。
"""

from __future__ import annotations

from core.logger import context, faces
from core.logger.formatters import colorize, format_json, format_tree
from core.logger.setup import Log, log, setup_logging

__all__ = [
    "Log",
    "colorize",
    "context",
    "faces",
    "format_json",
    "format_tree",
    "log",
    "setup_logging",
]
