# 07. Streaming 模块：main 实现、dev 对照与落地边界

## 1. 分析基线与范围

- `main`：`e317f7b8d9b2afb4c3925812d4774da602c9f8f3`
- `dev`：`8a91e95799c9b345d9540c7e201b33c603e7870c`
- main 演进区间：`3be3969f..e317f7b8`
- 范围：Agent frame 到 SSE 的发布、replay cursor、gap、namespace、terminal、持久化、lease authority、批处理与测试。
- Run 的进程拓扑和 Job settlement 见 Run/Worker 文档；本篇只分析流协议与存储。

## 2. 结论

main 与 dev 的 Streaming 有根本不同的权威模型：

```text
main:
  StreamBridge = bounded live/reconnect buffer
  RunEventStore = 另一条 durable message/task/audit journal
  二者不是逐帧同一事实

dev:
  PostgreSQL run_events(category="stream") = live + replay 的同一权威
  notifier 只降低延迟，提交失败前绝不通知
```

main 因 memory/Redis retained window 有界，明确产生 `StreamGap`，客户端应 reload durable state。
dev 先把每一帧提交到 PostgreSQL，再 best-effort notify；Gateway 按 thread-global BIGINT cursor
读表，因此 Worker 重启、Gateway 重启和通知丢失都不丢已提交 frame。

当前 dev 有三个可确认问题：

1. subgraph 的 marked LLM fallback 会参与 parent Run fallback 判断，缺少 main 的 root-only guard；
2. subagent persisted-event batch 写失败后被丢弃；
3. 缺少 main 的大文件 tool-argument batching，`write_file/str_replace` 大 payload 会产生高频 DB frame 和前端
   growing-JSON 重解析，形成明显卡顿风险。

## 3. main 源码地图

| 职责 | 路径 | 关键符号 |
| --- | --- | --- |
| bridge 协议 | `backend/packages/harness/deerflow/runtime/stream_bridge/base.py` | `StreamEvent`, `StreamGap`, `StreamBridge` |
| 内存 bridge | `backend/packages/harness/deerflow/runtime/stream_bridge/memory.py` | `MemoryStreamBridge` |
| Redis bridge | `backend/packages/harness/deerflow/runtime/stream_bridge/redis.py` | `RedisStreamBridge` |
| bridge 工厂 | `backend/packages/harness/deerflow/runtime/stream_bridge/__init__.py`, `backend/packages/harness/deerflow/runtime/stream_bridge/async_provider.py` | `make_stream_bridge()` |
| 配置 | `backend/packages/harness/deerflow/config/stream_bridge_config.py` | `StreamBridgeConfig` |
| graph frame 发布 | `backend/packages/harness/deerflow/runtime/runs/worker.py` | `_unpack_stream_item()`, `_publish_stream_item()` |
| 大文件批处理 | `backend/packages/harness/deerflow/runtime/runs/worker.py` | `_LargeFileToolChunkBatcher` |
| subagent event batch | `backend/packages/harness/deerflow/runtime/runs/worker.py` | `_SubagentEventBuffer` |
| Gateway SSE | `backend/app/gateway/services.py` | `sse_consumer()`, `wait_for_run_completion()` |
| durable journal | `backend/packages/harness/deerflow/runtime/events/store/` | `RunEventStore` 及 memory/jsonl/db |
| Run journal | `backend/packages/harness/deerflow/runtime/journal.py` | `RunJournal` |

## 4. main 协议与精确接口

```python
@dataclass(frozen=True)
class StreamEvent:
    id: str
    event: str
    data: Any

@dataclass(frozen=True)
class StreamGap:
    requested_event_id: str | None
    earliest_available_event_id: str
    latest_available_event_id: str
```

`StreamBridge`：

```python
async publish(run_id: str, event: str, data: Any) -> None
async publish_end(run_id: str) -> None
subscribe(run_id: str, *,
          last_event_id: str | None = None,
          heartbeat_interval: float = 15.0) -> AsyncIterator[StreamItem]
async cleanup(run_id: str, *, delay: float = 0) -> None
async close() -> None
```

