"""联网台：调用 DeepSeek 的联网搜索与链接阅读能力。

搜索与抓取都在服务端完成（文档：guides/web_tools），本模块只负责声明工具、
发一次 Chat Completions 请求并整理回答与引用；不下载网页，也不做 SSRF 判断。

配置复用 `llm.chat` 端点：服务端工具要求模型固定为 `deepseek-flash`，
且必须配置 API Key；未配置时直接回 `not_configured`，不发请求。
"""

from __future__ import annotations

import time
from typing import cast

import httpx2

from agent.cosmos.contracts import (
    AgentCard,
    AgentContext,
    AgentExample,
    AgentRequest,
    AgentResult,
    DesktopMeta,
    ExecutionPolicy,
)
from core.config import get_settings
from core.logger import log

#: DeepSeek 服务端工具要求的模型
SEARCH_MODEL = "deepseek-flash"
#: 单次请求超时（秒）；联网搜索比普通对话慢
SEARCH_TIMEOUT_SECONDS = 90.0
#: 返回正文默认字符数；0 表示不截断
DEFAULT_MAX_CHARS = 0
#: 最多回传几条引用
MAX_CITATIONS = 8


CARD = AgentCard(
    id="web",
    name="联网台",
    icon="🌐",
    summary="联网搜索并读链接",
    description=(
        "把问题交给 DeepSeek 联网搜索，返回带引用的回答；可选阅读指定链接。"
        "搜索与抓取在服务端完成，本工具不下载页面；需要 llm.chat 配置 DeepSeek API Key。"
        "不支持限定域名、地理位置或强制指定搜索工具。"
    ),
    capabilities=["web_search", "read_page"],
    params_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要查的问题；读链接时写成想从链接里得到什么",
            },
            "url": {"type": "string", "description": "可选：要阅读的链接"},
            "max_chars": {
                "type": "integer",
                "minimum": 0,
                "description": "可选上限；0 或缺省表示不截断",
            },
        },
        "required": ["query"],
    },
    examples=[
        AgentExample(input="查一下今天有哪些关于这个项目的新闻"),
        AgentExample(input="读一下这个链接说了什么", note="配合 url 参数"),
    ],
    desktop=DesktopMeta(group="web", order=10),
    execution=ExecutionPolicy(
        timeout_seconds=120, max_concurrent=2, queue=True, retryable=True, heartbeat_seconds=20
    ),
)


class WebNotConfigured(Exception):
    """没有可用的 DeepSeek 密钥，无法发起搜索。"""


