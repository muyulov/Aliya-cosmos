"""MCP 客户端测试：配置解析、连接生命周期、工具映射与结果归一。

约定：
- 多数用例注入假会话（只实现 list_tools / call_tool），确定性高、不依赖子进程。
- 另有一条用 mcp SDK 的内存流跑真实协议（initialize / list_tools / call_tool）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from mcp import ClientSession, types
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams

from core.agent import (
    MCPConfig,
    MCPConfigError,
    MCPConnectionError,
    MCPManager,
    MCPServerSettings,
    MCPTimeoutError,
    ToolRegistry,
)
from core.agent.mcp import Connector, _render_result  # pyright: ignore[reportPrivateUsage]
from core.llm import ToolCall


class _FakeSession:
    """只覆盖 manager 用到的那两个方法。"""

    def __init__(
        self,
        tools: list[types.Tool] | None = None,
        result: types.CallToolResult | None = None,
        error: BaseException | None = None,
        call_error: BaseException | None = None,
    ) -> None:
        self.tools: list[types.Tool] = tools or []
        self.result: types.CallToolResult | None = result
        self.error: BaseException | None = error
        self.call_error: BaseException | None = call_error
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def list_tools(self) -> types.ListToolsResult:
        if self.error is not None:
            raise self.error
        return types.ListToolsResult(tools=self.tools)

    async def call_tool(
        self, name: str, arguments: dict[str, object] | None = None
    ) -> types.CallToolResult:
        self.calls.append((name, arguments))
        if self.call_error is not None:
            raise self.call_error
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _connector(sessions: dict[str, _FakeSession], exited: list[str] | None = None) -> Connector:
    @asynccontextmanager
    async def connect(server: MCPServerSettings) -> AsyncGenerator[ClientSession]:
        try:
            yield cast("ClientSession", cast("object", sessions[server.name]))
        finally:
            if exited is not None:
                exited.append(server.name)

    return connect


def _spec(name: str = "echo", description: str | None = "回显") -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )


def _result(
    content: list[types.ContentBlock],
    *,
    structured: dict[str, object] | None = None,
    is_error: bool = False,
) -> types.CallToolResult:
    return types.CallToolResult(content=content, structured_content=structured, is_error=is_error)


def _config(*servers: MCPServerSettings) -> MCPConfig:
    """按文件里的 mcpServers 结构构造，顺带覆盖键名展开。"""
    return MCPConfig.model_validate(
        {"mcpServers": {server.name: server.model_dump(by_alias=True) for server in servers}}
    )


def _server(name: str = "demo", timeout: float = 5.0) -> MCPServerSettings:
    return MCPServerSettings(name=name, command="unused", timeout=timeout)


async def test_启动后映射工具并清洗名称() -> None:
    session = _FakeSession(tools=[_spec()], result=_result([types.TextContent(text="嗨")]))
    manager = MCPManager(
        _config(MCPServerSettings(name="my srv", command="unused")),
        connector=_connector({"my srv": session}),
    )

    await manager.start()

    assert [item.name for item in manager.tools] == ["my_srv__echo"]
    assert manager.tools[0].description == "回显"
    assert manager.tools[0].parameters == _spec().input_schema

    await manager.stop()
    assert manager.tools == ()


async def test_通过注册表执行并传参() -> None:
    session = _FakeSession(tools=[_spec()], result=_result([types.TextContent(text="嗨")]))
    manager = MCPManager(_config(_server()), connector=_connector({"demo": session}))
    registry = ToolRegistry()

    await manager.start()
    registry.register(*manager.tools)
    out = await registry.execute(ToolCall(id="1", name="demo__echo", arguments='{"text": "世界"}'))
    await manager.stop()

    assert out == "嗨"
    assert session.calls == [("echo", {"text": "世界"})]


async def test_描述为空时回退到工具名() -> None:
    session = _FakeSession(tools=[_spec(description=None)], result=_result([]))
    manager = MCPManager(_config(_server()), connector=_connector({"demo": session}))

    await manager.start()

    assert manager.tools[0].description == "echo"

    await manager.stop()


def test_结果归一() -> None:
    two_lines = _result([types.TextContent(text="第一行"), types.TextContent(text="第二行")])
    assert _render_result(two_lines) == "第一行\n第二行"
    assert _render_result(_result([types.ImageContent(data="x", mime_type="image/png")])) == (
        "[图片 image/png]"
    )
    assert (
        _render_result(
            _result(
                [
                    types.EmbeddedResource(
                        resource=types.TextResourceContents(uri="file:///a.txt", text="资源正文")
                    )
                ]
            )
        )
        == "资源正文"
    )
    assert (
        _render_result(
            _result(
                [
                    types.EmbeddedResource(
                        resource=types.BlobResourceContents(uri="file:///a.bin", blob="x")
                    )
                ]
            )
        )
        == "[资源 file:///a.bin]"
    )
    assert _render_result(_result([types.AudioContent(data="x", mime_type="audio/wav")])) == (
        "[audio]"
    )
    assert _render_result(_result([], structured={"结果": 1})) == '{"结果": 1}'
    assert _render_result(_result([types.TextContent(text="")])) == "ok"
    assert _render_result(_result([types.TextContent(text="坏了")], is_error=True)) == (
        "工具执行失败：坏了"
    )


async def test_arguments非法时回填() -> None:
    session = _FakeSession(tools=[_spec()], result=_result([]))
    manager = MCPManager(_config(_server()), connector=_connector({"demo": session}))

    await manager.start()
    item = manager.tools[0]

    assert await item.invoke("不是 JSON") == (
        "参数校验失败：arguments 不是合法 JSON（Expecting value）"
    )
    assert await item.invoke("[1]") == "参数校验失败：arguments 必须是 JSON 对象"
    assert await item.invoke("  ") == "ok"
    await manager.stop()


async def test_调用异常由注册表回填() -> None:
    session = _FakeSession(tools=[_spec()], call_error=RuntimeError("断开"))
    manager = MCPManager(_config(_server()), connector=_connector({"demo": session}))
    registry = ToolRegistry()

    await manager.start()
    registry.register(*manager.tools)
    out = await registry.execute(ToolCall(id="1", name="demo__echo", arguments="{}"))
    await manager.stop()

    assert out == "工具执行失败：[RuntimeError] 断开"


async def test_建连失败时关闭已连的server() -> None:
    exited: list[str] = []
    session = _FakeSession(result=_result([]))

    @asynccontextmanager
    async def connect(server: MCPServerSettings) -> AsyncGenerator[ClientSession]:
        if server.name == "bad":
            raise RuntimeError("连不上")
        try:
            yield cast("ClientSession", cast("object", session))
        finally:
            exited.append(server.name)

    manager = MCPManager(_config(_server("ok"), _server("bad")), connector=connect)

    with pytest.raises(MCPConnectionError, match="bad"):
        await manager.start()

    assert exited == ["ok"]
    assert manager.tools == ()


async def test_建连失败_列出工具失败也算连不上() -> None:
    session = _FakeSession(error=RuntimeError("列表坏了"))
    manager = MCPManager(_config(_server()), connector=_connector({"demo": session}))

    with pytest.raises(MCPConnectionError, match="列表坏了"):
        await manager.start()


async def test_建连超时() -> None:
    @asynccontextmanager
    async def slow(_server: MCPServerSettings) -> AsyncGenerator[ClientSession]:
        await asyncio.sleep(10)
        yield cast("ClientSession", cast("object", _FakeSession()))

    manager = MCPManager(_config(_server(timeout=0.05)), connector=slow)

    with pytest.raises(MCPTimeoutError, match="超时"):
        await manager.start()


async def test_启动与停止幂等() -> None:
    started: list[str] = []
    session = _FakeSession(result=_result([]))

    @asynccontextmanager
    async def connect(server: MCPServerSettings) -> AsyncGenerator[ClientSession]:
        started.append(server.name)
        yield cast("ClientSession", cast("object", session))

    manager = MCPManager(_config(_server()), connector=connect)
    await manager.start()
    await manager.start()
    assert started == ["demo"]

    await manager.stop()
    await manager.stop()
    assert manager.tools == ()


async def test_未配置server时直接跳过() -> None:
    manager = MCPManager(MCPConfig())

    await manager.start()

    assert manager.tools == ()

    await manager.stop()


async def test_工具名超长截断() -> None:
    session = _FakeSession(tools=[_spec(name="x" * 20)], result=_result([]))
    manager = MCPManager(_config(_server(name="s" * 60)), connector=_connector({"s" * 60: session}))

    await manager.start()

    assert len(manager.tools[0].name) == 64
    await manager.stop()


def test_配置文件不存在按未配置处理(tmp_path: Path) -> None:
    config = MCPConfig.load(tmp_path / "mcp.json", tmp_path / ".env")

    assert config.servers == []


async def test_从文件构造manager(tmp_path: Path) -> None:
    manager = MCPManager.from_file(tmp_path / "mcp.json", tmp_path / ".env")

    await manager.start()

    assert manager.tools == ()

    await manager.stop()


def test_配置文件非法json报错(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    _ = path.write_text("{不是 json", encoding="utf-8")

    with pytest.raises(MCPConfigError, match="读取失败"):
        _ = MCPConfig.load(path, tmp_path / ".env")


def test_配置文件顶层必须是对象(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    _ = path.write_text("[]", encoding="utf-8")

    with pytest.raises(MCPConfigError, match="顶层必须是对象"):
        _ = MCPConfig.load(path, tmp_path / ".env")


def test_配置文件占位符展开(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TOKEN", "secret")
    path = tmp_path / "mcp.json"
    _ = path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "http://127.0.0.1:3000/mcp",
                        "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = MCPConfig.load(path, tmp_path / ".env")

    assert config.servers[0].headers == {"Authorization": "Bearer secret"}


def test_占位符缺值报错(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    _ = path.write_text(
        json.dumps({"mcpServers": {"x": {"command": "${MISSING_VAR}"}}}), encoding="utf-8"
    )

    with pytest.raises(MCPConfigError, match="占位符"):
        _ = MCPConfig.load(path, tmp_path / ".env")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"mcpServers": {"x": {"transport": "stdio"}}}, id="stdio缺command"),
        pytest.param({"mcpServers": {"x": {"transport": "http"}}}, id="http缺url"),
        pytest.param({"mcpServers": {"x": "不是对象"}}, id="配置不是对象"),
        pytest.param({"mcpServers": ["不是映射"]}, id="mcpServers不是映射"),
    ],
)
def test_配置字段不合法报错(tmp_path: Path, body: dict[str, object]) -> None:
    path = tmp_path / "mcp.json"
    _ = path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(MCPConfigError, match="不合法"):
        _ = MCPConfig.load(path, tmp_path / ".env")


def test_键名优先且忽略未知字段(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    _ = path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "real": {"name": "fake", "command": "uvx", "unknown": 1, "args": ["a"]}
                }
            }
        ),
        encoding="utf-8",
    )

    config = MCPConfig.load(path, tmp_path / ".env")

    assert config.servers[0].name == "real"
    assert config.servers[0].args == ["a"]


async def test_内存协议全链路() -> None:
    """用 mcp SDK 的内存流跑一遍真实协议。"""
    app = MCPServer("测试服务")

    @app.tool()
    def echo(text: str) -> str:
        """回显文本。

        Args:
            text: 要回显的内容。
        """
        return text

    low = app._lowlevel_server  # pyright: ignore[reportPrivateUsage]

    @asynccontextmanager
    async def connect(_server: MCPServerSettings) -> AsyncGenerator[ClientSession]:
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with ClientSession(*client_streams) as session:
                task = asyncio.create_task(
                    low.run(*server_streams, low.create_initialization_options())
                )
                _ = await session.initialize()
                try:
                    yield session
                finally:
                    _ = task.cancel()
                    _ = await asyncio.gather(task, return_exceptions=True)

    manager = MCPManager(_config(_server("mem")), connector=connect)
    registry = ToolRegistry()

    async with manager:
        registry.register(*manager.tools)
        out = await registry.execute(ToolCall(id="1", name="mem__echo", arguments='{"text": "嗨"}'))

    assert out == "嗨"
