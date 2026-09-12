"""颜文字常量表。

调用日志时可用 face= 手动指定，未指定时按级别取默认值。
"""

from __future__ import annotations

CHEER = "(^_^)/"
START = "(•̀ᴗ•́)"
LOVE = "(*^▽^*)"
THINK = "(・_・;)"
SLEEP = "(－_－) zzZ"
BUG = "(╯°□°)╯"
BOOM = "(x_x)"
BYE = "(・ω・)ノ"

#: 按日志级别提供的默认颜文字
DEFAULT_BY_LEVEL: dict[str, str] = {
    "DEBUG": THINK,
    "INFO": CHEER,
    "WARNING": BUG,
    "ERROR": BOOM,
    "CRITICAL": BUG,
}
