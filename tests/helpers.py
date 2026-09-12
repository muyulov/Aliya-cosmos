"""测试辅助函数。

httpx 的 Response.json() 返回 Any，直接在用例里取值会让类型检查器一路报警。
这里统一收口，让用例只需一次显式转换即可安全取值。
"""

from __future__ import annotations

from typing import cast

from httpx import Response

JsonDict = dict[str, object]


def body(resp: Response) -> JsonDict:
    """把响应体解析为 dict，用于断言。"""
    return cast("JsonDict", resp.json())


def data_dict(resp: Response) -> JsonDict:
    """取出统一信封里的 data 字段，并断言它是对象。"""
    data = body(resp).get("data")
    assert isinstance(data, dict), f"data 应为对象，实际为 {type(data).__name__}"
    return cast("JsonDict", data)


def data_list(resp: Response) -> list[object]:
    """取出统一信封里的 data 字段，并断言它是数组。"""
    data = body(resp).get("data")
    assert isinstance(data, list), f"data 应为数组，实际为 {type(data).__name__}"
    return cast("list[object]", data)


def as_dict(value: object) -> JsonDict:
    """把任意值断言为对象。"""
    assert isinstance(value, dict), f"应为对象，实际为 {type(value).__name__}"
    return cast("JsonDict", value)
