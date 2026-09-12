"""统一错误模型与全局异常处理器。

对外响应信封固定为 {code, message, data, request_id}：
- 业务异常由 AppError 子类表达，携带稳定的 code 与 HTTP 状态码。
- 未捕获异常统一记 E 级日志（含 traceback），对外只暴露 500 与 request_id。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar, cast

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.api.response import error_body
from core.logger import faces, log
from core.service.item_service import ItemNotFoundError


class AppError(Exception):
    """业务异常基类。

    code / status_code 是子类定制的类级常量（ClassVar）。
    message 兼作默认文案与实例文案：类上给默认值，构造函数可覆盖，
    因此声明为普通实例属性，子类覆写类属性作为默认值。
    """

    code: ClassVar[str] = "APP_ERROR"
    status_code: ClassVar[int] = status.HTTP_400_BAD_REQUEST
    default_message: ClassVar[str] = "请求处理失败"

    message: str
    extra: dict[str, object]

    def __init__(
        self,
        message: str | None = None,
        *,
        extra: dict[str, object] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.extra = extra or {}
        super().__init__(self.message)


class NotFoundError(AppError):
    """资源不存在。"""

    code: ClassVar[str] = "NOT_FOUND"
    status_code: ClassVar[int] = status.HTTP_404_NOT_FOUND
    default_message: ClassVar[str] = "资源不存在"


class ConflictError(AppError):
    """资源冲突。"""

    code: ClassVar[str] = "CONFLICT"
    status_code: ClassVar[int] = status.HTTP_409_CONFLICT
    default_message: ClassVar[str] = "资源冲突"


class ValidationError(AppError):
    """请求参数不合法。"""

    code: ClassVar[str] = "INVALID_ARGUMENT"
    status_code: ClassVar[int] = status.HTTP_422_UNPROCESSABLE_CONTENT
    default_message: ClassVar[str] = "请求参数不合法"


def register_exception_handlers(app: FastAPI) -> None:
    """注册全部异常处理器。"""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        log.warning(
            "业务异常",
            face=faces.THINK,
            路径=request.url.path,
            错误码=exc.code,
            原因=exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, _request_id(request), exc.extra),
        )

    @app.exception_handler(ItemNotFoundError)
    async def _handle_item_not_found(request: Request, exc: ItemNotFoundError) -> JSONResponse:
        log.warning(
            "条目不存在",
            face=faces.THINK,
            路径=request.url.path,
            条目编号=exc.item_id,
        )
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=error_body("ITEM_NOT_FOUND", str(exc), _request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        log.warning(
            "参数校验失败",
            face=faces.THINK,
            路径=request.url.path,
            问题数=len(exc.errors()),
        )
        detail = _describe_validation_errors(exc)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=error_body(
                "INVALID_ARGUMENT", "请求参数不合法", _request_id(request), {"错误": detail}
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(f"HTTP_{exc.status_code}", str(exc.detail), _request_id(request)),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # exception 会带上完整堆栈，仅在此处记录，对外不暴露。
        # request_id 必须显式传入：本处理器由 ServerErrorMiddleware 在最外层执行，
        # 此时 RequestContextMiddleware 的上下文已退出，从上下文取会得到占位符。
        request_id = _request_id(request)
        log.exception(
            "未捕获异常",
            face=faces.BOOM,
            request_id=request_id,
            路径=request.url.path,
            异常=type(exc).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body("INTERNAL_ERROR", "服务内部错误", request_id),
        )


def _request_id(request: Request) -> str:
    """取请求 id，用于填充响应信封与异常日志字段。

    只读 request.state：中间件一进入就写入该值，且在所有异常处理器路径下都可靠；
    而日志上下文在 500 处理器执行时已退出，不能作为来源。
    """
    return cast("str", getattr(request.state, "request_id", "-"))


def _describe_validation_errors(exc: RequestValidationError) -> list[dict[str, object]]:
    """把 pydantic 的校验错误整理成可读的中文结构。

    exc.errors() 的元素是 pydantic 的 ErrorDetails（TypedDict 的松散形态），
    类型检查器视为 Any，因此这里显式规范化为 str 键的 dict，避免 Any 扩散。
    """
    raw_errors = cast("Sequence[object]", list(exc.errors()))
    described: list[dict[str, object]] = []
    for raw in raw_errors:
        item = cast("Mapping[str, object]", raw)
        location: object = item.get("loc") or []
        places: list[str] = []
        if isinstance(location, (list, tuple)):
            places = [str(part) for part in cast("Sequence[object]", location)]
        described.append({"位置": places, "原因": str(item.get("msg", ""))})
    return described
