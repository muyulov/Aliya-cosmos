"""cosmos 进程入口。

uv run python -m agent.cosmos.app

只做三件事：读配置、初始化日志、驱动容器生命周期。业务逻辑在服务里，
入口不承担装配细节（装配在 `assembly.build_cosmos_manager`）。
"""

from __future__ import annotations

import asyncio

from agent.cosmos.assembly import build_cosmos_manager
from core.config import get_settings
from core.logger import faces, log, setup_logging
from core.main import run


def main() -> None:
    settings = get_settings()
    _ = setup_logging(settings.log)
    log.info(
        "cosmos 启动中",
        face=faces.START,
        应用=settings.app.app_name,
        配置源=settings.config_source,
    )
    try:
        asyncio.run(run(build_cosmos_manager(settings)))
    except Exception as exc:
        # 装配 / 启动失败是 fail fast：日志要收口（含堆栈）再以非零码退出
        log.exception(
            "cosmos 启动失败，进程退出",
            错误类型=type(exc).__name__,
            错误消息=str(exc),
        )
        raise SystemExit(1) from exc
    log.info("cosmos 已关闭", face=faces.BYE, 应用=settings.app.app_name)


if __name__ == "__main__":
    main()
