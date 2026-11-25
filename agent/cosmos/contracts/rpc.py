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
