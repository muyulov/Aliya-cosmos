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
from typing import cast

from agent.cosmos.assembly import build_cosmos_manager
from agent.cosmos.contracts import TerminalMessage
from agent.cosmos.services import TerminalService
from core.config import Settings, get_settings
from core.logger import faces, log, setup_logging

#: 退出命令，大小写不敏感
_QUIT_COMMANDS = frozenset({"/quit", "/exit", ":q"})
#: 默认会话标识：同一会话的历史与摘要会累积
_DEFAULT_SESSION = "cli"


def _enable_ansi() -> bool:
    """让 Windows 控制台也解释 ANSI 转义（VT 处理默认关闭）。"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = cast("int", kernel32.GetStdHandle(-11))
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(cast("int", kernel32.SetConsoleMode(handle, mode.value | 0x0004)))
    except (AttributeError, OSError):
        return False


class ConsoleUserChannel:
    """用户通道：把 Aliya 发出的消息逐条打到 stdout。

    cosmos 只做分段与脱敏、不合并；这里也逐条打印，保持「她发几条就是几条」。
    回复随时可能到达——包括用户正在敲下一句的时候——所以打印前先清掉当前行的
    输入提示符，打完再把提示符补回来，避免出现「你> Aliya> ...」这种串行。
    """

    _PROMPT: str = "你> "
    _REPLY_PREFIX: str = "Aliya> "
    _CLEAR_LINE: str = "\r\033[K"

    def __init__(self) -> None:
        # 管道/重定向时不打提示符、不清行，输出保持干净可解析
        self._interactive: bool = sys.stdout.isatty() and _enable_ansi()
        self._prompt_shown: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()

    async def send(self, message: TerminalMessage) -> None:
        async with self._lock:
            self._write(message.text)

    def prompt(self) -> None:
        """在空闲行上摆好输入提示符。"""
        if not self._interactive or self._prompt_shown:
            return
        _ = sys.stdout.write(self._PROMPT)
        _ = sys.stdout.flush()
        self._prompt_shown = True

    def submitted(self) -> None:
        """用户按下了回车：提示符随输入行被终端换行，不再占着当前行。"""
        self._prompt_shown = False

    def close(self) -> None:
        """退出前结束提示符所在行，别让 shell 提示符接在同一行。"""
        if self._prompt_shown:
            _ = sys.stdout.write("\n")
            _ = sys.stdout.flush()
            self._prompt_shown = False

    def _write(self, text: str) -> None:
        if self._prompt_shown:
            _ = sys.stdout.write(self._CLEAR_LINE)
            self._prompt_shown = False
        lines = text.splitlines() or [""]
        print(f"{self._REPLY_PREFIX}{lines[0]}", flush=True)
        indent = " " * len(self._REPLY_PREFIX)
        for line in lines[1:]:
            print(f"{indent}{line}", flush=True)
        self.prompt()


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
        log.warning("未设置控制面令牌，Aliya 进程将无法连接", 环境变量=control.token_env)
        print(
            f"  警告：未设置 {control.token_env}，Aliya 进程需要同一个非空令牌才能连上",
            file=sys.stderr,
            flush=True,
        )
    print(
        f"  另一个终端启动 Aliya：{control.token_env}=<同一个值> uv run python -m agent.aliya.app",
        flush=True,
    )
    print(flush=True)


async def run(settings: Settings, session_id: str = _DEFAULT_SESSION) -> None:
    """装配 cosmos，读 stdin 并投递用户消息，直到 EOF 或退出命令。"""
    manager = build_cosmos_manager(settings)
    channel = ConsoleUserChannel()
    log.info("终端会话已开始", 会话=session_id, 控制面=settings.agent.control.socket_path)
    try:
        async with manager.lifespan():
            terminal = manager.get(TerminalService)
            terminal.set_user_channel(channel)
            _print_banner(settings)
            channel.prompt()
            async for line in _stdin_lines():
                channel.submitted()
                text = line.strip()
                if text.lower() in _QUIT_COMMANDS:
                    log.info("收到退出命令", 命令=text)
                    break
                if text:
                    log.debug("投递用户消息", 会话=session_id, 字数=len(text))
                    _ = await terminal.ingest(
                        TerminalMessage(
                            direction="user_to_aliya",
                            kind="chat",
                            text=text,
                            session_id=session_id,
                        )
                    )
                channel.prompt()
    finally:
        channel.close()
        log.info("终端会话已结束", 会话=session_id)


def main() -> None:
    settings = get_settings()
    # 交互模式下终端只留 ERROR：INFO 照常完整写进日志文件
    _ = setup_logging(settings.log, console_level="ERROR")
    log.info(
        "cosmos 终端通道启动",
        face=faces.START,
        配置源=settings.config_source,
        控制面=settings.agent.control.socket_path,
    )
    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run(settings))
    except Exception as exc:
        log.exception(
            "cosmos 终端通道启动失败",
            错误类型=type(exc).__name__,
            错误消息=str(exc),
        )
        print(f"启动失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
    finally:
        print("\n已退出", flush=True)
        log.info("cosmos 终端通道已退出", face=faces.BYE)


if __name__ == "__main__":
    main()
