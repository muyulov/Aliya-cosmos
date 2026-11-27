# 部署

## 1. 部署形态

| 形态 | 适用 | 差异点 |
| --- | --- | --- |
| 同进程（默认） | 单机部署、开发调试 | `MessageBus` / `EventBus` 用 asyncio 队列；时间戳统一 `ClockService`；无序列化开销 |
| 独立进程（Aliya） | Aliya 独立迭代、崩溃隔离 | 控制面走 Unix 域套接字 + NDJSON；首行握手，之后请求-响应、入站与事件共用一条连接 |
| 分进程 / 跨机 | 多客户端、远程部署 | 换成 IPC / WebSocket 适配器；所有契约必须可 JSON 序列化；事件需要重连与补偿 |

设计要求：**契约模型一律可 JSON 序列化**，不使用只有同进程能传的对象（future、生成器、闭包）。这样换传输层时不用改契约。

传输层是端口（`MessageBus` / `EventBus`），不是组件：`TerminalService` 只依赖端口，不知道背后是队列还是 WebSocket。

### 1.1 为什么端口要由一个 Service 承载

端口是接口，接口本身没有生命周期，但进程内实现有：队列要创建、要关闭、要取消订阅者。因此默认实现交给 `BusService` 托管——它只做三件事：

| 职责 | 说明 |
| --- | --- |
| 持有实现 | 内部构造 `MemoryMessageBus` / `MemoryEventBus` |
| 暴露端口 | `bus.messages` / `bus.events` 返回 `MessageBusPort` / `EventBusPort` |
| 管理生命周期 | `start()` 初始化、`stop()` 关闭所有队列并唤醒等待者 |

其他服务把 `BusService` 当普通依赖注入，再通过属性拿端口；换 WebSocket / IPC 时只换 `BusService` 内部的实现，服务代码与端口签名不动。

`UserChannelPort` / `WorldReasoningPort` 这类部署方端口不走容器注入（容器只认 Service 与配置节点），而是用 setter 注入：

```python
terminal.set_user_channel(channel)
world.set_port(port)
```

内置的 `WebSocketChannelService` 在自己的 `start()` 里注入自己，停止时把通道交还给内存兜底；CLI 则在 `lifespan()` 之后接管，因此 CLI 与 WebSocket 通道只能二选一。

链路状态这类「反向读取」也用 setter 解耦：`assembly` 把 `TerminalService.link_state` 注册给 `WorldBridgeService`，两个服务的构造器因此没有循环依赖。

## 2. 分进程形态的必答题

同进程下可以不做，跨进程必须处理：

- 断线重连后的事件补偿：按 `event_id` 与 `sequence` 拉取缺口。
- 投递语义：默认至少一次（at-least-once），消费方按 `event_id` 去重。
- 时钟偏差：以 cosmos 侧时间戳为准，Aliya 侧不自行生成排序依据。

## 3. 本地依赖数据库

`docker/docker-compose.yml` 编排两个本地数据库，供记忆层使用：

```bash
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml down
```

| 服务 | 镜像 | 端口 | 数据目录 |
| --- | --- | --- | --- |
| `qdrant` | `qdrant/qdrant:latest` | 6333（REST）/ 6334（gRPC） | `data/memory/qdrant` |
| `tigergraph` | `tigergraph/community:4.2.2` | 9000（REST++）/ 14240（GraphStudio）/ 14022（SSH） | `data/memory/tigergraph` |

端口与密钥可在 `docker/.env` 覆盖，参考 `docker/.env.example`；两个库的地址要与 `data/config/cosmos.yaml` 的 `memory` 段保持一致。

!!! warning "与当前代码状态不符"
    compose 文件与注释指向 `core/memory`，但当前 `core/` 下**没有** `memory` 模块，`cosmos.yaml` 也没有 `memory` 配置段。这套编排属于为记忆层预留的基础设施，尚未接入代码。启动主流程不需要它。

## 4. 生产注意事项

| 项 | 建议 |
| --- | --- |
| 镜像版本 | 生产环境锁定具体 tag，不要用 `latest` |
| 鉴权 | Qdrant 默认无鉴权，只监听回环或内网；开启后 `QDRANT_API_KEY` 与 `cosmos.yaml` 保持同值 |
| 数据落盘 | `data/memory/` 已在 `.gitignore` 中忽略，需单独备份 |
| 控制面令牌 | `ALIYA_CONTROL_TOKEN` 只从进程环境变量读，`.env` 不会注入 `os.environ`；服务端与客户端必须同值 |
| 日志 | 按天轮转，默认保留 7 天；长期采集请另接 sink |