sentinel：

- `HEARTBEAT_SENTINEL`
- `END_SENTINEL`
- `StreamGap`

配置：

```python
class StreamBridgeConfig(BaseModel):
    type: Literal["memory", "redis"] = "memory"
    redis_url: str | None = None
    queue_maxsize: int = 256
    max_connections: int | None = None
    stream_ttl_seconds: int = 86400
    recovered_stream_cleanup_delay_seconds: float = 60.0
```

memory 不支持跨进程；Redis `supports_cross_process=True`。

## 5. main 完整数据链

```text
agent.astream(...)
  -> worker._unpack_stream_item(item, modes, stream_subgraphs)
       得到 (mode, chunk, namespace)
  -> root-only fallback classification
  -> _publish_stream_item(...)
       namespace != ()：
         event = base_mode|ns1|ns2
         直接 bridge.publish
       namespace == ()：
         messages 可经大文件 batcher
         custom 可交给 subagent event buffer
         bridge.publish
  -> Memory/Redis retained stream
  -> Gateway sse_consumer()
       Last-Event-ID
       bridge.subscribe()
       StreamEvent -> id/event/data SSE
       heartbeat -> comment frame
       gap -> event: gap + recovery=reload_durable_state
       end -> event: end
  -> browser/SDK
```

`wait_for_run_completion()` 也消费 bridge，而不是直接 `await record.task`，因此 wait 与 SSE
共享 disconnect/cancel 和 terminal 语义。

## 6. main bridge 内部语义

### 6.1 `MemoryStreamBridge`

每个 Run 有：

```python
events: list[StreamEvent]
condition: asyncio.Condition
ended: bool
start_offset: int
```

event ID 是 `{millisecond_timestamp}-{per_run_seq}`。`seq` 等于该事件在 Run 中的绝对 offset；
通过 `seq - start_offset` 可 O(1) 定位 retained event，而非线性扫描。

发布超过 `queue_maxsize` 时删除头部并推进 `start_offset`。订阅者：

- cursor 命中 retained event => 从下一帧开始；
- cursor seq 已低于 watermark => `StreamGap` 后结束；
- live subscriber 消费速度落后到 watermark 前 => `StreamGap`；
- 未知/格式不符 cursor => 从最早 retained event 保守重放；
- 无数据等待 `Condition`，到期发 heartbeat；
- `ended` 且无剩余 frame => END。

### 6.2 `RedisStreamBridge`

- 每 Run 一个 Redis Stream key；
- publish 使用 exact `MAXLEN` 保持 bounded history；
- publish/publish_end 刷新 rolling TTL；
- Redis Stream ID 自带递增 cursor；
- subscriber 先用 transaction 同时读 retained earliest/latest 与 cursor 后 entries，避免 trim/read race；
- blocking `XREAD` 只作 wake-up，再回到 atomic snapshot 验证；
- transient Redis error 有界重试/backoff；
- cursor 落后 earliest => `StreamGap`；
- cleanup 删除 key，TTL 是 cleanup 未执行时的最终回收线。

这仍是有界 replay buffer，不是永久 durable event log。

## 7. root 与 subgraph frame

`_compose_sse_event(base, namespace)`：

```text
root:     messages
subgraph: messages|child_ns|nested_ns
```

SDK 按 `|` 解析 namespace。main 的 `_publish_stream_item()` 对 subgraph 做三项隔离：

1. namespace 保留在 event name，不能冒充 root `values/messages`；
2.绕过 root `_LargeFileToolChunkBatcher`；
3.绕过 root subagent lifecycle persistence；`task_*` lifecycle 本来就是 root custom frame。

fallback 判断同样只允许 root：

```python
if not namespace:
    llm_error_fallback_message = ...
```

child 的 fallback 应由 `SubagentExecutor` 映射成 `task_failed`，不能把 parent Run 设为 error。

## 8. 大文件与内部事件批处理

### 8.1 `_LargeFileToolChunkBatcher`

仅处理 root `messages` 模式中 `write_file` 和 `str_replace` 的 tool-call argument delta：

