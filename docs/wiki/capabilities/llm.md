# LLM 层

`LLMService` 把「调大模型」收成一个服务，业务代码不直接接触 SDK。五项能力：

```python
from pydantic import BaseModel

from core.llm import (
    LLMService,
    assistant,
    image_url,
    system,
    tool,
    tool_result,
    user,
    user_with_images,
)


class Item(BaseModel):
    name: str
    price: float


class WeatherArgs(BaseModel):
    city: str


async def main(svc: LLMService, picture_url: str) -> None:
    # 对话
    reply = await svc.chat([system("你是助手"), user("你好")])

    # 流式：逐段产出，不自动拼接
    parts = [part async for part in svc.stream([user("讲个故事")])]

    # 结构化输出
    item = await svc.chat_structured([user("苹果多少钱")], Item)

    # 工具调用（单轮）：回传与循环由调用方写
    messages = [user("上海天气")]
    call_reply = await svc.chat_tools(messages, [tool("get_weather", "查天气", WeatherArgs)])
    if call_reply.tool_calls:
        # 要第二轮就自己拼：assistant() 原样带回 tool_calls，tool_result() 回填结果
        call = call_reply.tool_calls[0]
        messages += [assistant(tool_calls=call_reply.tool_calls), tool_result(call.id, "晴 26℃")]
        final = await svc.chat(messages)

    # 多模态：不单独开方法，显式指定 vision 端点
    seen = await svc.chat([user_with_images("这是什么", image_url(picture_url))], endpoint="vision")
```

端点与密钥配置见[配置](../config.md)。

## 约定

| 约定 | 说明 |
| --- | --- |
| 不记正文 | 日志记调用开始（操作 / 模型 / 端点 / 消息数 / 采样参数，DEBUG）与完成（操作 / 模型 / 端点 / 耗时 / token），失败记错误类型与状态码；消息正文该不该记由调用方自己决定并自己打 |
| 不自动多轮 | `chat_tools` 只发一轮并返回 `tool_calls`，回传用 `assistant(tool_calls=...)` + `tool_result(...)`，循环由调用方写 |
| chat / vision 两端点 | 两个端点各自带连接与采样参数，可指向同一服务也可分开接；端点只按 `endpoint=` 显式选（多模态传 `"vision"`），不给就走 chat——不按模型名自动切换。用 Chat Completions 而非 Responses API，因为兼容端点普遍只实现前者 |
| 缺密钥不 fail fast | 端点没配 `api_key` 时只警告、不建客户端；两个端点全没配健康检查才 unhealthy，调用时才抛 `LLMConfigError` |
| 重试交给 SDK | 超时与重试由端点的 `timeout` / `retries` 控制；流式一旦开始消费就不再重试，断流是否重放由调用方决定 |
| `json_object` 要提示词配合 | 该模式要求提示词里出现 json 字样，属调用方责任 |

## 错误

错误全部继承 `LLMError`，原始 SDK 异常挂在 `__cause__`：

| 类型 | 触发条件 |
| --- | --- |
| `LLMConfigError` | 端点未配 `api_key` |
| `LLMRequestError` | 端点返回 4xx / 5xx（带 `status_code` / `endpoint` / `model` / `request_id`） |
| `LLMTimeoutError` | 请求超时 |
| `LLMConnectionError` | 连不上端点（超时除外） |
| `LLMSchemaError` | 结构化输出不符合给定 schema |
| `LLMResponseError` | 返回体缺内容（choices 为空、`content` 为 `None`） |
