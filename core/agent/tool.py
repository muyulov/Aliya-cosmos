"""工具模型：把带类型注解的函数推导成模型可调用的 Tool。

约定：
- 声明入口只有 `@tool` 装饰器：名称取函数名，描述取 docstring，参数 schema 由 pydantic
  从签名推导，参数描述取 docstring 的 `Args:` / `参数:` 段。
- 执行入口统一成 `invoke(原始 JSON) -> 回填文本`：本地工具在这里做参数校验与返回值归一，
  MCP 工具在做自己的映射，注册表只认这一条路径。
- 参数校验失败不抛，返回 `参数校验失败：…`；handler 的异常原样冒泡，由注册表回填。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import cast, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from pydantic.errors import PydanticSchemaGenerationError

from core.agent.errors import ToolDefinitionError

#: 执行入口：原始 arguments JSON → 回填文本
type Invoker = Callable[[str], Awaitable[str]]

#: docstring 里参数段的标题（Google 风格；也认中文）
_ARGS_HEADER = re.compile(r"^(?:Args|Arguments|参数)\s*[:：]\s*$")
#: 参数段之后的其它段落标题，遇到即停止解析
_NEXT_SECTION = re.compile(
    r"^(?:Returns?|Raises?|Yields?|Examples?|Notes?|Attributes?|返回|异常|示例|注意)\s*[:：]"
)
#: 一行参数：`name: 描述` 或 `name (type): 描述`，也接受 `*args` 写法（后续会报错）
_PARAM_LINE = re.compile(r"^(\*{0,2}[A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*[:：]\s*(.*)$")


@dataclass(frozen=True, slots=True)
class Tool:
    """一个可交给模型的工具。

    parameters 是完整的 JSON Schema（object）：本地工具由 pydantic 推导，
    MCP 工具原样透传服务端给的 inputSchema。
    """

    name: str
    description: str
    parameters: Mapping[str, object]
    invoke: Invoker
    #: 本地工具的原始函数；MCP 工具为 None
    func: Callable[..., object] | None = None

    def __call__(self, *args: object, **kwargs: object) -> object:
        """本地工具装饰后仍可直接调用；MCP 工具没有本地实现，调用即报错。"""
        if self.func is None:
            raise TypeError(f"工具 {self.name} 没有本地实现，只能通过注册表执行")
        return self.func(*args, **kwargs)


def tool(func: Callable[..., object]) -> Tool:
    """把一个普通函数声明成 Tool。

    没有 docstring 或描述为空时报错：描述是模型选工具的唯一依据，缺了等于让模型盲猜。
    """
    name = getattr(func, "__name__", None)
    if not isinstance(name, str) or not name:
        raise ToolDefinitionError("工具函数必须有名字")
    doc = inspect.getdoc(func) or ""
    description, param_docs = _split_docstring(doc) if doc.strip() else ("", {})
    if not description:
        raise ToolDefinitionError(f"工具 {name} 缺少 docstring 描述")
    model = _build_model(func, param_docs)
    return Tool(
        name=name,
        description=description,
        parameters=_schema(model),
        invoke=_invoke_for(func, model),
        func=func,
    )


def _split_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """把 docstring 拆成「正文描述」与「参数名 → 描述」。

    cleandoc 之后段落标题在行首、参数行有缩进，据此区分段落边界与参数续行。
    """
    lines = inspect.cleandoc(doc).splitlines()
    body: list[str] = []
    params: dict[str, list[str]] = {}
    current: str | None = None
    current_indent = 0
    in_args = False
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if not in_args:
            if indent == 0 and _ARGS_HEADER.match(stripped):
                in_args = True
                continue
            body.append(line)
            continue
        if not stripped:
            continue
        if indent == 0 and _NEXT_SECTION.match(stripped):
            break
        # 比参数行缩进更深的才是续行；同缩进的是下一个参数
        if current is not None and indent > current_indent:
            params[current].append(stripped)
            continue
        matched = _PARAM_LINE.match(stripped)
        if matched is None:
            continue
        key = matched.group(1).lstrip("*")
        current = key
        current_indent = indent
        text = matched.group(2).strip()
        params[key] = [text] if text else []
    description = "\n".join(body).strip()
    return description, {key: " ".join(parts).strip() for key, parts in params.items()}


def _build_model(
    func: Callable[..., object], param_docs: Mapping[str, str]
) -> type[BaseModel] | None:
    """按签名建参数模型；无参数返回 None。

    注解用 get_type_hints 解析：模块开了 `from __future__ import annotations`，
    signature 里拿到的是字符串，直接交给 pydantic 会当成前向引用。
    """
    try:
        hints = cast("dict[str, object]", get_type_hints(func))
    except NameError as exc:
        raise ToolDefinitionError(f"工具 {func.__name__} 的类型注解无法解析：{exc}") from exc
    fields: dict[str, object] = {}
    for name, parameter in inspect.signature(func).parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise ToolDefinitionError(f"工具 {func.__name__} 不支持 *args / **kwargs 参数：{name}")
        annotation = hints.get(name)
        if annotation is None:
            raise ToolDefinitionError(f"工具 {func.__name__} 的参数 {name} 缺少类型注解")
        description = param_docs.get(name, "")
        # Parameter.default 的注解就是 Any，这里只看「有没有默认值」与把值原样交给 pydantic
        default: object = parameter.default  # pyright: ignore[reportAny]
        field = (
            Field(description=description)
            if default is inspect.Parameter.empty
            else Field(default=default, description=description)
        )
        fields[name] = (annotation, field)
    if not fields:
        return None
    try:
        # extra="forbid"：多传的字段算模型违约，报错回填比静默忽略更容易纠正。
        # create_model 是动态签名（字段名即 kwargs），类型检查给不出精确重载
        created: object = create_model(  # pyright: ignore[reportCallIssue, reportUnknownVariableType]
            f"{func.__name__}Args",
            __config__=ConfigDict(extra="forbid"),
            **fields,  # pyright: ignore[reportArgumentType]
        )
        return cast("type[BaseModel]", created)
    except PydanticSchemaGenerationError as exc:
        raise ToolDefinitionError(f"工具 {func.__name__} 的参数类型不受支持：{exc}") from exc


def _schema(model: type[BaseModel] | None) -> dict[str, object]:
    """参数模型 → JSON Schema；无参工具给一个空对象 schema。"""
    if model is None:
        return {"type": "object", "properties": {}}
    return cast("dict[str, object]", model.model_json_schema())


def _invoke_for(func: Callable[..., object], model: type[BaseModel] | None) -> Invoker:
    """构造执行闭包：校验 → 调用（同步下线程）→ 归一。"""
    is_async = inspect.iscoroutinefunction(func)

    async def invoke(arguments: str) -> str:
        if model is None:
            kwargs: dict[str, object] = {}
        else:
            try:
                parsed = model.model_validate_json(arguments or "{}")
            except ValidationError as exc:
                return f"参数校验失败：{_describe_validation(exc)}"
            kwargs = cast("dict[str, object]", parsed.model_dump())
        if is_async:
            result = await cast("Awaitable[object]", func(**kwargs))
        else:
            # 同步工具可能读文件、发 HTTP、跑 CPU 计算，不能占住事件循环
            result = await asyncio.to_thread(func, **kwargs)
        return _render(result)

    return invoke


def _describe_validation(exc: ValidationError) -> str:
    """把 pydantic 的多行报告压成单行：`字段: 原因；字段: 原因`。"""
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "参数"
        parts.append(f"{location}: {error['msg']}")
    return "；".join(parts)


def _render(value: object) -> str:
    """返回值归一成文本：None → ok，字符串原样，模型与容器转 JSON。"""
    if value is None:
        return "ok"
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return json.dumps(value, ensure_ascii=False, default=str)
