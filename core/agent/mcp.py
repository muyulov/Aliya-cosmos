"""MCP 客户端：把远端 server 的工具映射成本地 Tool。

约定：
- 配置固定读 `data/mcp.json`（标准 mcpServers 结构），占位符复用 core.config 的插值，
  因此 `${VAR}` / `${VAR:默认}` 的语义与 cosmos.yaml 完全一致。
- 生命周期由调用方掌握：`async with MCPManager.from_file() as mcp`。
- 每个 server 一个后台任务持有传输的 `async with` 作用域——传输一退出连接就断，
  任务把「连接存活期」变成显式的 start / stop。
- 建连失败不静默跳过：跳过会让工具无声消失；抛错并关闭已连的 server。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import ClassVar, Literal, Self, cast

import httpx2
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from core.agent.errors import MCPConfigError, MCPConnectionError, MCPTimeoutError
from core.agent.tool import Tool
from core.config.loader import ENV_FILE, ConfigError, interpolate, read_env
from core.logger import log

#: MCP 配置文件，相对当前工作目录
MCP_CONFIG_FILE = Path("data/mcp.json")
#: 工具名的长度上限（OpenAI function name 限制 64 字符）
MAX_TOOL_NAME = 64


class MCPServerSettings(BaseModel):
    """一个 MCP server 的配置。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", populate_by_name=True)

    name: str = Field(min_length=1)
    #: 配置文件里沿用生态通用的 `type`，代码里叫 transport
    transport: Literal["stdio", "http"] = Field(default="stdio", alias="type")
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    #: 建连 + initialize + list_tools 的总上限（秒）
    timeout: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _check_required(self) -> Self:
        """两种传输各自必填的字段，在配置加载期就报错。"""
        if self.transport == "stdio" and not self.command:
            raise ValueError(f"stdio 传输必须提供 command：{self.name}")
        if self.transport == "http" and not self.url:
            raise ValueError(f"http 传输必须提供 url：{self.name}")
        return self


