"""联网台：抓取公开网页并转成纯文本。

安全边界：
- 只允许 http/https，跳转后的地址同样要过检查；
- 拒绝回环、私网、链路本地与保留地址，避免被当成内网探测器（SSRF）；
- 只收文本类响应，正文按上限截断。
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from urllib.parse import urlparse

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

#: 抓取总超时（秒）
FETCH_TIMEOUT_SECONDS = 15
#: 响应体上限（字节）；超过直接拒绝，不边下边裁
MAX_BODY_BYTES = 2_000_000
#: 返回正文默认与最大字符数
DEFAULT_MAX_CHARS = 4_000
MAX_MAX_CHARS = 20_000

_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")


CARD = AgentCard(
    id="web",
    name="联网台",
    icon="🌐",
    summary="抓取公开网页正文",
    description=(
        "抓取一个公开网页并转成纯文本，适合读文章、文档与公告。"
        "不支持登录、提交表单、下载文件；只访问公网地址，拒绝本机与内网。"
    ),
    capabilities=["web_fetch", "read_page"],
    params_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "以 http:// 或 https:// 开头的地址"},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000},
        },
        "required": ["url"],
    },
    examples=[
        AgentExample(input="读一下这篇公告的内容"),
        AgentExample(input="把这个文档页面的正文抓下来"),
    ],
    desktop=DesktopMeta(group="web", order=10),
    execution=ExecutionPolicy(
        timeout_seconds=60, max_concurrent=2, queue=True, retryable=True, heartbeat_seconds=15
    ),
)


async def handle(request: AgentRequest, ctx: AgentContext) -> AgentResult:
    url = _text(request.params.get("url"))
    limit = min(
        max(_int(request.params.get("max_chars"), default=DEFAULT_MAX_CHARS), 200),
        MAX_MAX_CHARS,
    )

    error = _validate_url(url)
    if error is not None:
        return _failed(request, error, "只支持公开的 http/https 地址")

    if ctx.cancelled:
        return AgentResult.cancelled(request_id=request.request_id)
    await ctx.report(progress=20, step="检查地址")

    await ctx.report(progress=50, step="抓取中")
    try:
        status, content_type, body = await _fetch(url)
    except BlockedHost as exc:
        return _failed(request, f"拒绝访问本机或内网地址：{exc}", "只能抓公网上的页面")
    except _DnsError as exc:
        return _failed(request, f"域名解析失败：{exc}", "地址可能写错了", retriable=True)
    except _BodyTooLarge as exc:
        return AgentResult.failed(
            code="too_large",
            message=str(exc),
            retriable=False,
            user_hint="页面太大了，换一个小一点的页面",
            request_id=request.request_id,
        )
    except httpx2.TimeoutException:
        return AgentResult.failed(
            code="timeout",
            message="抓取超时",
            retriable=True,
            user_hint="对方网站太慢了，过一会儿再试",
            request_id=request.request_id,
        )
    except httpx2.RequestError as exc:
        return AgentResult.failed(
            code="network_error",
            message=f"抓取失败：{exc}",
            retriable=True,
            user_hint="网络没通，过一会儿再试",
            request_id=request.request_id,
        )

    if not _is_text(content_type):
        return _failed(
            request,
            f"不是文本页面：{content_type or '未知类型'}",
            "这个地址返回的是文件",
        )
    if status >= 400:
        return AgentResult.failed(
            code="http_error",
            message=f"对方返回 {status}",
            retriable=status >= 500,
            user_hint="页面打不开，可能已经删了",
            request_id=request.request_id,
        )

    text = body if "json" in content_type.lower() else _to_text(body)
    truncated = len(text) > limit
    if truncated:
        text = text[:limit].rstrip() + "\n（正文已截断）"

    await ctx.report(progress=90, step="整理正文")
    title = _title(body) or url
    return AgentResult.ok(
        summary=f"读到了《{title[:30]}》",
        content=f"地址：{url}\n状态：{status}\n\n{text}",
        request_id=request.request_id,
    )


class _BodyTooLarge(Exception):
    """响应体超过上限，按输入问题处理。"""


class BlockedHost(Exception):
    """目标主机不是公网地址。"""


class _DnsError(Exception):
    """主机名解析失败。"""


async def guard_request(request: httpx2.Request) -> None:
    """每一跳都校验目标主机，防止 302 跳到内网。

    httpx 的 request 钩子在每次重定向时都会触发，因此这里能覆盖首跳与后续跳转。
    DNS 解析放线程池：同步钩子会阻塞事件循环。
    """
    host = request.url.host or ""
    try:
        reachable = await asyncio.to_thread(_host_is_public, host)
    except OSError as exc:
        raise _DnsError(str(exc)) from exc
    if not reachable:
        raise BlockedHost(host)


async def _fetch(url: str) -> tuple[int, str, str]:
    """流式读取并限制总量；先看 content-type，非文本不下载正文。"""
    headers = {"User-Agent": "AliyaCosmos/0.1 (+public page reader)"}
    async with httpx2.AsyncClient(
        timeout=FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=headers,
        event_hooks={"request": [guard_request]},
    ) as client, client.stream("GET", url) as response:
        content_type = response.headers.get("content-type", "")
        if not _is_text(content_type):
            return response.status_code, content_type, ""
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise _BodyTooLarge(f"响应体超过 {MAX_BODY_BYTES} 字节")
            chunks.append(chunk)
        return (
            response.status_code,
            content_type,
            b"".join(chunks).decode("utf-8", errors="replace"),
        )


def _validate_url(url: str) -> str | None:
    """协议与形式检查；解析结果放在 `_host_is_public` 里做。"""
    if not url:
        return "缺少 url"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return f"不支持的协议：{parsed.scheme or '空'}"
    if not parsed.hostname:
        return "地址缺少主机名"
    return None


def _host_is_public(host: str) -> bool:
    """域名解析后所有地址都必须是公网地址；任一私网即拒绝。"""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise OSError(str(exc)) from exc
    for info in infos:
        address = str(info[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return bool(infos)


def _is_text(content_type: str) -> bool:
    lowered = content_type.lower()
    if not lowered:
        return True
    return (
        lowered.startswith("text/")
        or "json" in lowered
        or "xml" in lowered
        or "xhtml" in lowered
    )


def _to_text(body: str) -> str:
    """去脚本、去标签、解实体、压空白；保留段落换行。"""
    text = _SCRIPT_RE.sub(" ", body)
    text = _TAG_RE.sub("\n", text)
    text = html.unescape(text)
    text = _SPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return _BLANK_RE.sub("\n\n", text).strip()


def _title(body: str) -> str:
    matched = re.search(r"<title\b[^>]*>(.*?)</title\s*>", body, re.I | re.S)
    if matched is None:
        return ""
    return html.unescape(_TAG_RE.sub("", matched.group(1))).strip()


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
    retriable: bool = False,
) -> AgentResult:
    return AgentResult.failed(
        code="invalid_params" if not retriable else "network_error",
        message=message,
        retriable=retriable,
        user_hint=hint,
        request_id=request.request_id,
    )
