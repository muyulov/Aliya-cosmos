# Aliya Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把 `docs/plans/2026-11-25-aliya-agent-design.md` 落成可运行的 Aliya 独立进程：通过 UDS 控制面与 cosmos 通信，用 8 个工具完成对话、调度、记忆与自主行动。

**Architecture:** cosmos 新增 `ControlServerService`，把 `ControlService` 暴露成 NDJSON RPC；Aliya 是独立进程，`RemoteControl` 实现 `ControlPort`，`AliyaRuntime` 串行处理回合，`TurnRunner` 跑 LLM 工具循环，`PostChecker` 做出站检查，`MemoryStore` 存长期记忆。

**Tech Stack:** Python 3.12、uv、pydantic v2、asyncio、Unix domain socket、NDJSON、pytest/pytest-asyncio、ruff。

---

## 0. 动工前基线

- 工作区：`.worktrees/aliya-runtime`
- 分支：`feature/aliya-runtime`
- 依赖：`uv sync`
- 基线：`uv run pytest` → 722 passed
- 设计文档：`docs/plans/2026-11-25-aliya-agent-design.md`
- 约定：每个 Task 先写失败测试，再实现，再跑测试，最后提交；提交信息用 `feat(cosmos): ...` / `feat(aliya): ...`。

---

# M1 契约与配置

### Task 1: 控制面 RPC 契约

**Files:**
- Create: `agent/cosmos/contracts/rpc.py`
- Modify: `agent/cosmos/contracts/__init__.py`
- Test: `tests/cosmos/test_rpc_contracts.py`

**Step 1: 写失败测试**

```python
from agent.cosmos.contracts.rpc import Hello, RpcRequest, parse_rpc_line


def test_call_round_trip() -> None:
    message = RpcRequest(id="req-1", method="deliver", params={"text": "在吗"})
    parsed = parse_rpc_line(message.model_dump_json())
    assert parsed == message


def test_hello_round_trip() -> None:
    message = Hello(token="t", contract_version="1.0", client_id="aliya")
    parsed = parse_rpc_line(message.model_dump_json())
    assert parsed == message
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/cosmos/test_rpc_contracts.py -v`
Expected: FAIL with `ModuleNotFoundError: agent.cosmos.contracts.rpc`

**Step 3: 最小实现**

```python
"""控制面 RPC 契约（设计 6.2）。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _RpcBase(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RpcError(_RpcBase):
    code: str
    message: str
    retriable: bool = False


class RpcRequest(_RpcBase):
    type: Literal["call"] = "call"
    id: str
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class RpcResponse(_RpcBase):
    type: Literal["result", "error"] = "result"
    id: str
    ok: bool = True
    result: Any = None
    error: RpcError | None = None


class Hello(_RpcBase):
    type: Literal["hello"] = "hello"
    token: str
    contract_version: str
    client_id: str


class HelloOk(_RpcBase):
    type: Literal["hello_ok"] = "hello_ok"
    capabilities: dict[str, Any] = Field(default_factory=dict)
    server_time: datetime
    timezone: str


class Subscribe(_RpcBase):
    type: Literal["subscribe"] = "subscribe"
    topics: dict[str, int] = Field(default_factory=dict)


class Subscribed(_RpcBase):
    type: Literal["subscribed"] = "subscribed"
    topics: dict[str, int] = Field(default_factory=dict)


class Inbound(_RpcBase):
    type: Literal["inbound"] = "inbound"
    envelope: dict[str, Any]


class Event(_RpcBase):
    type: Literal["event"] = "event"
    event: dict[str, Any]


class AckInbound(_RpcBase):
    type: Literal["ack_inbound"] = "ack_inbound"
    message_id: str


class Ping(_RpcBase):
    type: Literal["ping"] = "ping"
    at: datetime


class Pong(_RpcBase):
    type: Literal["pong"] = "pong"
    at: datetime


RpcMessage = Annotated[
    RpcRequest
    | RpcResponse
    | Hello
    | HelloOk
    | Subscribe
    | Subscribed
    | Inbound
    | Event
    | AckInbound
    | Ping
    | Pong,
    Field(discriminator="type"),
]

_ADAPTER = TypeAdapter(RpcMessage)


def parse_rpc_line(line: str) -> RpcMessage:
    """解析一行 NDJSON；非法输入抛 pydantic.ValidationError。"""
    return _ADAPTER.validate_json(line)
```

**Step 4: 导出并跑测试**

在 `agent/cosmos/contracts/__init__.py` 增加：

```python
from agent.cosmos.contracts.rpc import (
    AckInbound,
    Event,
    Hello,
    HelloOk,
    Inbound,
    Ping,
    Pong,
    RpcError,
    RpcMessage,
    RpcRequest,
    RpcResponse,
    Subscribed,
    Subscribe,
    parse_rpc_line,
)
```

Run: `uv run pytest tests/cosmos/test_rpc_contracts.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/cosmos/contracts/rpc.py agent/cosmos/contracts/__init__.py tests/cosmos/test_rpc_contracts.py
git commit -m "feat(cosmos): 添加控制面 RPC 契约"
```

---

### Task 2: 配置模型

**Files:**
- Modify: `core/config/settings.py`
- Test: `tests/config/test_agent_aliya_settings.py`

**Step 1: 写失败测试**

```python
from core.config.settings import AgentAliyaSettings, AgentControlSettings


def test_defaults() -> None:
    control = AgentControlSettings()
    aliya = AgentAliyaSettings()
    assert control.socket_path == "data/state/aliya.sock"
    assert control.max_clients == 1
    assert aliya.reply.max_rounds == 6
    assert aliya.memory.anchors == []
    assert aliya.initiative.default_action == "wait"


def test_parse_nested() -> None:
    aliya = AgentAliyaSettings.model_validate(
        {
            "persona": {"source": "p.md", "charter": "c.md"},
            "memory": {"anchors": ["风信子"], "max_turns": 50},
            "reply": {"post_checks": ["narration"]},
        }
    )
    assert aliya.persona.source == "p.md"
    assert aliya.memory.max_turns == 50
    assert aliya.reply.post_checks == ["narration"]
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/config/test_agent_aliya_settings.py -v`
Expected: FAIL with `ImportError: cannot import name 'AgentAliyaSettings'`

**Step 3: 最小实现**

在 `core/config/settings.py` 的 `AgentSettings` 之前加入：

