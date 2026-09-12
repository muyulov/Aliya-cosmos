"""日志模块的类型定义。

为什么单独成文件：
- 集中放类型别名，避免在各模块里散布 Any。
- 打断 core.logger 包内的导入环：__init__ → setup → formatters → types，
  types 不反向导入任何同级模块。

loguru 的 Record / Level / FilterFunction 只存在于类型存根（.pyi）中，运行时
不可导入，因此放在 TYPE_CHECKING 下；同时给出运行时兜底别名，供
`from core.logger.types import Record` 这类运行时导入使用（注解在
`from __future__ import annotations` 下是字符串，兜底只保证导入不报错）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loguru import FilterFunction, Level, Record
else:
    # 运行时兜底：仅为满足 import 语句，不参与实际类型检查
    FilterFunction = Callable[[Any], bool]
    Level = Any
    Record = Any

#: 结构化字段：日志调用传入的额外 kwargs
LogFields = Mapping[str, object]

#: 可变的结构化字段
MutableLogFields = MutableMapping[str, object]

__all__ = [
    "FilterFunction",
    "Level",
    "LogFields",
    "MutableLogFields",
    "Record",
]
