"""cosmos 终端通道：在控制台里直接与 Aliya 对话。

两个终端，令牌必须相同：

    ALIYA_CONTROL_TOKEN=dev uv run python -m agent.cosmos.cli
    ALIYA_CONTROL_TOKEN=dev uv run python -m agent.aliya.app

CLI 进程本身就是 cosmos：启动全部常驻服务与 UDS 控制面，Aliya 作为独立进程连上来。
用户输入经 `TerminalService.ingest()` 进总线，Aliya 的回复经 `UserChannelPort` 打印。
先开 CLI 再开 Aliya 也不会丢消息：消息总线在无订阅者时会积压。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator

from agent.cosmos.assembly import build_cosmos_manager
from agent.cosmos.contracts import TerminalMessage
from agent.cosmos.services import TerminalService
from core.config import Settings, get_settings
from core.logger import setup_logging

#: 退出命令，大小写不敏感
_QUIT_COMMANDS = frozenset({"/quit", "/exit", ":q"})
#: 默认会话标识：同一会话的历史与摘要会累积
_DEFAULT_SESSION = "cli"


class ConsoleUserChannel:
    """用户通道：把 Aliya 发出的消息逐条打到 stdout。

    cosmos 只做分段与脱敏、不合并；这里也逐条打印，保持「她发几条就是几条」。
    """

    async def send(self, message: TerminalMessage) -> None:
        print(f"\nAliya> {message.text}", flush=True)


async def _stdin_lines() -> AsyncIterator[str]:
    """逐行读取 stdin，EOF 时结束。

    优先用事件循环读管道：Ctrl-C 时不会留下阻塞在 `input()` 的线程，
    退出不必等线程池收尾。Windows 的事件循环不实现它，退回线程读取。
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    try:
        _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    except (NotImplementedError, ValueError):
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return
            yield line.rstrip("\r\n")
    while True:
        line = await reader.readline()
        if not line:
            return
        yield line.decode("utf-8", "replace").rstrip("\r\n")


def _print_banner(settings: Settings) -> None:
    """启动提示：控制面位置、令牌状态与 Aliya 的启动命令。"""
    control = settings.agent.control
    token = os.environ.get(control.token_env, "")
    print("cosmos 终端通道已就绪（输入 /quit 退出）", flush=True)
    print(f"  控制面：{control.socket_path}", flush=True)
    if token:
        print(f"  令牌：{control.token_env} 已设置", flush=True)
    else:
        print(
            f"  警告：未设置 {control.token_env}，Aliya 进程需要同一个非空令牌才能连上",
            file=sys.stderr,
            flush=True,
        )
    print(
        f"  另一个终端启动 Aliya：{control.token_env}=<同一个值> uv run python -m agent.aliya.app",
        flush=True,
    )


async def run(settings: Settings, session_id: str = _DEFAULT_SESSION) -> None:
    """装配 cosmos，读 stdin 并投递用户消息，直到 EOF 或退出命令。"""
    manager = build_cosmos_manager(settings)
    channel = ConsoleUserChannel()
    async with manager.lifespan():
        terminal = manager.get(TerminalService)
        terminal.set_user_channel(channel)
        _print_banner(settings)
        print("\n你> ", end="", flush=True)
        async for line in _stdin_lines():
            text = line.rstrip()
            if text.strip().lower() in _QUIT_COMMANDS:
                break
            if text.strip():
                _ = await terminal.ingest(
                    TerminalMessage(
                        direction="user_to_aliya",
                        kind="chat",
                        text=text,
                        session_id=session_id,
                    )
                )
            print("\n你> ", end="", flush=True)


def main() -> None:
    settings = get_settings()
    # 交互模式下终端只留 ERROR：INFO 照常完整写进日志文件
    _ = setup_logging(settings.log, console_level="ERROR")
    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run(settings))
    except Exception as exc:
        print(f"启动失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
    finally:
        print("\n已退出", flush=True)


if __name__ == "__main__":
    main()