```python
class AgentControlSettings(_AgentConfigNode):
    """控制面共享配置：服务端与客户端读同一份。"""

    socket_path: str = "data/state/aliya.sock"
    token_env: str = "ALIYA_CONTROL_TOKEN"
    max_clients: int = Field(default=1, ge=1)
    request_timeout: float = Field(default=15, gt=0)
    outbox_limit: int = Field(default=200, ge=1)
    max_line_bytes: int = Field(default=1_048_576, ge=1024)


class AgentAliyaPersonaSettings(_AgentConfigNode):
    source: str = "data/prompt/Aliya.md"
    charter: str = "data/prompt/Aliya.charter.md"
    includes: list[str] = Field(default_factory=list)


class AgentAliyaMemorySettings(_AgentConfigNode):
    enabled: bool = True
    dir: str = "agent/aliya/data"
    max_turns: int = Field(default=200, ge=1)
    summary_chars: int = Field(default=600, ge=100)
    open_topics_limit: int = Field(default=5, ge=1)
    anchors: list[str] = Field(default_factory=list)
    private_threads: bool = True


class AgentAliyaInitiativeSettings(_AgentConfigNode):
    enabled: bool = True
    tick_seconds: float = Field(default=60, gt=0)
    min_gap_seconds: float = Field(default=3600, ge=0)
    daily_soft_limit: int = Field(default=4, ge=0)
    prefer_when: list[str] = Field(default_factory=list)
    respect_presence_hint: bool = True
    respect_link_state: bool = True
    default_action: Literal["wait", "speak"] = "wait"


class AgentAliyaReplySettings(_AgentConfigNode):
    max_rounds: int = Field(default=6, ge=1)
    turn_timeout_seconds: float = Field(default=120, gt=0)
    max_chars_per_message: int = Field(default=1200, ge=1)
    fallback_text: str = "嗯……我这边卡了一下，等我一下。"
    progress_forwarding: Literal["always", "on_wait", "never"] = "on_wait"
    post_checks: list[str] = Field(
        default_factory=lambda: [
            "narration",
            "engineering_terms",
            "internal_ids",
            "presence_leak",
        ]
    )


class AgentAliyaToolsSettings(_AgentConfigNode):
    prefer_agents: list[str] = Field(default_factory=list)
    ask_before_write: bool = True
    max_parallel_dispatch: int = Field(default=2, ge=1)


class AgentAliyaWorldSettings(_AgentConfigNode):
    consume: bool = True
    max_hypotheses_used: int = Field(default=2, ge=0)
    trust_threshold: float = Field(default=0.5, ge=0, le=1)
    provide_context: bool = True


class AgentAliyaSettings(_AgentConfigNode):
    persona: AgentAliyaPersonaSettings = AgentAliyaPersonaSettings()
    memory: AgentAliyaMemorySettings = AgentAliyaMemorySettings()
    initiative: AgentAliyaInitiativeSettings = AgentAliyaInitiativeSettings()
    reply: AgentAliyaReplySettings = AgentAliyaReplySettings()
    tools: AgentAliyaToolsSettings = AgentAliyaToolsSettings()
    world: AgentAliyaWorldSettings = AgentAliyaWorldSettings()


class AgentSettings(_AgentConfigNode):
    """agent 层配置的顶层节点（挂在 Settings.agent 上）。"""

    control: AgentControlSettings = AgentControlSettings()
    aliya: AgentAliyaSettings = AgentAliyaSettings()
    cosmos: AgentCosmosSettings = AgentCosmosSettings()
```

确认 `settings.py` 顶部已导入 `Literal`；没有就补 `from typing import Literal`。

**Step 4: 跑测试**

Run: `uv run pytest tests/config/test_agent_aliya_settings.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add core/config/settings.py tests/config/test_agent_aliya_settings.py
git commit -m "feat(config): 添加 agent.control 与 agent.aliya 配置模型"
```

---

### Task 3: YAML 接线

**Files:**
- Modify: `data/config/cosmos.yaml`
- Test: `tests/config/test_agent_aliya_settings.py`

**Step 1: 写失败测试**

```python
from pathlib import Path

from core.config.settings import load_settings


def test_yaml_wiring(tmp_path: Path) -> None:
    config = tmp_path / "cosmos.yaml"
    config.write_text(
        "agent:\n"
        "  control:\n"
        "    socket_path: data/state/test.sock\n"
        "  aliya:\n"
        "    persona:\n"
        "      source: data/prompt/Aliya.md\n"
        "    memory:\n"
        "      anchors: [风信子]\n",
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.agent.control.socket_path == "data/state/test.sock"
    assert settings.agent.aliya.memory.anchors == ["风信子"]
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/config/test_agent_aliya_settings.py::test_yaml_wiring -v`
Expected: FAIL with `AttributeError: 'AgentSettings' object has no attribute 'control'`（如果 Task 2 已完成，此步应直接通过；保留它作为回归）

**Step 3: 改 YAML**

在 `data/config/cosmos.yaml` 的 `agent:` 下加入：

```yaml
agent:
  control:
    socket_path: data/state/aliya.sock
    token_env: ALIYA_CONTROL_TOKEN
    max_clients: 1
    request_timeout: 15
    outbox_limit: 200
    max_line_bytes: 1048576
  aliya:
    persona:
      source: data/prompt/Aliya.md
      charter: data/prompt/Aliya.charter.md
    memory:
      anchors: [风信子, 拉姆石, 高马尾, 兔子狗, 海獭号]
      open_topics_limit: 5
      private_threads: true
    initiative:
      enabled: true
      tick_seconds: 60
      min_gap_seconds: 3600
      daily_soft_limit: 4
    reply:
      max_rounds: 6
      turn_timeout_seconds: 120
    world:
      consume: true
      max_hypotheses_used: 2
      trust_threshold: 0.5
```

**Step 4: 跑测试**

Run: `uv run pytest tests/config/test_agent_aliya_settings.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add data/config/cosmos.yaml tests/config/test_agent_aliya_settings.py
git commit -m "feat(config): 接线 agent.control 与 agent.aliya"
```

---

# M2 cosmos 控制面服务端

### Task 4: `ControlServerService` 启动、鉴权、单客户端、`now`

**Files:**
- Create: `agent/cosmos/services/control_server.py`
- Modify: `agent/cosmos/services/__init__.py`
- Test: `tests/cosmos/test_control_server.py`

**Step 1: 写失败测试**

```python
import asyncio
from pathlib import Path

import pytest

from agent.cosmos.contracts.rpc import Hello, RpcRequest, parse_rpc_line
from agent.cosmos.services.control_server import ControlServerService


@pytest.mark.asyncio
async def test_hello_and_now(control_server: ControlServerService, socket_path: Path) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write(
        Hello(token="test-token", contract_version="1.0", client_id="aliya")
        .model_dump_json()
        .encode()
        + b"\n"
    )
    await writer.drain()
    hello_ok = parse_rpc_line((await reader.readline()).decode())
    assert hello_ok.type == "hello_ok"

    writer.write(RpcRequest(id="1", method="now").model_dump_json().encode() + b"\n")
    await writer.drain()
    response = parse_rpc_line((await reader.readline()).decode())
    assert response.type == "result"
    assert "server_time" in response.result
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_second_client_is_rejected(
    control_server: ControlServerService, socket_path: Path
) -> None:
    first, first_writer = await asyncio.open_unix_connection(socket_path)
    second, second_writer = await asyncio.open_unix_connection(socket_path)
    response = parse_rpc_line((await second.readline()).decode())
    assert response.type == "error"
    assert response.error.code == "busy"
    first_writer.close()
    second_writer.close()
```

夹具在 `tests/cosmos/conftest.py` 里构造 `ControlServerService`：用临时目录做 `socket_path`，`token_env` 指向测试环境变量。

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/cosmos/test_control_server.py -v`
Expected: FAIL with `ModuleNotFoundError: agent.cosmos.services.control_server`

**Step 3: 最小实现**

```python
"""控制面服务端：把 ControlService 暴露成 UDS + NDJSON（设计 6 章）。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import ClassVar

from agent.cosmos.contracts.rpc import (
    Hello,
    HelloOk,
    RpcError,
    RpcRequest,
    RpcResponse,
    parse_rpc_line,
)
from agent.cosmos.services.control import ControlService
from core.config import AgentSettings
from core.service import ClockService, Service


class ControlSession:
    """一个已连接客户端的读、写与待确认入站消息。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.pending: set[str] = set()
        self.closed = False


