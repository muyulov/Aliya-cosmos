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

#: 健康检查日志的保留字段：服务自报的 extra 与它们同名时一律让位，
#: 免得服务把 健康=true 这类元数据覆盖掉（与 HealthStatus.to_dict 同一条规则）。
HEALTH_LOG_RESERVED_KEYS: frozenset[str] = frozenset({"服务", "健康", "状态", "详情"})


async def _run(manager: ServiceManager) -> None:
    """启动全部服务并常驻，直到收到退出信号。"""
    async with manager.lifespan():
        await _report_health(manager)
        await _wait_for_shutdown()


async def _report_health(manager: ServiceManager) -> None:
    """打印一次健康检查结果。

    健康时状态恒为 running、`detail` 恒为空，「服务健康 健康=true」已经说完；
    不健康时这两项才是定位所需，按需附加即可，正常日志不会多出冗余字段
    （`detail` 尤其：健康检查抛错的原因就写在这里，丢了就看不到）。

    服务自报的 `HealthStatus.extra`（模型 / 端点 / 时区…）一并带出：诊断「接的
    是哪一家」时它就写在同一条日志上，不必再去翻配置或代码。
    """
    for status in await manager.health():
        # 值声明成 str：`**fields` 会被类型检查器逐个形参对账，object 连 face 都对不上
        fields: dict[str, str] = {} if status.healthy else {"状态": status.state.value}
        if status.detail:
            fields["详情"] = status.detail
        for key, value in status.extra.items():
            if key not in HEALTH_LOG_RESERVED_KEYS:
                fields[key] = str(value)
        log.info("服务健康", 服务=status.name, 健康=status.healthy, **fields)


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
    # 本次启动的日志文件路径只有排查时才需要，正常流程里不消费
    _ = setup_logging(settings.log)

    log.info(
        "应用启动中",
        face=faces.START,
        应用=settings.app.app_name,
        环境=settings.app.env,
        配置源=settings.config_source,
    )
    try:
        asyncio.run(_run(build_manager(settings)))
    except Exception as exc:
        # 装配 / 启动失败是 fail fast：日志要收口（含堆栈）再以非零码退出。
        # 不捕获的话只剩一段裸 traceback，日志文件里也没有「这次是整体失败」的结论。
        log.exception("应用启动失败，进程退出", 错误=f"{type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc
    log.info("应用已关闭", face=faces.BYE, 应用=settings.app.app_name)


if __name__ == "__main__":
    main()
