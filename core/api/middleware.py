"""请求上下文中间件。

为每个请求生成 request_id，写入 request.state 与响应头，并进入日志上下文，
使该请求链路中所有日志自动携带该 id（无需调用点手写字段）。
请求结束时打一条 access 日志。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import override

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.logger import context as log_context
from core.logger import faces, log

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """注入 request_id 并记录访问日志。"""

    @override
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        with log_context.request_scope(request.headers.get(REQUEST_ID_HEADER)) as request_id:
            request.state.request_id = request_id

            begin = time.perf_counter()
            try:
                response = await call_next(request)
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - begin) * 1000, 1)
                log.error(
                    "请求处理异常",
                    face=faces.BOOM,
                    方法=request.method,
                    路径=request.url.path,
                    耗时=f"{elapsed_ms}ms",
                    异常=type(exc).__name__,
                )
                raise

            elapsed_ms = round((time.perf_counter() - begin) * 1000, 1)
            response.headers[REQUEST_ID_HEADER] = request_id
            log.info(
                "请求完成",
                方法=request.method,
                路径=request.url.path,
                状态码=response.status_code,
                耗时=f"{elapsed_ms}ms",
            )
            return response