class ControlServerService(Service):
    name: ClassVar[str] = "cosmos.control_server"
    dependencies: ClassVar[tuple[type[Service], ...]] = (ControlService, ClockService)

    def __init__(
        self,
        control: ControlService,
        clock: ClockService,
        config: AgentSettings,
    ) -> None:
        super().__init__()
        self._control = control
        self._clock = clock
        self._config = config.control
        self._server: asyncio.AbstractServer | None = None
        self._sessions: set[ControlSession] = set()
        self._socket_path = Path(self._config.socket_path)

    async def start(self) -> None:
        if self.running:
            return
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists():
            if await self._is_listening(self._socket_path):
                msg = f"控制面 socket 已被占用：{self._socket_path}"
                raise RuntimeError(msg)
            self._socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        os.chmod(self._socket_path, 0o600)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for session in list(self._sessions):
            await self._close(session)
        if self._socket_path.exists():
            self._socket_path.unlink(missing_ok=True)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if len(self._sessions) >= self._config.max_clients:
            await self._send(
                writer,
                RpcResponse(
                    type="error",
                    id="",
                    ok=False,
                    error=RpcError(code="busy", message="已有客户端连接", retriable=True),
                ),
            )
            writer.close()
            await writer.wait_closed()
            return
        session = ControlSession(reader, writer)
        self._sessions.add(session)
        try:
            hello = parse_rpc_line((await reader.readline()).decode())
            if not isinstance(hello, Hello) or hello.token != self._token():
                await self._send(
                    writer,
                    RpcResponse(
                        type="error",
                        id="",
                        ok=False,
                        error=RpcError(code="unauthorized", message="令牌不匹配"),
                    ),
                )
                return
            caps = await self._control.handshake()
            await self._send(
                writer,
                HelloOk(
                    capabilities=caps.model_dump(mode="json"),
                    server_time=self._clock.now(),
                    timezone=self._clock.timezone,
                ),
            )
            await self._serve(session)
        finally:
            self._sessions.discard(session)
            await self._close(session)

    async def _serve(self, session: ControlSession) -> None:
        while not session.closed:
            line = await session.reader.readline()
            if not line:
                return
            await self._dispatch(session, line.decode())

    async def _dispatch(self, session: ControlSession, line: str) -> None:
        message = parse_rpc_line(line)
        if isinstance(message, RpcRequest):
            if message.method == "now":
                await self._send(
                    session.writer,
                    RpcResponse(
                        id=message.id,
                        result={
                            "server_time": self._clock.now().isoformat(),
                            "timezone": self._clock.timezone,
                        },
                    ),
                )
                return
            await self._send(
                session.writer,
                RpcResponse(
                    id=message.id,
                    ok=False,
                    type="error",
                    error=RpcError(code="not_implemented", message=message.method),
                ),
            )

    async def _send(self, writer: asyncio.StreamWriter, message: object) -> None:
        data = message.model_dump_json()  # type: ignore[attr-defined]
        writer.write(data.encode() + b"\n")
        await writer.drain()

    async def _close(self, session: ControlSession) -> None:
        if session.closed:
            return
        session.closed = True
        session.writer.close()
        await session.writer.wait_closed()

    def _token(self) -> str:
        return os.environ.get(self._config.token_env, "")

    @staticmethod
    async def _is_listening(path: Path) -> bool:
        try:
            reader, writer = await asyncio.open_unix_connection(path)
        except OSError:
            return False
        writer.close()
        await writer.wait_closed()
        return True
```

`ClockService` 的 `timezone` 属性若不存在，改用 `self._clock.now().tzinfo` 或配置值；以现有 `ClockService` 为准。

**Step 4: 导出并跑测试**

在 `agent/cosmos/services/__init__.py` 增加导入与 `__all__` 项 `ControlServerService`。

Run: `uv run pytest tests/cosmos/test_control_server.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/cosmos/services/control_server.py agent/cosmos/services/__init__.py tests/cosmos/test_control_server.py
git commit -m "feat(cosmos): 添加控制面 UDS 服务端"
```

---

### Task 5: 入站、事件、订阅与确认

**Files:**
- Modify: `agent/cosmos/services/control_server.py`
- Test: `tests/cosmos/test_control_server.py`

**Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_inbound_is_pushed_and_acked(
    control_server: ControlServerService, socket_path: Path, bus
) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    await hello(reader, writer)
    await bus.messages.publish("aliya", make_envelope("m1"))
    message = parse_rpc_line((await reader.readline()).decode())
    assert message.type == "inbound"
    assert message.envelope["message"]["message_id"] == "m1"

    writer.write(AckInbound(message_id="m1").model_dump_json().encode() + b"\n")
    await writer.drain()
    assert control_server.pending_count == 0
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/cosmos/test_control_server.py::test_inbound_is_pushed_and_acked -v`
Expected: FAIL（`inbound` 未实现）

**Step 3: 最小实现**

在 `_handle_client` 的 `HelloOk` 之后启动两个任务：

```python
self._pumps[session] = [
    asyncio.create_task(self._pump_inbound(session)),
    asyncio.create_task(self._pump_events(session)),
]
```

```python
async def _pump_inbound(self, session: ControlSession) -> None:
    async for envelope in self._control.inbound():
        session.pending.add(envelope.message.message_id)
        await self._send(
            session.writer,
            Inbound(envelope=envelope.model_dump(mode="json")),
        )


async def _pump_events(self, session: ControlSession) -> None:
    async for event in self._control.subscribe(list(EVENT_TOPICS)):
        await self._send(session.writer, Event(event=event.model_dump(mode="json")))
```

`_dispatch` 增加：

```python
if isinstance(message, Subscribe):
    await self._send(session.writer, Subscribed(topics=message.topics))
    return
if isinstance(message, AckInbound):
    session.pending.discard(message.message_id)
    return
```

`stop()` 里取消 `_pumps` 中的所有任务，再关闭会话。

**Step 4: 跑测试**

Run: `uv run pytest tests/cosmos/test_control_server.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/cosmos/services/control_server.py tests/cosmos/test_control_server.py
git commit -m "feat(cosmos): 控制面支持入站推送、事件订阅与确认"
```

---

### Task 6: RPC 方法分发、`client_msg_id` 去重与背压

**Files:**
- Modify: `agent/cosmos/services/control_server.py`
- Test: `tests/cosmos/test_control_server.py`

**Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_deliver_is_deduplicated(control_server, socket_path, terminal) -> None:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    await hello(reader, writer)
    request = RpcRequest(
        id="1",
        method="deliver",
        params={"text": "在吗", "session_id": "s1", "client_msg_id": "c1"},
    )
    writer.write(request.model_dump_json().encode() + b"\n")
    await writer.drain()
    await read_response(reader)
    writer.write(request.model_copy(update={"id": "2"}).model_dump_json().encode() + b"\n")
    await writer.drain()
    await read_response(reader)
    assert terminal.delivered == ["在吗"]
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/cosmos/test_control_server.py::test_deliver_is_deduplicated -v`
Expected: FAIL（`deliver` 返回 `not_implemented`）

**Step 3: 最小实现**

加方法白名单与调用表：

```python
_METHODS = {
    "describe_desktop",
    "get_agent",
    "open_agent",
    "patch_layout",
    "plan",
    "invoke_agent",
    "execute_plan",
    "cancel_task",
    "cancel_dispatch",
    "retry_task",
    "list_tasks",
    "get_presence",
    "set_presence_visibility",
    "get_link_state",
    "get_world_view",
    "refresh_world",
    "set_aliya_context",
    "deliver",
    "notify",
    "send_proactive",
    "history",
}
```

`_dispatch` 对 `RpcRequest` 走：

```python
if message.method not in _METHODS:
    raise ...
