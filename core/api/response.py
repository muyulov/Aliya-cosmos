"""统一响应信封。

成功：{code: "OK", message: "success", data: ..., request_id: "..."}
失败：{code: "错误码", message: "原因", data: null, request_id: "..."}

信封本身会被 FastAPI 序列化为 JSON，因此 data 用 object 而非 Any，
既表达「任意可序列化值」，又不触发类型检查器的裸 Any 告警。
"""

from __future__ import annotations


def ok_body(
    data: object = None,
    request_id: str = "-",
    *,
    message: str = "success",
) -> dict[str, object]:
    """构造成功响应体。"""
    return {"code": "OK", "message": message, "data": data, "request_id": request_id}


def error_body(
    code: str,
    message: str,
    request_id: str = "-",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """构造失败响应体。extra 会合并进 data，便于携带细节。"""
    return {"code": code, "message": message, "data": extra or None, "request_id": request_id}