- identity = `(namespace, message_id, tool-call index/id)`；
- 普通 assistant text 和非文件工具仍逐 token；
- 工具名称未完整识别前不盲目 batch；
- 将非 tool payload 拆成可见 chunk；
- 只聚合 tool-only chunk；
- 默认每 32 个 delta 发布一次；
- 切换 identity/mode、stream finish 或 exception-finally 时 flush；
- 去掉会重复 growing payload 的 `additional_kwargs.function_call/tool_calls`。

这降低浏览器反复解析不断增长 JSON 的二次成本，也减少 bridge/Redis/网络 frame 数。

### 8.2 `_SubagentEventBuffer`

- live custom event 先正常发给 SSE；
- recognized task event 转为 bounded `subagent.start/step/end`；
- 每 25 条 `put_batch()`；
- terminal eager flush；
- run finally 再 flush；
- main 在 batch 失败时将失败 batch 放回新 pending 前面，保持原序，异常不打断 live stream。

## 9. main live stream 与 durable journal 的区别

main 的 `RunEventStore` 不是 StreamBridge 的持久化实现：

| 内容 | StreamBridge | RunEventStore |
| --- | --- | --- |
| token/values 原始 frame | 是 | 不保证逐帧 |
| reconnect | bounded Last-Event-ID | message/task/query API |
| retention | queue max / Redis TTL | store 自身策略 |
| subagent step | live custom | selected event 批量持久化 |
| RunJournal | callback 来源 | token/tool/message/delivery 等结构化事实 |

发生 `StreamGap` 时，Gateway 返回 `recovery="reload_durable_state"`，意思是客户端重新请求 thread state/
message/event API，而不是去 RunEventStore 找每一个丢失 token frame。

## 10. main 错误、断连与 terminal

- `on_disconnect=cancel`：仅本地 owned Run 的原始 SSE 断开触发 cancel；
- `on_disconnect=continue`：执行继续，断开的客户端不再消费；
- cross-worker `store_only` observation handle 断开不会凭空写新的 cancel；
- heartbeat 后若观察到 orphan recovery terminal，Gateway 发 end；
- terminal Run 但 bridge 已不存在时直接发 end；
- gap 不触发 disconnect cancel；
- publish/subscribe Redis error 超出重试预算后向上失败；
- `publish_end()` 与 delayed cleanup 给 reconnect client 留 drain 窗口。

## 11. main 测试

| 测试 | 覆盖 |
| --- | --- |
| `test_stream_bridge.py` | memory/Redis cursor、trim、gap、heartbeat、end、TTL、race |
| `test_gateway_services.py` | SSE gap payload、terminal missing、Last-Event-ID |
| `test_wait_disconnect_handling.py` | wait 与 SSE 相同 disconnect 语义 |
| `test_worker_stream_subgraph_namespace.py` | root/subgraph event name、fallback 隔离、root consumers |
| `test_worker_subagent_persistence.py` | event batch、terminal eager flush、failure re-buffer |
| `test_run_worker_rollback.py` | 大文件 batcher 的 identity、flush、异常路径 |
| `test_run_event_stream_contract.py` | RunEvent 类型/边界 |
| frontend `artifact-batched-stream.spec.ts` | 大文件 streaming UI 响应性 |
| frontend task/message unit tests | namespace 路由和 task card lifecycle |

## 12. main 关键提交的实现演进

| 提交 | 实际变化 |
| --- | --- |
| `4a2ecd43` / `f1632cc3` | task custom event 与持久化 event contract，给 Streaming 增加结构化内部事件 |
| `1cd5dea3` | Memory/Redis 增加 retained watermark 与 `StreamGap`；Gateway/前端显式 reload durable state |
| `5f0108f5` | 保留 subgraph namespace；child frame 绕过 root consumers；root-only fallback |
| `a38b1dae` | 加入 `_LargeFileToolChunkBatcher` 和前端 E2E，避免大文件 growing JSON 卡顿 |
| `18c32bea` | subagent `put_batch` 失败后 re-buffer，修复无声丢失 |
| `ad9ec65c` | memory bridge 增加 `stream_exists()`，terminal recovery 不再错误创建空 stream |
| `8be7411d` | durable SQLite event sequence 分配串行化；这是 journal 顺序修复，不等于 live bridge 持久化 |