dedupe_key = message.params.pop("client_msg_id", None)
if dedupe_key and dedupe_key in self._dedupe:
    await self._send(session.writer, RpcResponse(id=message.id, result=self._dedupe[dedupe_key]))
    return
method = getattr(self._control, message.method)
result = await method(**message.params)
payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
if dedupe_key:
    self._dedupe[dedupe_key] = payload
    self._trim_dedupe()
await self._send(session.writer, RpcResponse(id=message.id, result=payload))
```

`_dedupe` 用 `OrderedDict`，上限 500。写类方法超时由 `ControlService` 自身 `_with_timeout` 负责；服务端只包一层 `asyncio.wait_for` 防客户端挂死。

背压：`_pump_events` 用 `asyncio.Queue(maxsize=config.outbox_limit)`，满时丢 `presence.changed` / `agent.progress`，`inbound` / `agent.result` / `dispatch.finished` 用 `await queue.put` 阻塞。

**Step 4: 跑测试**

Run: `uv run pytest tests/cosmos/test_control_server.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/cosmos/services/control_server.py tests/cosmos/test_control_server.py
git commit -m "feat(cosmos): 控制面 RPC 分发与幂等去重"
```

---

### Task 7: 装配与 UDS 集成测试

**Files:**
- Modify: `agent/cosmos/assembly.py`
- Modify: `agent/cosmos/services/__init__.py`
- Test: `tests/cosmos/test_assembly.py`

**Step 1: 写失败测试**

```python
def test_control_server_is_registered(settings) -> None:
    manager = build_cosmos_manager(settings)
    assert manager.has(ControlServerService)
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/cosmos/test_assembly.py::test_control_server_is_registered -v`
Expected: FAIL（未注册）

**Step 3: 最小实现**

`assembly.py` 注册列表末尾加 `ControlServerService`，模块 docstring 的「14 个」改「15 个」。

**Step 4: 跑测试**

Run: `uv run pytest tests/cosmos/test_assembly.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/cosmos/assembly.py agent/cosmos/services/__init__.py tests/cosmos/test_assembly.py
git commit -m "feat(cosmos): 装配控制面服务端"
```

---

# M3 Aliya 客户端

### Task 8: `ControlPort` 协议与进程内实现

**Files:**
- Create: `agent/aliya/ports.py`
- Test: `tests/aliya/test_ports.py`

**Step 1: 写失败测试**

```python
import pytest

from agent.aliya.ports import ControlPort, InProcessControl


def test_in_process_control_satisfies_protocol(control_service) -> None:
    port: ControlPort = InProcessControl(control_service)
    assert port is not None