class WebSearchError(Exception):
    """搜索请求失败；按 code 与 retriable 决定是否重试。"""

    def __init__(self, code: str, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message
        self.retriable: bool = retriable


async def handle(request: AgentRequest, ctx: AgentContext) -> AgentResult:
    query = _text(request.params.get("query"))
    url = _text(request.params.get("url"))
    limit = max(_int(request.params.get("max_chars"), default=DEFAULT_MAX_CHARS), 0)

    if not query:
        return _failed(request, "缺少 query", "需要说明要查什么")
    if url and not _is_http_url(url):
        return _failed(request, "url 必须是 http/https 链接", "检查链接是否完整")

    if ctx.cancelled:
        return AgentResult.cancelled(request_id=request.request_id)
    await ctx.report(progress=30, step="联网查询")

    try:
        answer, citations = await request_search(query, url or None)
    except WebNotConfigured:
        return _failed(
            request,
            "没有配置 DeepSeek 密钥",
            "先在 .env 里配置 DEEPSEEK_API_KEY",
            code="not_configured",
        )
    except WebSearchError as exc:
        return AgentResult.failed(
            code=exc.code,
            message=exc.message,
            retriable=exc.retriable,
            user_hint="稍后再试" if exc.retriable else "换个问法或链接再试",
            request_id=request.request_id,
        )

    if not answer.strip():
        return AgentResult.failed(
            code="empty_response",
            message="搜索没有给出内容",
            retriable=True,
            user_hint="没查到有用的信息，可以换个问法",
            request_id=request.request_id,
        )

    await ctx.report(progress=90, step="整理结果")
    text = answer.strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "\n（正文已截断）"
    body = text
    if citations:
        lines = [f"- {title}：{link}" if title else f"- {link}" for title, link in citations]
        body += "\n\n参考：\n" + "\n".join(lines)
    summary = "读完了链接" if url else "搜到结果"
    if citations:
        summary += f"，{len(citations)} 条引用"
    return AgentResult.ok(
        summary=summary,
        content=body,
        request_id=request.request_id,
    )


async def request_search(query: str, url: str | None) -> tuple[str, list[tuple[str, str]]]:
    """向 DeepSeek 发一次联网请求，返回 `(回答, [(标题, 链接)])`。

    独立成公开函数便于测试替换；实际搜索与抓取都由服务端完成。
    """
    settings = get_settings().llm.chat
    if not settings.api_key:
        raise WebNotConfigured

    prompt = f"阅读这个链接并回答：{query}\n{url}" if url else f"联网搜索并回答：{query}"
    tools: list[dict[str, str]] = [
        {"type": "deepseek_read_url"} if url else {"type": "deepseek_web_search"}
    ]
    payload: dict[str, object] = {
        "model": SEARCH_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "tools": tools,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    endpoint = f"{settings.base_url.rstrip('/')}/chat/completions"

    started = time.monotonic()
    try:
        async with httpx2.AsyncClient(timeout=SEARCH_TIMEOUT_SECONDS) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
    except httpx2.TimeoutException as exc:
        raise WebSearchError("timeout", "联网搜索超时", retriable=True) from exc
    except httpx2.RequestError as exc:
        raise WebSearchError("network_error", f"联网搜索失败：{exc}", retriable=True) from exc

    if response.status_code != 200:
        raise WebSearchError(
            _code_for(response.status_code),
            _error_message(response),
            retriable=response.status_code == 429 or response.status_code >= 500,
        )

    try:
        data = cast("dict[str, object]", response.json())
    except ValueError as exc:
        raise WebSearchError("bad_response", "搜索返回的不是合法 JSON", retriable=True) from exc

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise WebSearchError("empty_response", "搜索没有返回结果", retriable=True)
    message = cast("dict[str, object]", choices[0]).get("message")
    if not isinstance(message, dict):
        raise WebSearchError("empty_response", "搜索没有返回内容", retriable=True)
    answer = cast("dict[str, object]", message).get("content")
    citations = extract_citations(cast("dict[str, object]", message).get("annotations"))

    log.debug(
        "联网搜索完成",
        耗时毫秒=round((time.monotonic() - started) * 1000),
        模式="read_url" if url else "search",
        回答字数=len(answer) if isinstance(answer, str) else 0,
        引用数=len(citations),
    )
    return (answer if isinstance(answer, str) else ""), citations


def extract_citations(raw: object) -> list[tuple[str, str]]:
    """从 `message.annotations` 取 url_citation；兼容嵌套与平铺两种形态。

    同一链接可能被多次引用，按 URL 去重并保持首次出现的顺序。
    """
    if not isinstance(raw, list):
        return []
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            continue
        data = cast("dict[str, object]", item)
        nested = data.get("url_citation")
        if isinstance(nested, dict):
            data = cast("dict[str, object]", nested)
        url = data.get("url")
        if not isinstance(url, str) or not url or url in seen:
            continue
        seen.add(url)
        title = data.get("title")
        found.append((title if isinstance(title, str) else "", url))
        if len(found) >= MAX_CITATIONS:
            break
    return found


def _code_for(status: int) -> str:
    if status in {401, 403}:
        return "auth_error"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    return "bad_request"


def _error_message(response: httpx2.Response) -> str:
    """取对端错误说明；只保留 message，不把整个响应体回给模型。"""
    try:
        data = cast("object", response.json())
    except ValueError:
        return f"服务端返回 {response.status_code}"
    if isinstance(data, dict):
        error = cast("dict[str, object]", data).get("error")
        if isinstance(error, dict):
            message = cast("dict[str, object]", error).get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()[:200]
    return f"服务端返回 {response.status_code}"


def _is_http_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return default


def _failed(
    request: AgentRequest,
    message: str,
    hint: str,
    *,
    code: str = "invalid_params",
) -> AgentResult:
    return AgentResult.failed(
        code=code,
        message=message,
        retriable=False,
        user_hint=hint,
        request_id=request.request_id,
    )
