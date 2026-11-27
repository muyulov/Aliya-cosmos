# 判断层

`DecisionService` 把「让模型做一次判断」收成一个服务，业务代码不直接接触 HTTP。两个端点（jev 云端 / laya 本地）走**同一套 `/systemone` 协议、字段完全相同**——换 `base_url` 即可切换供应商。`backend` 显式选端点（默认 `laya`，本地优先：免费、数据不出域），**层内不做兜底**。

```python
from core.decision import DecisionService, choice, noul, score


async def main(svc: DecisionService, message: str) -> None:
    # 批量：一次前向把三题问完（协议的延迟优势就在这里）
    result = await svc.predict(
        message,
        {
            "部门": choice("归哪个部门？", {"billing": "账单", "technical": "技术故障"}),
            "愤怒": score("愤怒程度？", ["平静", "不满", "愤怒"]),
            "退款": noul("是否明确要求退款？", true="明确要求", false="没提"),
        },
    )
    department = result.answers["部门"]

    # 便捷方法：一次只问一题
    picked = await svc.choose(message, "归哪个部门？", {"billing": "账单", "technical": "技术"})
    anger = await svc.rate(message, "愤怒程度？", ["平静", "不满", "愤怒"])
    wants_refund = await svc.ask(message, "是否明确要求退款？")

    # 换后端：默认 laya（本地），要云端就传 backend
    on_cloud = await svc.ask(message, "是否明确要求退款？", backend="jev")

    # 并发批量：多条相互独立的提问一起飞；limit 限制同时在飞的请求数（结果顺序与入参一致）
    batched = await svc.predict_many(
        [
            (message, {"退款": noul("是否要求退款？")}),
            (other, {"部门": choice("归哪个部门？", {"billing": "账单"})}),
        ],
        backend="jev",
        limit=4,
    )

    # 结果带回本次实际走的端点：confidence 跨后端不可比，按 result.backend 分别校准阈值
    assert result.backend == "laya"
```

端点地址、模型与重试配置见[配置](../config.md)。

## 约定

| 约定 | 说明 |
| --- | --- |
| 不记正文 | 日志记调用开始（后端 / 模型 / 端点 / 问题数 / 问题 id，DEBUG）与完成（后端 / 问题数 / 耗时 / token），失败记尝试次数与状态码；`state` 与 `questions` 的正文由调用方决定要不要自己记 |
| `confidence` 不能跨后端比较 | Jev 的 `confidence` 是 `(n·p_max−1)/(n−1)`，Laya 是 `1−归一化熵`，两个数不是同一个量；阈值要按当前 `backend` 单独校准 |
| 零样本质量很差 | 官方 model card 自述「Laya 是便于特化的基座，不是零样本决策引擎」（零样本准确率接近随机），判断结果必须配阈值或人工兜底 |
| 两端点同协议 | jev 与 laya 走同一套 `/systemone` 协议、字段完全相同，换 `base_url` 即可切换供应商 |
| `backend` 显式选 | 默认 `laya`（本地优先），要云端显式传 `backend="jev"` |
| 层内不兜底 | 一个端点未启用时直接抛 `DecisionConfigError`，不会静默换另一头；`confidence` 跨端点语义不同，兜底必然出错 |
| 缺端点不 fail fast | 两个端点都没启用时只让健康检查 unhealthy，进程照常启动，调用时才报未启用 |
| 重试按状态码 | 429 / 502 / 503 / 504 / 529 按「指数退避 + 全抖动」重试（次数由端点 `retries` 控制），其余 4xx / 5xx 不重试；退避用尽抛 `DecisionRetryError` |
| 并发批量要限流 | `predict_many` 并发跑多条独立提问，`limit` 限制同时在飞的请求数（搭配端点 `max_connections` 收窄连接池）；空入参早退、不发请求，任一题失败原样抛出 |

## 错误

错误全部继承 `DecisionError`（调用方一次 `except` 就能兜住整个判断层）。原始 `httpx2` 异常**只在超时 / 连接失败两类上**挂 `__cause__`，其余类型无 cause——对账信息已收成属性（`DecisionRequestError` 的 `status_code` / `endpoint` / `model` / `request_id`），按文档直接读属性即可：

| 类型 | 触发条件 | `__cause__` |
| --- | --- | --- |
| `DecisionConfigError` | 调用的端点未启用 | 无（没发起过调用） |
| `DecisionRequestError` | 端点返回 4xx / 5xx（带 `status_code` / `endpoint` / `model` / `request_id`） | 无（对账信息在四个属性上） |
| `DecisionRetryError` | 可重试状态码（429 / 502 / 503 / 504 / 529）退避重试后仍失败；`DecisionRequestError` 子类，对账坐标一致 | 无 |
| `DecisionTimeoutError` | 请求超时 | `httpx2.TimeoutException` |
| `DecisionConnectionError` | 连不上端点（超时除外），或其余请求错误 | `httpx2.TransportError` / `httpx2.RequestError` |
| `DecisionResponseError` | 返回体不合法：非 JSON（含非 UTF-8）/ 压缩体解不开 / 顶层不是对象 / 缺 answers / 答案字段不完整 | JSON 解析异常或 `httpx2.DecodingError`；其余无 |
