"""日志上下文。

为什么用 contextvars：request_id 这类维度属于"横切"信息，希望调用点零手写、
全链路自动携带。loguru 的 logger.configure(extra=...) 是全局的，多请求并发时
会互相覆盖；contextvars 按协程隔离，配合 reset 保证嵌套场景正确回退。

本模块不依赖 loguru，只依赖标准库，因此可被 api 层直接使用而不引入框架耦合。
值类型限制为 str：上下文会跨 await 传播并写入日志，限制成字符串可避免往上下文
塞大对象导致引用滞留。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

#: 上下文快照。不可变用法：每次 bind 生成新字典。
#: 默认值取 None 而非 {}，避免可变对象作为默认值被跨上下文共享（ruff B039）。
_context: ContextVar[dict[str, str] | None] = ContextVar("logger_context", default=None)

#: 上下文里的保留键
REQUEST_ID = "request_id"

#: request_id 缺失时的占位符
MISSING = "-"

ContextToken = Token[dict[str, str] | None]


def _snapshot() -> dict[str, str]:
    """取当前上下文快照，未绑定时为只读空字典。"""
    return _context.get() or {}


def current() -> dict[str, str]:
    """读出当前上下文快照（副本，外部改动不影响内部）。"""
    return dict(_snapshot())


def bind(**kv: object) -> ContextToken:
    """把键值并入当前上下文，返回用于还原的 token。

    值统一转成字符串，保证上下文里只有 str。
    """
    merged = {**_snapshot(), **{key: str(value) for key, value in kv.items()}}
    return _context.set(merged)


def reset(token: ContextToken) -> None:
    """还原到 bind 之前的状态。"""
    _context.reset(token)


def request_id() -> str:
    """取当前 request_id，缺失时返回占位符。"""
    return _snapshot().get(REQUEST_ID, MISSING)


def new_request_id() -> str:
    """生成新的 request_id（16 位十六进制）。"""
    return uuid.uuid4().hex[:16]


@contextmanager
def request_scope(request_id_value: str | None = None) -> Iterator[str]:
    """进入请求上下文，退出时自动还原。

    传入为空时自动生成 id，yield 出最终生效的 id。
    """
    rid = request_id_value or new_request_id()
    token = bind(**{REQUEST_ID: rid})
    try:
        yield rid
    finally:
        reset(token)


__all__ = [
    "MISSING",
    "REQUEST_ID",
    "ContextToken",
    "bind",
    "current",
    "new_request_id",
    "request_id",
    "request_scope",
    "reset",
]
