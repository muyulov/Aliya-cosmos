"""Aliya 进程入口：连 cosmos 控制面，启动运行时，等信号退出。"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from agent.aliya.runtime import AliyaRuntime
from agent.aliya.transport import RemoteControl
from core.config import CONFIG_FILE, load_settings
from core.llm import LLMService


async def run(config_path: Path | None = None) -> None:
    if config_path is not None and not Path(config_path).is_file():
        raise SystemExit(f"配置文件不存在：{config_path}")
    settings = load_settings(config_path or CONFIG_FILE)
    token = os.environ.get(settings.agent.control.token_env, "")
    if not token:
        raise SystemExit("缺少控制面令牌")

    control = RemoteControl(settings.agent.control.socket_path, token=token)
    runtime: AliyaRuntime | None = None
    try:
        await control.connect()
        runtime = AliyaRuntime(
            control=control,
            llm=LLMService(),
            config=settings.agent.aliya,
        )
        await runtime.start()
        await runtime.wait_closed()
    finally:
        if runtime is not None:
            await runtime.stop()
        await control.close()


def main(config_path: Path | None = None) -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(config_path))
