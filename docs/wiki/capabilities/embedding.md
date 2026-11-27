# 向量层

`EmbeddingService` 把「文本转向量」收成一个服务，业务代码不直接接触 SDK：

```python
from core.embedding import EmbeddingService


async def main(svc: EmbeddingService, texts: list[str]) -> None:
    # 单条
    vector = await svc.embed("要向量化的文本")

    # 批量：按 batch_size 切片、逐批串行请求，结果顺序与入参一致
    vectors = await svc.embed_many(texts)

    # 截断到指定维度：调用覆盖 > 配置 dimensions > 不传（用模型原始维度）
    short = await svc.embed("要向量化的文本", dimensions=512)
```

端点、模型与批量大小配置见[配置](../config.md)。

## 约定

| 约定 | 说明 |
| --- | --- |
| 原样透传、不归一化 | 返回的是模型原始向量，模长信息不丢；要不要 L2 归一化由调用方决定 |
| 空序列早退 | `embed_many([])` 返回 `[]`，不发请求、不查密钥（空输入不需要服务）；空串与全空白串在本地抛 `EmbeddingInputError` |
| `embed_many` 只收序列 | 传裸字符串（`str` 本身就是 `Sequence[str]`）会抛 `EmbeddingInputError`，否则会被静默按字符拆成 N 条向量；单个字符串请用 `embed()` |
| 缺密钥不 fail fast | 没配 `api_key` 时只警告、不建内核，健康检查 unhealthy，调用时才抛 `EmbeddingConfigError` |
| 重试交给 SDK | 超时与重试由端点的 `timeout` / `retries` 控制 |
| 不记正文 | 日志记调用开始（操作 / 模型 / 端点 / 文本数 / 目标维度，DEBUG）与每批完成（操作 / 文本数 / 维度 / 耗时 / 输入 token），失败记错误类型与状态码；文本正文不记 |

## 错误

错误全部继承 `EmbeddingError`，原始 SDK 异常挂在 `__cause__`：

| 类型 | 触发条件 |
| --- | --- |
| `EmbeddingConfigError` | 端点未配 `api_key` |
| `EmbeddingInputError` | 文本为空或全空白 |
| `EmbeddingRequestError` | 端点返回 4xx / 5xx（带 `status_code` / `endpoint` / `model` / `request_id`） |
| `EmbeddingTimeoutError` | 请求超时 |
| `EmbeddingConnectionError` | 连不上端点（超时除外） |
| `EmbeddingResponseError` | 返回体与请求对不上：条数不符 / `index` 越界或重复 / 空向量 / 声明维度不符 |