## 13. dev 源码地图

| 职责 | 路径 | 关键符号 |
| --- | --- | --- |
| frame 类型 | `backend/packages/harness/deerflow/runtime/events/models.py` | `StreamFrame`, `StoredStreamFrame`, `StreamLeaseProof` |
| bridge 接口 | `backend/packages/harness/deerflow/runtime/events/stream_base.py` | `StreamBridge`, sentinels |
| PostgreSQL bridge | `backend/packages/harness/deerflow/runtime/events/stream.py` | `PostgresStreamBridge`, `parse_stream_cursor()` |
| durable store | `backend/packages/harness/deerflow/runtime/events/store/db.py` | `DbRunEventStore.append_stream_frame/list_stream_frames/ensure_settled_stream_terminal` |
| Worker lease adapter | `backend/app/reliability/execution.py` | `LeaseAuthorizedStreamBridge` |
| Gateway SSE | `backend/app/gateway/routers/private_work.py` | `_durable_private_sse_consumer()` |
| harness 发布 | `backend/packages/harness/deerflow/runtime/runs/worker.py` | `_stream_once()` |
| DB model | `backend/packages/harness/deerflow/persistence/models/run_event.py` | `RunEventRow`, thread seq |

## 14. dev 精确类型与接口

```python
@dataclass(frozen=True, slots=True)
class StreamLeaseProof:
    job_id: UUID
    lease_token: str  # repr=False

@dataclass(frozen=True, slots=True)
class StreamFrame:
    event: str
    data: Any
    category: str = "stream"
    terminal: bool = False

@dataclass(frozen=True, slots=True)
class StoredStreamFrame:
    id: str
    thread_id: str
    run_id: str
    event: str
    data: Any
    terminal: bool = False
    created: bool = True
```

event base 只允许 `[a-z][a-z0-9_.-]{0,31}`；namespace 最深 32、每段最长 256、完整 event name
最长 4096，拒绝 NUL/CR/LF。terminal 只能用 `event="end"` 和受控 status。

```python
parse_stream_cursor(value: str) -> int
```

只接受 canonical ASCII decimal（`0` 或无前导零正数），最大 PostgreSQL signed BIGINT。

`PostgresStreamBridge` 的关键接口：

```python
publish_frame(scope, thread_id, run_id, frame, *, lease=None)
publish_terminal(scope, thread_id, run_id, *, status, lease=None)
ensure_settled_terminal(scope, thread_id, run_id, *, status)
read_after(scope, thread_id, *, cursor, limit, run_id=None)
subscribe_scoped(scope, thread_id, run_id, *, last_event_id=None, ...)
```

无 scope 的 `publish/publish_end/subscribe/stream_exists` 主动抛 `StreamScopeRequired`。

## 15. dev store-first 完整调用链

```text
Worker run_agent()
  -> LeaseAuthorizedStreamBridge.publish()
       boundary.stream_lease_proof()
       PostgresStreamBridge.publish_frame(scope, thread, run, frame, lease)
         transaction
           DbRunEventStore.append_stream_frame()
             lock thread event sequence
             require scoped parent Run
             reject frame after terminal
             lock/revalidate Project + Membership
             lock Job + Run
             validate raw-token hash + both lease deadlines/statuses
             honor cancel markers
             assign thread-global seq
             INSERT run_events(category="stream")
         COMMIT
         best_effort notifier
  -> Gateway _durable_private_sse_consumer()
       parse Last-Event-ID
       read_after(scope, thread, cursor, run_id)
       yield committed frames
       empty时 0.25 秒 poll、15 秒 heartbeat
       Run settled 时 ensure_settled_terminal()
       yield exact durable terminal
```

通知失败只增加日志；读方继续轮询 PostgreSQL，所以通知不是正确性依赖。