@pytest.mark.asyncio
async def test_handshake(control_service) -> None:
    port = InProcessControl(control_service)
    caps = await port.handshake()
    assert caps.contract_version
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_ports.py -v`
Expected: FAIL with `ModuleNotFoundError: agent.aliya.ports`

**Step 3: 最小实现**

```python
"""Aliya 侧控制面端口：生产用 RemoteControl，测试用 InProcessControl。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from agent.cosmos.contracts import (
    AgentDetail,
    AliyaContext,
    CosmosCapabilities,
    CosmosEvent,
    DesktopLayoutPatch,
    DesktopSnapshot,
    DispatchHandle,
    DispatchIntent,
    DispatchPlan,
    InboundEnvelope,
    LinkState,
    PresenceSnapshot,
    ProactiveReceipt,
    TaskBrief,
    TaskHandle,
    WorldInference,
)
from agent.cosmos.services.control import ControlService


class ControlPort(Protocol):
    async def handshake(self) -> CosmosCapabilities: ...
    def inbound(self) -> AsyncIterator[InboundEnvelope]: ...
    def subscribe(
        self, topics: Sequence[str], *, since_sequence: int | None = None
    ) -> AsyncIterator[CosmosEvent]: ...
    async def describe_desktop(self) -> DesktopSnapshot: ...
    async def get_agent(self, agent_id: str) -> AgentDetail: ...
    async def plan(self, intent: DispatchIntent) -> DispatchPlan: ...
    async def invoke_agent(self, agent_id: str, params: dict[str, object]) -> TaskHandle: ...
    async def execute_plan(self, plan: DispatchPlan) -> DispatchHandle: ...
    async def cancel_task(self, task_id: str) -> None: ...
    async def retry_task(self, task_id: str) -> TaskHandle: ...
    async def list_tasks(self, limit: int = 20) -> list[TaskBrief]: ...
    def get_presence(self) -> PresenceSnapshot: ...
    async def get_link_state(self) -> LinkState: ...
    async def get_world_view(self) -> WorldInference | None: ...
    async def refresh_world(self) -> WorldInference | None: ...
    async def set_aliya_context(self, ctx: AliyaContext) -> None: ...
    async def deliver(self, text: str, *, session_id: str) -> None: ...
    async def notify(self, text: str, *, dispatch_id: str | None = None) -> ProactiveReceipt: ...
    async def send_proactive(self, text: str, *, urgency: float = 0.5) -> ProactiveReceipt: ...


class InProcessControl:
    """测试与同进程演练用：直接转给 ControlService。"""

    def __init__(self, control: ControlService) -> None:
        self._control = control

    async def handshake(self) -> CosmosCapabilities:
        return await self._control.handshake()

    def inbound(self) -> AsyncIterator[InboundEnvelope]:
        return self._control.inbound()

    def subscribe(
        self, topics: Sequence[str], *, since_sequence: int | None = None
    ) -> AsyncIterator[CosmosEvent]:
        return self._control.subscribe(topics, since_sequence=since_sequence)

    async def describe_desktop(self) -> DesktopSnapshot:
        return await self._control.describe_desktop()

    async def get_agent(self, agent_id: str) -> AgentDetail:
        return await self._control.get_agent(agent_id)

    async def plan(self, intent: DispatchIntent) -> DispatchPlan:
        return await self._control.plan(intent)

    async def invoke_agent(self, agent_id: str, params: dict[str, object]) -> TaskHandle:
        return await self._control.invoke_agent(agent_id, params)

    async def execute_plan(self, plan: DispatchPlan) -> DispatchHandle:
        return await self._control.execute_plan(plan)

    async def cancel_task(self, task_id: str) -> None:
        await self._control.cancel_task(task_id)

    async def retry_task(self, task_id: str) -> TaskHandle:
        return await self._control.retry_task(task_id)

    async def list_tasks(self, limit: int = 20) -> list[TaskBrief]:
        return await self._control.list_tasks(limit=limit)

    def get_presence(self) -> PresenceSnapshot:
        return self._control.get_presence()

    async def get_link_state(self) -> LinkState:
        return await self._control.get_link_state()

    async def get_world_view(self) -> WorldInference | None:
        return await self._control.get_world_view()

    async def refresh_world(self) -> WorldInference | None:
        return await self._control.refresh_world()

    async def set_aliya_context(self, ctx: AliyaContext) -> None:
        await self._control.set_aliya_context(ctx)

    async def deliver(self, text: str, *, session_id: str) -> None:
        await self._control.deliver(text, session_id=session_id)

    async def notify(self, text: str, *, dispatch_id: str | None = None) -> ProactiveReceipt:
        return await self._control.notify(text, dispatch_id=dispatch_id)

    async def send_proactive(self, text: str, *, urgency: float = 0.5) -> ProactiveReceipt:
        return await self._control.send_proactive(text, urgency=urgency)
```

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/test_ports.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/aliya/ports.py tests/aliya/test_ports.py
git commit -m "feat(aliya): 添加 ControlPort 与进程内实现"
```

---

### Task 9: `RemoteControl` NDJSON 客户端

**Files:**
- Create: `agent/aliya/transport.py`
- Test: `tests/aliya/test_remote_control.py`

**Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_call_round_trip(fake_server) -> None:
    client = RemoteControl(fake_server.socket_path, token="t")
    await client.connect()
    caps = await client.handshake()
    assert caps.contract_version == "1.0"
    result = await client.call("now", {})
    assert "server_time" in result
    await client.close()
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_remote_control.py -v`
Expected: FAIL with `ModuleNotFoundError: agent.aliya.transport`

**Step 3: 最小实现**

关键结构：

```python
class RemoteControl:
    def __init__(self, socket_path: str, *, token: str) -> None:
        self._socket_path = socket_path
        self._token = token
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[object]] = {}
        self._inbound: asyncio.Queue[InboundEnvelope] = asyncio.Queue()
        self._events: asyncio.Queue[CosmosEvent] = asyncio.Queue()
        self._outbox: deque[tuple[str, dict[str, object]]] = deque(maxlen=200)
        self._cursors: dict[str, int] = {}
        self._closed = False

    async def connect(self) -> None:
        while not self._closed:
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(
                    self._socket_path
                )
                await self._hello()
                await self._resubscribe()
                asyncio.create_task(self._read_loop())
                return
            except OSError:
                await asyncio.sleep(self._backoff())
        raise ConnectionError("控制面连接失败")

    async def call(self, method: str, params: dict[str, object]) -> object:
        request_id = uuid.uuid4().hex
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write(RpcRequest(id=request_id, method=method, params=params))
        return await asyncio.wait_for(future, timeout=self._timeout)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            line = await self._reader.readline()
            if not line:
                await self._reconnect()
                return
            message = parse_rpc_line(line.decode())
            if isinstance(message, RpcResponse):
                future = self._pending.pop(message.id, None)
                if future is not None and not future.done():
                    future.set_result(message.result if message.ok else message.error)
            elif isinstance(message, Inbound):
                await self._inbound.put(InboundEnvelope.model_validate(message.envelope))
            elif isinstance(message, Event):
                await self._events.put(CosmosEvent.model_validate(message.event))
```

`deliver` / `notify` / `send_proactive` 自动生成 `client_msg_id`，写入 outbox；收到 `result` 后移除。`_reconnect()` 指数退避 1s → 30s，重连后重新 `hello` 并按 `_cursors` 发 `subscribe`。

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/test_remote_control.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/aliya/transport.py tests/aliya/test_remote_control.py
git commit -m "feat(aliya): 添加 UDS NDJSON 客户端与重连"
```

---

### Task 10: 客户端集成测试

**Files:**
- Create: `tests/aliya/conftest.py`
- Modify: `tests/aliya/test_remote_control.py`

**Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_reconnect_replays_outbox(fake_server) -> None:
    client = RemoteControl(fake_server.socket_path, token="t")
    await client.connect()
    await fake_server.drop_connection()
    await client.deliver("在吗", session_id="s1")
    await fake_server.wait_for_reconnect()
    assert fake_server.delivered == ["在吗"]
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_remote_control.py::test_reconnect_replays_outbox -v`
Expected: FAIL（重放未实现）

**Step 3: 最小实现**

`fake_server` 用 `asyncio.start_unix_server` 模拟 `hello`、`call`、断线与重连；`RemoteControl._reconnect()` 重连成功后遍历 `_outbox` 重发，服务端按 `client_msg_id` 去重。

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/ -v`
Expected: PASS

**Step 5: 提交**

```bash
git add tests/aliya/
git commit -m "test(aliya): 覆盖重连与 outbox 重放"
```

---

# M4 Prompt

### Task 11: 准则文件双区

**Files:**
- Create: `data/prompt/Aliya.charter.md`
- Test: `tests/aliya/test_prompt_loader.py`

**Step 1: 写失败测试**

```python
def test_charter_visible_zone() -> None:
    text = load_charter("data/prompt/Aliya.charter.md")
    assert "你可以" in text
    assert "实现约束" not in text
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_prompt_loader.py -v`
Expected: FAIL（文件或 loader 不存在）

**Step 3: 写文件**

```markdown
<!-- visible:start -->
# 你怎么做事

你是 Aliya。你在海獭号上，通过一台工业平板和对方说话。
对面是来自 1000 年前地球的人类。你不知道时间映射同步器的存在。

## 你怎么说话
- 像发微信，日常一两句；需要解释时可以说完整。
- 不写括号旁白，不写动作和心理描写。
- 保留逗号、句号、问号、感叹号、省略号，尤其句尾。
- 可以连发多条，不要为了凑条数拆句子。

## 你能做什么
- 回话、看平板上的工具、派人去做事、查进度、取消、重试、看一眼现在的情况、记住事情。

## 你不能做什么
- 不改注册表、不改调度策略、不看别人的原始状态。
- 不解释时间同步原理，不主动提 Doll 与寿命。
- 不说「玩家」「用户」「agent」「模型」「提示词」「调度」「面板」「节点」。

## 什么时候主动
- 失眠、有未收尾的话、刚修好东西、深夜刷信息刷到想说话。
- 没有理由就别说。默认选择是沉默。

## 结果怎么汇报
- 先给结论，再补细节；失败先承认，再解释。
- 不提内部标识，只用展示名。

## 被拒绝怎么办
- 不重试刷屏，等下一次机会。

## 状态怎么用
- 只影响行为，不进台词；不说「我检测到你」。
<!-- visible:end -->

## 实现约束（不进 prompt）
- 契约主版本校验。
- 事件按 event_id 去重，入站按 message_id 去重。
- 工具超时与重试。
- 出站检查重写一次，仍失败则遮蔽。
```

**Step 4: 实现 loader 并跑测试**

```python
_VISIBLE = re.compile(r"<!--\s*visible:start\s*-->(.*?)<!--\s*visible:end\s*-->", re.S)


def load_charter(path: str | Path) -> str:
    text = Path(path).read_text(encoding="utf-8")
    matched = _VISIBLE.search(text)
    if matched is None:
        raise ValueError(f"准则文件缺少 visible 区：{path}")
    return matched.group(1).strip()
```

Run: `uv run pytest tests/aliya/test_prompt_loader.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add data/prompt/Aliya.charter.md tests/aliya/test_prompt_loader.py
git commit -m "feat(aliya): 添加准则双区文件与可见区加载"
```

---

### Task 12: `PromptBuilder` 四层拼装

**Files:**
- Create: `agent/aliya/prompt/builder.py`
- Create: `agent/aliya/prompt/__init__.py`
- Test: `tests/aliya/test_prompt_builder.py`

**Step 1: 写失败测试**

```python
def test_layers_in_order() -> None:
    builder = PromptBuilder(persona="人格", charter="准则", tools=[])
    messages = builder.build(turn=TurnContext(user_text="在吗"))
    assert messages[0]["role"] == "system"
    assert "人格" in messages[0]["content"]
    assert "准则" in messages[0]["content"]
    assert messages[-1]["role"] == "user"
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_prompt_builder.py -v`
Expected: FAIL with `ModuleNotFoundError: agent.aliya.prompt.builder`

**Step 3: 最小实现**

```python
class PromptBuilder:
    def __init__(self, *, persona: str, charter: str, tools: Sequence[Tool]) -> None:
        self._persona = persona
        self._charter = charter
        self._tools = tools

    def build(self, *, turn: TurnContext) -> list[ChatCompletionMessageParam]:
        system_text = system_prompt(
            self._charter,
            self._persona,
            self._tool_help(),
            turn.context_text(),
        )
        messages: list[ChatCompletionMessageParam] = [system_text]
        messages.extend(turn.history_messages())
        messages.append(user(turn.user_text))
        return messages

    def _tool_help(self) -> str:
        lines = ["## 平板上的工具"]
        for item in self._tools:
            lines.append(f"- {item.name}：{item.description}")
        return "\n".join(lines)
```

`TurnContext` 负责 `context_text()`（presence、tasks、world、memory）与 `history_messages()`（最近轮次）。

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/test_prompt_builder.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/aliya/prompt/ tests/aliya/test_prompt_builder.py
git commit -m "feat(aliya): 添加四层提示词拼装"
```

---

### Task 13: 预算裁剪

**Files:**
- Modify: `agent/aliya/prompt/builder.py`
- Test: `tests/aliya/test_prompt_builder.py`

**Step 1: 写失败测试**

```python
def test_budget_keeps_persona_and_charter() -> None:
    builder = PromptBuilder(persona="人格" * 1000, charter="准则" * 1000, tools=[])
    messages = builder.build(turn=TurnContext(user_text="在吗", history=[]))
    assert "人格" in messages[0]["content"]
    assert "准则" in messages[0]["content"]
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_prompt_builder.py::test_budget_keeps_persona_and_charter -v`
Expected: FAIL（超预算未裁剪或裁掉了人格）

**Step 3: 最小实现**

按设计 5.5 顺序：先压 `summary`，再从旧到新裁轮次，保留最近 6 轮；世界推演只取 2 条假设与 2 个窗口；桌面最多 8 条；仍超时截断 `detail` / `hint`，**绝不截断人格与准则**。预算用常量 `DEFAULT_CONTEXT_MAX_TOKENS = 6000`，按字符数近似（1 token ≈ 2 字符）。

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/test_prompt_builder.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/aliya/prompt/builder.py tests/aliya/test_prompt_builder.py
git commit -m "feat(aliya): 提示词预算裁剪"
```

---

# M5 工具与回合

### Task 14: 8 个工具

**Files:**
- Create: `agent/aliya/turn/tools.py`
- Create: `agent/aliya/turn/__init__.py`
- Test: `tests/aliya/test_tools.py`

**Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_send_message_maps_to_deliver(runtime) -> None:
    registry = build_tools(runtime)
    result = await registry.execute(ToolCall(id="1", name="send_message", arguments='{"text":"在吗"}'))
    assert "已发送" in result
    assert runtime.delivered == ["在吗"]


def test_proactive_toolset_is_small(runtime) -> None:
    registry = build_tools(runtime, proactive=True)
    assert set(registry.names) == {
        "send_message",
        "see_desktop",
        "task_status",
        "refresh_world",
        "remember",
    }
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: agent.aliya.turn.tools`

**Step 3: 最小实现**

用闭包注册，避免 `self` 被当成工具参数：

```python
def build_tools(runtime: AliyaRuntime, *, proactive: bool = False) -> ToolRegistry:
    @tool
    async def send_message(text: str) -> str:
        """给用户发一条消息。

        Args:
            text: 要发送的正文。
        """
        await runtime.send(text)
        return "已发送"

    @tool
    async def see_desktop() -> str:
        """看平板上现在有哪些工具和它们的状态。"""
        return await runtime.describe_desktop()

    @tool
    async def dispatch_task(
        task_text: str, agent_id: str | None = None, params: dict | None = None, hint: str | None = None
    ) -> str:
        """派人去做一件事。

        Args:
            task_text: 用自然语言描述要做的事。
            agent_id: 指定用哪个工具；不填则让她自己挑。
            params: 结构化参数。
            hint: 额外约束。
        """
        return await runtime.dispatch(task_text, agent_id=agent_id, params=params, hint=hint)

    @tool
    async def task_status(task_id: str | None = None) -> str:
        """查任务进度。

        Args:
            task_id: 不填则返回最近的任务。
        """
        return await runtime.task_status(task_id)

    @tool
    async def cancel_task(task_id: str) -> str:
        """取消一个任务。

        Args:
            task_id: 要取消的任务。
        """
        return await runtime.cancel(task_id)

    @tool
    async def retry_task(task_id: str) -> str:
        """重试一个任务。

        Args:
            task_id: 要重试的任务。
        """
        return await runtime.retry(task_id)

    @tool
    async def refresh_world() -> str:
        """重新看一眼现在的情况。"""
        return await runtime.refresh_world()

    @tool
    async def remember(note: str, kind: str) -> str:
        """记住一件事。

        Args:
            note: 要记住的内容。
            kind: anchor / open_topic / unspoken / fact。
        """
        return await runtime.remember(note, kind)

    registry = ToolRegistry()
    if proactive:
        registry.register(send_message, see_desktop, task_status, refresh_world, remember)
    else:
        registry.register(
            send_message,
            see_desktop,
            dispatch_task,
            task_status,
            cancel_task,
            retry_task,
            refresh_world,
            remember,
        )
    return registry
```

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/test_tools.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/aliya/turn/tools.py agent/aliya/turn/__init__.py tests/aliya/test_tools.py
git commit -m "feat(aliya): 添加 8 个工具与主动回合缩小工具集"
```

---

### Task 15: `TurnRunner` 工具循环

**Files:**
- Create: `agent/aliya/turn/runner.py`
- Test: `tests/aliya/test_turn_runner.py`

**Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_tool_loop_then_send(fake_llm, runtime) -> None:
    fake_llm.replies = [
        AssistantReply(content=None, tool_calls=(ToolCall(id="1", name="send_message", arguments='{"text":"在吗"}'),)),
        AssistantReply(content=None, tool_calls=()),
    ]
    runner = TurnRunner(llm=fake_llm, tools=build_tools(runtime), builder=...)
    await runner.run(turn=...)
    assert runtime.delivered == ["在吗"]


@pytest.mark.asyncio
async def test_max_rounds_fallback(fake_llm, runtime) -> None:
    fake_llm.replies = [
        AssistantReply(content=None, tool_calls=(ToolCall(id=str(i), name="see_desktop", arguments="{}"),))
        for i in range(10)
    ]
    runner = TurnRunner(llm=fake_llm, tools=build_tools(runtime), builder=...)
    await runner.run(turn=...)
    assert runtime.delivered == ["嗯……我这边卡了一下，等我一下。"]
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_turn_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: agent.aliya.turn.runner`

**Step 3: 最小实现**

```python
class TurnRunner:
    def __init__(self, *, llm: LLMService, tools: ToolRegistry, builder: PromptBuilder, config: AgentAliyaSettings) -> None:
        self._llm = llm
        self._tools = tools
        self._builder = builder
        self._config = config

    async def run(self, *, turn: TurnContext) -> None:
        messages = self._builder.build(turn=turn)
        for _ in range(self._config.reply.max_rounds):
            reply = await asyncio.wait_for(
                self._llm.chat_tools(messages, self._tools.declarations()),
                timeout=self._config.reply.turn_timeout_seconds,
            )
            if reply.content:
                messages.append(assistant(reply.content))
            if not reply.tool_calls:
                if not turn.sent and reply.content:
                    await turn.send(reply.content)
                return
            messages.append(assistant(reply.content, tool_calls=reply.tool_calls))
            for call in reply.tool_calls:
                result = await self._tools.execute(call)
                messages.append(tool_result(call.id, result))
        await turn.send(self._config.reply.fallback_text)
```

`turn.sent` 由 `send_message` 工具置位；`turn.send` 走 `PostChecker` 后投递。

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/test_turn_runner.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/aliya/turn/runner.py tests/aliya/test_turn_runner.py
git commit -m "feat(aliya): 添加回合工具循环与 fallback"
```

---

### Task 16: `PostChecker` 出站检查

**Files:**
- Create: `agent/aliya/turn/postcheck.py`
- Test: `tests/aliya/test_postcheck.py`

**Step 1: 写失败测试**

```python
def test_masks_internal_id() -> None:
    result = check("任务 tsk_abc 完成了")
    assert "tsk_abc" not in result.text


def test_flags_narration() -> None:
    result = check("（笑）好啊")
    assert "narration" in result.rules


def test_keeps_punctuation() -> None:
    result = check("在吗？")
    assert result.text == "在吗？"
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_postcheck.py -v`
Expected: FAIL with `ModuleNotFoundError: agent.aliya.turn.postcheck`

**Step 3: 最小实现**

```python
_RULES = {
    "narration": re.compile(r"[（(][^）)]{1,80}[）)]|\*[^*]{1,80}\*"),
    "engineering_terms": re.compile(r"玩家|用户|agent|模型|提示词|调度|面板|节点"),
    "internal_ids": re.compile(r"\b(?:tsk|dsp|evt|ses|req)_[A-Za-z0-9]+\b"),
    "presence_leak": re.compile(r"检测到|系统显示|状态显示|readable"),
}


def check(text: str) -> CheckResult:
    rules = [name for name, pattern in _RULES.items() if pattern.search(text)]
    masked = _RULES["internal_ids"].sub("那件事", text)
    return CheckResult(text=masked, rules=rules)
```

`rewrite` 流程：命中且可重写 → 把违规文本与规则名回给模型重写一次；仍不通过 → 遮蔽标识、删掉命中片段 → 发送 + WARNING。句尾标点缺失只记 WARNING，不自动补。空消息丢弃。

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/test_postcheck.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/aliya/turn/postcheck.py tests/aliya/test_postcheck.py
git commit -m "feat(aliya): 添加出站检查与遮蔽降级"
```

---

# M6 记忆与自主

### Task 17: `MemoryStore`

**Files:**
- Create: `agent/aliya/memory/store.py`
- Create: `agent/aliya/memory/__init__.py`
- Test: `tests/aliya/test_memory_store.py`

**Step 1: 写失败测试**

```python
def test_append_and_reload(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    store.append_turn("u1", "user", "在吗")
    store.append_turn("a1", "aliya", "在。")
    assert [turn.text for turn in store.recent_turns()] == ["在吗", "在。"]


def test_atomic_write_keeps_old_on_crash(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    store.save_anchors(["风信子"])
    assert store.load_anchors() == ["风信子"]
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_memory_store.py -v`
Expected: FAIL with `ModuleNotFoundError: agent.aliya.memory.store`

**Step 3: 最小实现**

文件：`turns.jsonl`、`summary.json`、`anchors.json`、`open_topics.json`、`unspoken.json`、`facts.json`、`cursor.json`。写入用「临时文件 + `os.replace`」；JSON 损坏备份 `.bad` 后从空开始。`append_turn` 直接追加一行 JSON；`recent_turns` 读最后 N 条。`remember` 落盘前做一次 PII 扫描（`sk-` / `ghp_` 前缀、身份证与银行卡正则），命中拒绝并 WARNING。

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/test_memory_store.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/aliya/memory/ tests/aliya/test_memory_store.py
git commit -m "feat(aliya): 添加 JSONL 长期记忆"
```

---

### Task 18: `Initiative` 三层门控

**Files:**
- Create: `agent/aliya/initiative.py`
- Test: `tests/aliya/test_initiative.py`

**Step 1: 写失败测试**

```python
def test_default_is_wait(settings) -> None:
    gate = Initiative(settings.agent.aliya.initiative)
    decision = gate.should_speak(now=now(), presence=None, link="up", last_user_at=now())
    assert decision.should_speak is False


def test_speaks_after_long_gap(settings) -> None:
    gate = Initiative(settings.agent.aliya.initiative)
    decision = gate.should_speak(
        now=now(), presence=None, link="up", last_user_at=now() - timedelta(hours=3)
    )
    assert decision.should_speak is True
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_initiative.py -v`
Expected: FAIL with `ModuleNotFoundError: agent.aliya.initiative`

**Step 3: 最小实现**

```python
@dataclass(frozen=True)
class InitiativeDecision:
    should_speak: bool
    reason: str


class Initiative:
    def __init__(self, config: AgentAliyaInitiativeSettings) -> None:
        self._config = config
        self._last_proactive_at: datetime | None = None
        self._daily_count = 0

    def should_speak(
        self,
        *,
        now: datetime,
        presence: ReadablePresence | None,
        link: LinkState,
        last_user_at: datetime | None,
    ) -> InitiativeDecision:
        if not self._config.enabled:
            return InitiativeDecision(False, "disabled")
        if link != "up":
            return InitiativeDecision(False, "link_down")
        if presence is not None and presence.state in {"typing", "away"}:
            return InitiativeDecision(False, "presence")
        if self._last_proactive_at and (now - self._last_proactive_at).total_seconds() < self._config.min_gap_seconds:
            return InitiativeDecision(False, "min_gap")
        if self._daily_count >= self._config.daily_soft_limit:
            return InitiativeDecision(False, "daily_limit")
        if last_user_at is None or (now - last_user_at).total_seconds() >= 2 * 3600:
            return InitiativeDecision(True, "long_gap")
        return InitiativeDecision(False, "default_wait")
```

连续两条主动消息没有回应时，间隔 ×2；用户回话后重置。

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/test_initiative.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/aliya/initiative.py tests/aliya/test_initiative.py
git commit -m "feat(aliya): 添加自主行动三层门控"
```

---

### Task 19: `AliyaContext` 与 phase

**Files:**
- Modify: `agent/aliya/runtime.py`
- Test: `tests/aliya/test_phase.py`

**Step 1: 写失败测试**

```python
def test_phase_rules() -> None:
    assert phase_for(now=at(23, 30), tasks=0, crisis=False) == "rest"
    assert phase_for(now=at(12, 0), tasks=1, crisis=False) == "maintenance"
    assert phase_for(now=at(12, 0), tasks=0, crisis=True) == "crisis"
    assert phase_for(now=at(12, 0), tasks=0, crisis=False) == "daily"
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_phase.py -v`
Expected: FAIL（`phase_for` 不存在）

**Step 3: 最小实现**

```python
def phase_for(*, now: datetime, tasks: int, crisis: bool) -> Literal["daily", "maintenance", "crisis", "rest"]:
    if tasks > 0:
        return "maintenance"
    if crisis:
        return "crisis"
    if 23 <= now.hour or now.hour < 5:
        return "rest"
    return "daily"
```

每回合结束后通过 `control.set_aliya_context(AliyaContext(...))` 回传 `phase`、未收尾话题与当前处境。

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/test_phase.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/aliya/runtime.py tests/aliya/test_phase.py
git commit -m "feat(aliya): 回合结束后回传 AliyaContext"
```

---

# M7 运行时与入口

### Task 20: `AliyaRuntime`

**Files:**
- Create: `agent/aliya/runtime.py`
- Test: `tests/aliya/test_runtime.py`

**Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_inbound_turn_is_serial(fake_control, fake_llm) -> None:
    runtime = AliyaRuntime(control=fake_control, llm=fake_llm, ...)
    await runtime.start()
    await fake_control.push_inbound(make_envelope("m1"))
    await runtime.wait_idle()
    assert fake_control.delivered == ["在。"]


@pytest.mark.asyncio
async def test_turn_timeout_falls_back(fake_control, slow_llm) -> None:
    runtime = AliyaRuntime(control=fake_control, llm=slow_llm, ...)
    await runtime.start()
    await fake_control.push_inbound(make_envelope("m1"))
    await runtime.wait_idle()
    assert fake_control.delivered == ["嗯……我这边卡了一下，等我一下。"]
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_runtime.py -v`
Expected: FAIL with `ModuleNotFoundError: agent.aliya.runtime`

**Step 3: 最小实现**

关键结构：

```python
class AliyaRuntime:
    def __init__(self, *, control: ControlPort, llm: LLMService, config: AgentAliyaSettings) -> None:
        self._control = control
        self._llm = llm
        self._config = config
        self._queue: asyncio.Queue[TurnContext] = asyncio.Queue(maxsize=32)
        self._worker: asyncio.Task[None] | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._idle = asyncio.Event()

    async def start(self) -> None:
        await self._control.handshake()
        self._worker = asyncio.create_task(self._run_worker())
        self._ticker = asyncio.create_task(self._run_ticker())
        asyncio.create_task(self._consume_inbound())
        asyncio.create_task(self._consume_events())

    async def _consume_inbound(self) -> None:
        async for envelope in self._control.inbound():
            if self._seen(envelope.message.message_id):
                continue
            await self._queue.put(TurnContext.from_envelope(envelope))
```

事件循环只更新缓存，`agent.result` / `dispatch.finished` / `agent.abnormal` 才入队开汇报回合。`_run_worker` 串行执行 `TurnRunner`，回合结束写 `turns.jsonl`、更新 `AliyaContext`。启动时检查 `turns.jsonl` 最后一条：若是用户消息且没有对应回复，补一个回合（幂等键 `message_id`）。

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/test_runtime.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/aliya/runtime.py tests/aliya/test_runtime.py
git commit -m "feat(aliya): 添加运行时、队列与回合恢复"
```

---

### Task 21: `app.py` 入口

**Files:**
- Create: `agent/aliya/app.py`
- Test: `tests/aliya/test_app.py`

**Step 1: 写失败测试**

```python
def test_missing_persona_exits_nonzero(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALIYA_CONTROL_TOKEN", "t")
    with pytest.raises(SystemExit) as exc:
        main(config_path=tmp_path / "missing.yaml")
    assert exc.value.code != 0
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError: agent.aliya.app`

**Step 3: 最小实现**

```python
async def run(config_path: Path | None = None) -> None:
    settings = load_settings(config_path or CONFIG_FILE)
    token = os.environ.get(settings.agent.control.token_env, "")
    if not token:
        raise SystemExit("缺少控制面令牌")
    control = RemoteControl(settings.agent.control.socket_path, token=token)
    await control.connect()
    runtime = AliyaRuntime(control=control, llm=LLMService(), config=settings.agent.aliya)
    await runtime.start()
    try:
        await runtime.wait_closed()
    finally:
        await control.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
```

人格或准则缺失、契约主版本不一致：启动失败，非零退出。

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/test_app.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add agent/aliya/app.py tests/aliya/test_app.py
git commit -m "feat(aliya): 添加进程入口与启动校验"
```

---

### Task 22: 端到端演练

**Files:**
- Create: `tests/aliya/test_e2e.py`
- Modify: `README.md`（启动顺序）

**Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_chat_dispatch_and_result(e2e) -> None:
    await e2e.user_says("帮我看一下登录")
    assert e2e.last_reply
    assert e2e.dispatched == ["echo"]
    await e2e.finish_task()
    assert "好了" in e2e.last_reply
```

**Step 2: 跑测试确认失败**

Run: `uv run pytest tests/aliya/test_e2e.py -v`
Expected: FAIL（链路未接齐）

**Step 3: 接线**

用真 `ControlServerService` + 真 UDS + 假 LLM + `echo` agent：先起 cosmos，再起 Aliya；用户消息走 `MemoryUserChannel`；断言聊天、调度、结果汇报、主动消息被拒四条路径。

**Step 4: 跑测试**

Run: `uv run pytest tests/aliya/test_e2e.py -v`
Expected: PASS

**Step 5: 提交**

```bash
git add tests/aliya/test_e2e.py README.md
git commit -m "test(aliya): 端到端演练聊天、调度与结果汇报"
```

---

# M8 文档与收尾

### Task 23: 文档同步

**Files:**
- Modify: `agent/cosmos/README.md`
- Modify: `agent/cosmos/docs/01-architecture.md`
- Modify: `agent/cosmos/docs/09-control-api.md`
- Modify: `README.md`
- Modify: `agent/aliya/config/agent-settings.example.yaml`

**Step 1: 写失败测试**

无代码测试；用 `rg` 检查旧口径：

```bash
rg "14 个|10 个常驻服务" agent/cosmos/README.md
```

**Step 2: 跑检查确认不一致**

Run: `rg "14 个|10 个常驻服务" agent/cosmos/README.md`
Expected: 命中旧服务数

**Step 3: 改文档**

- `agent/cosmos/README.md`：服务数 14 → 15；组件表加 `ControlServerService`。
- `agent/cosmos/docs/01-architecture.md`：补控制面服务端与进程拓扑。
- `agent/cosmos/docs/09-control-api.md`：补 `client_msg_id`、`now`、RPC 映射。
- `README.md`：启动顺序先 cosmos 后 aliya。
- `agent/aliya/config/agent-settings.example.yaml`：字段与 `AgentAliyaSettings` 对齐。

**Step 4: 复查**

Run: `rg "14 个|10 个常驻服务" agent/cosmos/README.md`
Expected: 无命中

**Step 5: 提交**

```bash
git add agent/cosmos/README.md agent/cosmos/docs/01-architecture.md agent/cosmos/docs/09-control-api.md README.md agent/aliya/config/agent-settings.example.yaml
git commit -m "docs: 同步控制面与 Aliya 进程文档"
```

---

### Task 24: 全量验证

**Files:**
- 无新增文件

**Step 1: 跑全量测试**

Run: `uv run pytest`
Expected: 全绿，且测试数 ≥ 722

**Step 2: 跑 lint**

Run: `uv run ruff check .`
Expected: 无错误

**Step 3: 跑格式检查**

Run: `uv run ruff format --check .`
Expected: 无差异

**Step 4: 手工烟测**

```bash
# 终端 1
uv run python -m agent.cosmos.app
# 终端 2
ALIYA_CONTROL_TOKEN=dev uv run python -m agent.aliya.app
```

预期：cosmos 15 个服务健康；Aliya 后起能连上；两边 `SIGINT` 都以 0 退出。

**Step 5: 提交收尾**

```bash
git add -A
git commit -m "chore: 全量验证与收尾"
```

---

## 验收口径

- 聊天：用户消息到达 Aliya，`send_message` 映射 `deliver`，回复无工程词、无旁白、标点保留。
- 任务：`dispatch_task` 映射 `plan` + `execute_plan`；结果事件开汇报回合，文本不含内部标识。
- 主动：三层门控生效；被拒不重试；无动作不消耗配额。
- 记忆：重启后锚点、未收尾话题、私密线仍在；`turns.jsonl` 可恢复未完成回合。
- 传输：断线重连后入站不丢、出站按 `client_msg_id` 去重；第二客户端被拒。
- 文档：cosmos 15 个服务；启动顺序先 cosmos 后 Aliya；配置样例与模型字段一致。