class MCPConfig(BaseModel):
    """`data/mcp.json` 的内容：mcpServers 映射，键即 server 名。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True, extra="ignore")

    servers: list[MCPServerSettings] = Field(default_factory=list, alias="mcpServers")

    @field_validator("servers", mode="before")
    @classmethod
    def _expand_servers(cls, value: object) -> object:
        """把 `{名字: 配置}` 展开成带 name 的列表；键名优先于配置里的 name。"""
        if not isinstance(value, dict):
            return value
        items: list[object] = []
        for name, config in cast("dict[object, object]", value).items():
            if not isinstance(config, dict):
                raise ValueError(f"server {name} 的配置必须是对象")
            items.append({**cast("dict[str, object]", config), "name": str(name)})
        return items

    @classmethod
    def load(cls, path: Path = MCP_CONFIG_FILE, env_file: Path = ENV_FILE) -> MCPConfig:
        """读文件 → 占位符插值 → 校验。文件不存在按未配置处理。"""
        if not path.is_file():
            log.warning("MCP 配置文件不存在，按未配置处理", 文件=str(path))
            return cls()
        try:
            raw = cast("object", json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPConfigError(f"MCP 配置读取失败：{path}：{exc}") from exc
        if not isinstance(raw, dict):
            raise MCPConfigError(f"MCP 配置顶层必须是对象：{path}")
        try:
            data = interpolate(cast("dict[str, object]", raw), read_env(env_file))
        except ConfigError as exc:
            raise MCPConfigError(f"MCP 配置占位符取值失败：{path}：{exc}") from exc
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise MCPConfigError(f"MCP 配置不合法：{path}：{exc}") from exc


#: 建连工厂：yield 出一个已完成 initialize 的会话
type Connector = Callable[[MCPServerSettings], AbstractAsyncContextManager[ClientSession]]


class MCPManager:
    """管理多个 MCP server 的连接与工具映射。"""

    def __init__(self, config: MCPConfig, *, connector: Connector | None = None) -> None:
        self._config: MCPConfig = config
        self._connector: Connector = _connect if connector is None else connector
        self._tasks: list[asyncio.Task[None]] = []
        self._stop: asyncio.Event = asyncio.Event()
        self._tools: tuple[Tool, ...] = ()

    @classmethod
    def from_file(
        cls,
        path: Path = MCP_CONFIG_FILE,
        env_file: Path = ENV_FILE,
        *,
        connector: Connector | None = None,
    ) -> MCPManager:
        """从配置文件构造；`connector` 仅供测试注入内存传输。"""
        return cls(MCPConfig.load(path, env_file), connector=connector)

    @property
    def tools(self) -> tuple[Tool, ...]:
        """启动时缓存的工具集合；未启动为空。"""
        return self._tools

    async def __aenter__(self) -> MCPManager:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        """并发建连全部 server，等它们就绪；任一失败即回滚并抛错。"""
        if self._tasks:
            return
        servers = self._config.servers
        if not servers:
            log.debug("未配置 MCP server，跳过连接")
            return
        self._stop = asyncio.Event()
        ready: dict[str, asyncio.Event] = {server.name: asyncio.Event() for server in servers}
        failures: dict[str, BaseException] = {}
        collected: list[Tool] = []
        self._tasks = [
            asyncio.create_task(
                self._serve(server, ready[server.name], collected, failures),
                name=f"mcp-{server.name}",
            )
            for server in servers
        ]
        for server in servers:
            try:
                async with asyncio.timeout(server.timeout):
                    _ = await ready[server.name].wait()
            except TimeoutError as exc:
                await self._shutdown()
                raise MCPTimeoutError(
                    f"连接 MCP server {server.name} 超时（{server.timeout} 秒）"
                ) from exc
            failure = failures.get(server.name)
            if failure is not None:
                await self._shutdown()
                raise MCPConnectionError(
                    f"连接 MCP server {server.name} 失败：{failure}"
                ) from failure
        self._tools = tuple(collected)
        log.info("MCP server 已连接", 数量=len(servers), 工具数=len(self._tools))

    async def stop(self) -> None:
        """断开全部连接；幂等，未启动时直接返回。"""
        if not self._tasks:
            return
        await self._shutdown()
        log.debug("MCP 已断开")

    async def _serve(
        self,
        server: MCPServerSettings,
        ready: asyncio.Event,
        sink: list[Tool],
        failures: dict[str, BaseException],
    ) -> None:
        """一个 server 的连接任务：连上 → 列工具 → 守着停止事件。"""
        try:
            async with self._connector(server) as session:
                listed = await session.list_tools()
                sink.extend(_build_tools(server, listed.tools, session))
                _ = ready.set()
                _ = await self._stop.wait()
        except Exception as exc:
            # 记录而不是抛出：start() 等着读 failures，任务异常也不需要额外消费
            failures[server.name] = exc
        finally:
            _ = ready.set()

    async def _shutdown(self) -> None:
        """置停止事件、取消任务并等它们退出；清空缓存的工具。"""
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            _ = task.cancel()
        if tasks:
            # 取消可能让传输的 __aexit__ 抛 ExceptionGroup，这里一并吞掉
            _ = await asyncio.gather(*tasks, return_exceptions=True)
        self._tools = ()


def _build_tools(
    server: MCPServerSettings, listed: Sequence[types.Tool], session: ClientSession
) -> list[Tool]:
    """把 server 的工具描述映射成本地 Tool。"""
    return [_build_tool(server, spec, session) for spec in listed]


def _build_tool(server: MCPServerSettings, spec: types.Tool, session: ClientSession) -> Tool:
    parameters = cast("dict[str, object]", spec.input_schema)
    description = (spec.description or "").strip() or spec.name

    async def invoke(arguments: str) -> str:
        payload, error = _parse_arguments(arguments)
        if error is not None:
            return error
        # allow_input_required 默认 False，因此这里只可能是普通结果
        result = await session.call_tool(spec.name, arguments=payload)
        return _render_result(result)

    return Tool(
        name=_tool_name(server.name, spec.name),
        description=description,
        parameters=parameters,
        invoke=invoke,
    )


def _tool_name(server: str, name: str) -> str:
    """`{server}__{tool}`：非法字符换成下划线，超长截断。"""
    raw = f"{server}__{name}"
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if len(cleaned) > MAX_TOOL_NAME:
        log.warning("MCP 工具名超长，已截断", 工具=raw)
        return cleaned[:MAX_TOOL_NAME]
    return cleaned


def _parse_arguments(raw: str) -> tuple[dict[str, object] | None, str | None]:
    """JSON 字符串 → 字典；失败时返回回填文本，不抛。"""
    text = raw.strip()
    if not text:
        return {}, None
    try:
        value = cast("object", json.loads(text))
    except json.JSONDecodeError as exc:
        return None, f"参数校验失败：arguments 不是合法 JSON（{exc.msg}）"
    if not isinstance(value, dict):
        return None, "参数校验失败：arguments 必须是 JSON 对象"
    return cast("dict[str, object]", value), None


def _render_result(result: types.CallToolResult) -> str:
    """把内容块拼成文本；拿不到文本时退回结构化结果。"""
    parts: list[str] = []
    for item in result.content:
        if isinstance(item, types.TextContent):
            parts.append(item.text)
        elif isinstance(item, types.ImageContent):
            parts.append(f"[图片 {item.mime_type}]")
        elif isinstance(item, types.EmbeddedResource):
            resource = item.resource
            if isinstance(resource, types.TextResourceContents):
                parts.append(resource.text)
            else:
                parts.append(f"[资源 {resource.uri}]")
        else:
            parts.append(f"[{item.type}]")
    text = "\n".join(part for part in parts if part)
    structured = cast("object", result.structured_content)
    if not text and structured is not None:
        text = json.dumps(structured, ensure_ascii=False, default=str)
    if not text:
        text = "ok"
    if result.is_error:
        return f"工具执行失败：{text}"
    return text


@asynccontextmanager
async def _connect(server: MCPServerSettings) -> AsyncGenerator[ClientSession]:  # pragma: no cover
    """默认建连：stdio 起子进程，http 走 streamable HTTP；完成后才交给 manager。

    需要真实子进程 / 远端端点，测试里一律用 connector 注入内存传输。
    """
    if server.transport == "stdio":
        params = StdioServerParameters(
            command=server.command or "", args=server.args, env=server.env or None
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            _ = await session.initialize()
            yield session
        return
    async with (
        httpx2.AsyncClient(headers=server.headers) as client,
        streamable_http_client(server.url or "", http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        _ = await session.initialize()
        yield session
