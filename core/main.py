"""入口：仅负责构造应用与启动 uvicorn。

uv run python -m core.main
uv run uvicorn core.main:app --reload
"""

from __future__ import annotations

import uvicorn

from core.api import create_app
from core.config import get_settings

settings = get_settings()
app = create_app(settings)


def main() -> None:
    uvicorn.run(
        "core.main:app",
        host=settings.app.host,
        port=settings.app.port,
        reload=settings.app.debug,
        log_config=None,  # 交给 loguru 统一处理
    )


if __name__ == "__main__":
    main()
