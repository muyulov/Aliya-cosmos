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
    `retries` 不在这里传：httpx2 的重试只管连接层，429 / 502 / 503 / 504 / 529 的退避重试
    由服务层实现。
    `max_connections` 只在配置给值时收窄连接池（并发 predict_many 时压住对端点的连接数）；
    为 None 时整段不传，交给 httpx2 的默认 Limits，不改动它认识而本层不关心的其余参数。
    """
    headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
    if endpoint.max_connections is None:
        return httpx2.AsyncClient(
            base_url=endpoint.base_url,
            headers=headers,
            timeout=endpoint.timeout,
        )
    return httpx2.AsyncClient(
        base_url=endpoint.base_url,
        headers=headers,
        timeout=endpoint.timeout,
        limits=httpx2.Limits(max_connections=endpoint.max_connections),
    )