## 16. dev 顺序、授权与 terminal

### 16.1 thread-global sequence

`_lock_event_sequence()`：

1. PostgreSQL transaction advisory lock；
2. `ThreadEventSequenceRow FOR UPDATE`；
3. legacy row 不存在时从 scoped thread 的 `max(run_events.seq)` 初始化；
4. `_advance_event_sequence()` 分配连续 BIGINT。

cursor 是 thread-global，不是 per-run counter；按 `run_id` 过滤时 seq 中允许出现其他 Run/event 的空洞，
但顺序仍单调。cursor 大于 high watermark => `StreamCursorOutOfRange`，Gateway 返回受控 invalid cursor。

### 16.2 lease 与 governance

job-owned Run：

- 无 lease 禁写；
- lease job_id、token hash、Job/Run status、双 lease deadline 全部匹配；
- project active、未 suspended；
- membership active、role 可执行、version 匹配；
- cancel marker 后禁止 data frame；
- terminal 自动改为 `interrupted`。

jobless Run 反而禁止携带 Job lease，避免把错误 capability 绑定到内部流。

### 16.3 terminal

- 存在 terminal 后再次 data frame => `StreamClosed`；
- 重复 terminal idempotently 返回旧 row；
- generic `put/put_batch` 禁止写 `category="stream"` 或 `event_type="stream.end"`；
- `ensure_settled_stream_terminal()` 只允许 Run，及存在时 Job，已经形成匹配 terminal pair；
- 缺失 terminal 时追加；
- 已有 terminal 内容与 authoritative settled status 不一致时修正同一 row；
- cleanup no-op，stream rows 只由 retention 删除。

Gateway 先排空当前 page，再依据 settled Run 修复 terminal，不会因一次空 poll 过早结束。
首次 POST 采用 cancel-on-disconnect 时持久化取消；reconnect GET 只观察，不凭断开创造取消。

## 17. main 与 dev 的精确差异

| 维度 | main | dev |
| --- | --- | --- |
| live 权威 | Memory/Redis bounded buffer | PostgreSQL stream rows |
| durable replay | 与 live bridge 分离，非逐帧 | 同一 row 同时服务 live/replay |
| cursor | per-run timestamp-seq / Redis ID | thread-global canonical BIGINT |
| gap | 显式 `StreamGap` | 已提交 rows 直接 durable 读取；cursor ahead 为 invalid |
| 多进程 | memory 否、Redis 是 | PostgreSQL 是 |
| 写授权 | run_id + bridge access | project/owner + exact Job raw-token lease |
| 取消 | bridge 外 RunManager | append transaction 内禁止 data/改 terminal |
| terminal repair | orphan/record 与 bridge end 协作 | settled Run/Job authoritative repair |
| retention | queue trim/Redis TTL/cleanup | 独立 retention policy，cleanup 不删 |
| 大文件 batch | 已有 32-delta batching | 缺失 |
| root fallback | 只检查 root namespace | 当前检查所有 namespace |
| failed event batch | re-buffer | 丢 batch |

## 18. 已确认缺陷与风险

### 18.1 dev root fallback 缺陷

在 dev
`backend/packages/harness/deerflow/runtime/runs/worker.py::_stream_once()` 的 multi-mode/subgraph
分支，代码在 `_unpack_stream_item()` 后无 `if not namespace` 就调用
`_extract_llm_error_fallback_message()`。因此：

```text
child subagent marked fallback
  -> parent worker 捕获 fallback
  -> parent Run 最终 error
```

main 已有回归测试和 root-only 实现。dev 的
`test_worker_subgraph_streaming.py` 当前只证明 namespace 能传输，没有覆盖 terminal 隔离。

### 18.2 dev batch 丢失

dev `_SubagentEventBuffer.flush()` 将 pending move 到局部 batch 后调用 `put_batch()`；异常只日志，
没有把 batch 放回。live task card 当时可能可见，但 reload 后 step history 永久缺失。

### 18.3 dev 大文件卡顿

