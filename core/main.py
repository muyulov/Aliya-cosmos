"""入口：装配服务容器并驱动其生命周期。

uv run python -m core.main

流程：初始化日志 → 构造服务容器 → 启动全部服务 → 打印健康检查 →
等待退出信号 → 优雅关闭。业务逻辑在 lifespan 内挂载。
"""

from __future__ import annotations

import asyncio
import signal

from core.config import get_settings
from core.logger import faces, log, setup_logging
from core.service import ServiceManager
from core.service.registry import build_manager


async def _run(manager: ServiceManager) -> None:
    """启动全部服务并常驻，直到收到退出信号。"""
    async with manager.lifespan():
        for status in await manager.health():
            log.info("服务健康", 服务=status.name, 健康=status.healthy, 状态=status.state.value)
        await _wait_for_shutdown()


async def _wait_for_shutdown() -> None:
    """等待 SIGINT / SIGTERM，收到后返回以触发优雅关闭。"""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        _ = loop.add_signal_handler(sig, stop.set)
    _ = await stop.wait()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log)

    log.info(
        "应用启动中",
        face=faces.START,
        应用=settings.app.app_name,
        环境=settings.app.env,
        配置源=settings.config_source,
    )
    asyncio.run(_run(build_manager(settings)))
    log.info("应用已关闭", face=faces.BYE, 应用=settings.app.app_name)


if __name__ == "__main__":
    main()
