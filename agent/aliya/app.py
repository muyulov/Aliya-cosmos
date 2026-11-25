"""Aliya 进程入口：连 cosmos 控制面，启动运行时，等信号退出。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from agent.aliya.runtime import AliyaRuntime
from agent.aliya.transport import RemoteControl
from core.config import CONFIG_FILE, load_settings
from core.llm import LLMService
from core.logger import faces, log, setup_logging


async def run(config_path: Path | None = None) -> None:
    if config_path is not None and not Path(config_path).is_file():
        raise SystemExit(f"配置文件不存在：{config_path}")
    settings = load_settings(config_path or CONFIG_FILE)
    _ = setup_logging(settings.log)
    log.info(
        "Aliya 配置已加载",
        face=faces.START,
        配置源=settings.config_source,
        控制面=settings.agent.control.socket_path,
        模型=settings.llm.chat.model,
        端点=settings.llm.chat.base_url,
    )
    token = os.environ.get(settings.agent.control.token_env, "")
    if not token:
        log.error("缺少控制面令牌，无法启动", 环境变量=settings.agent.control.token_env)
        raise SystemExit("缺少控制面令牌")

    control = RemoteControl(settings.agent.control.socket_path, token=token)
    # LLM 端点按配置建客户端；没配 api_key 只警告，调用时才报错。
    llm = LLMService(settings.llm)
    await llm.start()
    runtime: AliyaRuntime | None = None
    try:
        await control.connect()
        runtime = AliyaRuntime(
            control=control,
            llm=llm,
            config=settings.agent.aliya,
        )
        await runtime.start()
        log.info(
            "Aliya 已启动",
            face=faces.LOVE,
            控制面=settings.agent.control.socket_path,
            模型=settings.llm.chat.model,
        )
        await runtime.wait_closed()
    except Exception as exc:
        log.exception(
            "Aliya 运行失败，进程退出",
            错误类型=type(exc).__name__,
            错误消息=str(exc),
        )
        raise
    finally:
        if runtime is not None:
            await runtime.stop()
        await control.close()
        await llm.stop()
        log.info("Aliya 已关闭", face=faces.BYE)


def main(config_path: Path | None = None) -> None:
    try:
        asyncio.run(run(config_path))
    except KeyboardInterrupt:
        log.info("收到中断信号，退出", face=faces.BYE)


if __name__ == "__main__":
    main()
