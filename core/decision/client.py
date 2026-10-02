"""判断层客户端工厂。

约定：这是全仓库唯一 new 出 httpx2 客户端的地方，也是测试注入替身的接缝。
base_url 只写到 /v1 为止（两家的完整地址都是 …/v1/systemone），路径由 SYSTEMONE_PATH 补。
"""

from __future__ import annotations

import httpx2

from core.config import DecisionEndpointSettings

#: /systemone 是 jev 与 laya-serve 共用的线协议路径
SYSTEMONE_PATH = "/systemone"


def build_client(endpoint: DecisionEndpointSettings) -> httpx2.AsyncClient:
    """按端点配置构造异步客户端。

    api_key 为空时不发 Authorization 头：laya-serve 不设 LAYA_API_KEY 时本来就不要求认证。
    """
    headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
    return httpx2.AsyncClient(
        base_url=endpoint.base_url,
        headers=headers,
        timeout=endpoint.timeout,
    )