dev 没有 `_LargeFileToolChunkBatcher`。大 `write_file/str_replace` 参数会：

1.每个 model delta 形成 frame；
2.每帧都走 lease + PostgreSQL transaction + sequence lock；
3.前端反复解析/合并不断增长的 tool-call JSON。

main 的代码、后端测试和 Chromium E2E 证明 batching 是针对该具体成本链的修复。移植时不能绕过
dev store-first；只应减少发布频率。

### 18.4 共同/运维风险

- namespace event 名是协议字段，base/segment/depth 限制改变需同步 SDK；
- notification 只能是 hint；若未来消费者只等 notify 会破坏当前正确性；
- terminal 与 Run/Job settlement 次序必须由 repair path 收敛，不能由 Gateway 猜测；
- retention 删除规则必须继续保护活跃/reconnect 窗口。

## 19. 可移植落点

1. 在 dev `runtime/runs/worker.py::_stream_once()` 加 root-only fallback：
   `if not namespace: ...`。
2.将 main `_publish_stream_item()` 的 namespace/root-consumer 分离思路适配到 dev，保留 event validator。
3.移植 `_LargeFileToolChunkBatcher`，但每次 batch 后仍调用
   `LeaseAuthorizedStreamBridge.publish()`；不能直接批量 INSERT 绕过 per-frame authority，除非另行设计
   同 transaction 的 lease-checked batch API。
4.移植 `_SubagentEventBuffer.flush()` re-buffer；event store 的 `scope` 必须原样保留。
5. model/token task payload 只扩展 JSON contract、validator/shared fixture/frontend reader，不加 SQL 列。
6.性能验证同时观察 DB transaction 数、event sequence lock wait、SSE first/last-byte latency 和浏览器 parse
   time。

## 20. 禁止直接合并

- 禁止把 main Memory/Redis bridge 作为 dev private Run 权威。
- 禁止先 notify 后 commit。
- 禁止绕过 `LeaseAuthorizedStreamBridge` 或 `append_stream_frame()`。
- 禁止用进程本地 counter 生成 dev cursor。
- 禁止让 generic `RunEventStore.put/put_batch` 写 reserved stream event。
- 禁止在 reconnect GET 断开时自动取消 Run。
- 禁止用 main gap/cleanup 语义删除 dev durable rows。
- 禁止把 subgraph event 改成 bare root name。
- 禁止由 child fallback terminalize parent Run。
- 禁止为降低卡顿而丢弃 tool payload；应有界聚合并在 finish/error 时 flush。

## 21. 建议测试矩阵

| 场景 | 期望 |
| --- | --- |
| cursor | 0、正常、前导零、负数、Unicode digit、BIGINT overflow、ahead watermark |
| scope | 跨 project/owner/thread/run 均不可见，外部错误不泄露存在性 |
| sequence | 多 Worker 并发 append 严格单调唯一；generic/stream 共用 high watermark |
| lease | 缺失、错误 job、错误 token、expired、Run/Job 任一非 running 均拒绝 |
| governance | membership version/role、project frozen/suspended/deleted |
| cancel race | data frame 与 cancel 同事务竞争；cancel 后 terminal 只能 interrupted |
| terminal | 重复 terminal idempotent、data-after-terminal 拒绝、settled repair |
| restart | Worker commit 后崩溃、notify 丢失、Gateway 重启，frame 仍可 replay |
| disconnect | 首次 POST cancel；reconnect GET continue；CancelledError 同样持久化意图 |
| namespace | root 与 1/32/33 层 child、非法 CR/LF/NUL、SDK 路由 |
| fallback | child marked fallback 只 task_failed；root marked fallback 才 parent error |
| 大文件 | 普通文本逐 token；文件 args 每 32 delta；identity/mode/error/final flush |
| batch retry | subagent batch 首次失败，下一次按原序持久化且无重复 terminal |
| retention | 活跃 Run 不删；过期数据按 project policy 删除；cursor 行为受控 |
| 容量 | 高并发 frame、sequence lock wait、DB pool、SSE p95、浏览器长任务响应 |
