"""入口：装配服务容器并驱动其生命周期。

uv run python -m core.main

流程：初始化日志 → 构造服务容器 → 启动全部服务 → 打印健康检查 →
等待退出信号 → 优雅关闭。业务逻辑在 lifespan 内挂载。
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from types import FrameType

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
        _ = _register_signal(loop, sig, stop.set)
    _ = await stop.wait()


def _register_signal(
    loop: asyncio.AbstractEventLoop, sig: signal.Signals, on_signal: Callable[[], None]
) -> bool:
    """把信号绑到回调，返回是否用上了事件循环。

    优先 `add_signal_handler`（POSIX 的标准做法，与事件循环天然集成）；Windows 的
    proactor 循环不实现它，退回 `signal.signal`——信号处理器不在事件循环线程里跑，
    因此必须经 `call_soon_threadsafe` 置位。
    """
    try:
        _ = loop.add_signal_handler(sig, on_signal)
    except NotImplementedError:
        _ = signal.signal(sig, _threadsafe_handler(loop, on_signal))
        return False
    return True


def _threadsafe_handler(
    loop: asyncio.AbstractEventLoop, on_signal: Callable[[], None]
) -> Callable[[int, FrameType | None], None]:
    """构造 `signal.signal` 用的处理器。"""

    def _handler(_signum: int, _frame: FrameType | None) -> None:
        _ = loop.call_soon_threadsafe(on_signal)

    return _handler


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
