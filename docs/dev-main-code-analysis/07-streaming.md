# 07. Streaming 模块：main 实现、dev 对照与实际落地

## 1. 分析基线与范围

- `main`：`e317f7b8d9b2afb4c3925812d4774da602c9f8f3`
- `dev`：`8a91e95799c9b345d9540c7e201b33c603e7870c`
- main 演进区间：`3be3969f..e317f7b8`
- 范围：Agent frame 到 SSE 的发布、replay cursor、gap、namespace、terminal、持久化、lease authority、批处理与测试。
- Run 的进程拓扑和 Job settlement 见 Run/Worker 文档；本篇只分析流协议与存储。
- 上述 commit 是制定移植计划时的只读基线；第 22 节起记录 2026-07-30 在 `dev`
  工作树完成的实际移植、真实模型测试和数据库证据。

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

迁移前 dev 有三个可确认问题：

1. subgraph 的 marked LLM fallback 会参与 parent Run fallback 判断，缺少 main 的 root-only guard；
2. subagent persisted-event batch 写失败后被丢弃；
3. 缺少 main 的大文件 tool-argument batching，`write_file/str_replace` 大 payload 会产生高频 DB frame 和前端
   growing-JSON 重解析，形成明显卡顿风险。

本轮已按 dev 的 PostgreSQL 权威模型完成定向移植，没有引入 main 的 Memory/Redis bridge：

- root/subgraph consumer 隔离和 root-only fallback 已落地；
- subagent batch 失败/取消 re-buffer 已落地；
- 大文件 32-delta batching 及 DeepSeek transport-noise 兼容已落地；
- `run_events.id/seq`、前端 cursor 全链路升级为 signed BIGINT 安全表示；
- 前端重连改为新 projection 从 cursor `0` 完整重建，旧 client 有明确生命周期和 abort 边界；
- 真实浏览器既验证执行中的双 Subagent 离页/重连，也验证 Run 在页面外先终态后再进入的完整重放；
- 长会话压缩真实暴露并修复 SDK 稀疏消息数组被对象展开提前执行 getter 而导致的页面崩溃。

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
| 大文件 batch | 已有 32-delta batching | 迁移前缺失；现已适配 store-first 发布 |
| root fallback | 只检查 root namespace | 迁移前检查所有 namespace；现已 root-only |
| failed event batch | re-buffer | 迁移前丢 batch；现按原序 re-buffer |

## 18. 迁移前已确认缺陷与风险

### 18.1 dev root fallback 缺陷（已修复）

迁移前 dev
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

### 18.2 dev batch 丢失（已修复）

迁移前 dev `_SubagentEventBuffer.flush()` 将 pending move 到局部 batch 后调用 `put_batch()`；异常只日志，
没有把 batch 放回。live task card 当时可能可见，但 reload 后 step history 永久缺失。

### 18.3 dev 大文件卡顿（已修复并做真实模型压力验证）

迁移前 dev 没有 `_LargeFileToolChunkBatcher`。大 `write_file/str_replace` 参数会：

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

## 19. 可移植落点与执行状态

1. **已完成**：在 dev `runtime/runs/worker.py::_stream_once()` 加 root-only fallback：
   `if not namespace: ...`。
2. **已完成**：将 main `_publish_stream_item()` 的 namespace/root-consumer 分离思路适配到 dev，保留 event validator。
3. **已完成**：移植 `_LargeFileToolChunkBatcher`，但每次 batch 后仍调用
   `LeaseAuthorizedStreamBridge.publish()`；不能直接批量 INSERT 绕过 per-frame authority，除非另行设计
   同 transaction 的 lease-checked batch API。
4. **已完成**：移植 `_SubagentEventBuffer.flush()` re-buffer；event store 的 `scope` 原样保留。
5. **已完成**：model/token task payload 只扩展 JSON contract、validator/shared fixture/frontend reader，不加 SQL 列。
6. **已完成主要验收**：用真实模型比较持久化 frame 数、验证执行中重连、唯一终态和浏览器恢复；
   production 级 p95 与数据库 lock-wait 压测仍属于部署环境容量验收，不伪装成本机功能测试。

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

## 22. 实际移植内容

### 22.1 Worker 根图/子图发布边界

`runtime/runs/worker.py` 新增统一 `_publish_stream_item()`：

- subgraph frame 保留 `mode|namespace...` 名称后直接走原 `StreamBridge`；
- child frame 不进入 root 的文件工具 batcher；
- child `custom` 不进入 parent `_SubagentEventBuffer`；
- `_extract_llm_error_fallback_message()` 只在 `namespace == ()` 时参与 parent terminal 判断；
- 所有实际发布仍经过注入的 `LeaseAuthorizedStreamBridge`，因此没有绕过 lease、transaction、
  commit-before-notify 或 project/owner scope。

前端不再请求 `streamSubgraphs: true`，`onCustomEvent` 也增加 root callback 判断。子任务 UI 只消费根图
`task_*` lifecycle 与持久化 `subagent.*`，避免 child 的原始 LLM/tool frame 混入 Lead 对话。

### 22.2 大文件工具参数 batcher

新增 `_LargeFileToolChunkBatcher`，精确行为如下：

1. 只处理 root `messages` 中 `write_file`、`str_replace`；
2. identity 为 `namespace + message_id + tool-call index/id`；
3. 工具名分片未完整匹配前仍逐帧发布；
4. 普通 assistant content、usage 和有意义 metadata 与工具参数分离后继续实时发布；
5. `function_call/tool_calls` growing payload 从可见副本移除；
6. tool-only delta 每 32 个合并发布；
7. identity 变化、非 message mode、`values`、正常结束和异常 finally 都会 flush；
8. `values` 表示工具参数边界结束，会同时清理 identity；
9. DeepSeek 插入的纯 `response_metadata.model_provider` transport chunk 不再冲刷 pending batch；
10. provider metadata 被视为传输信息而不是独立 UI 内容，但其他 response metadata 仍保留。

真实模型 C/C2 对照中，C2 做了更多 LLM 调用、生成更多 Tokens，`stream/messages` 仍从 9,840 降到
2,265，减少约 76.98%。

### 22.3 Subagent persistence 重试

`_SubagentEventBuffer.flush()` 现在：

- 普通异常：`batch + 新 pending` 放回队首，保留原始顺序；
- `CancelledError`：同样先放回，再重新抛出取消；
- terminal 仍 eager flush；
- scope 保持为服务端下发的 private scope；
- live stream 失败不被持久化辅助路径反向打断。

### 22.4 terminal replay

Gateway 在 Run 已 settled 时取得或修复 durable terminal 后，只有
`terminal_cursor > request_cursor` 才发送该 terminal。若客户端 cursor 已经精确等于 terminal：

```text
HTTP 200
response body empty
不重复 stream.end
```

同一 Thread 后续产生新 Run 时，旧 Run 的精确 terminal cursor 也不会错误重放或串入新 Run。

### 22.5 BIGINT schema 与 cursor

完整 schema 把：

```text
run_events.id:  SERIAL  -> BIGSERIAL
run_events.seq: INTEGER -> BIGINT
```

ORM 同步使用 `BigInteger`，catalog schema digest 同步更新。仓库坚持 single-full-schema 生命周期，
所以这不是在线 migration：旧库不能原地 stamp 或 ALTER 后继续使用，必须按项目规则创建空库并执行
`make setup-db`。

前端 durable SSE cursor 不再转成 JavaScript `number`，而是：

- canonical non-negative decimal string；
- 长度优先、字典序次之的十进制比较；
- 上限为 `9223372036854775807`；
- 兼容读取 sessionStorage 中既有的 safe-integer number；
- 落盘 cursor 只能单调前进，旧异步 consumer 不能回写更小值。

同一规则已扩展到四个非 SSE private-work feed：

| Feed | 序号与分页 cursor |
| --- | --- |
| `GET /threads/{thread_id}/runs/{run_id}/messages` | `seq`、`before_seq`、`after_seq` 使用 canonical decimal string |
| `GET /threads/{thread_id}/messages` | `seq`、`before_seq`、`after_seq` 使用 canonical decimal string |
| `GET /threads/{thread_id}/runs/{run_id}/events` | `seq`、`after_seq` 使用 canonical decimal string |
| `GET /threads/{thread_id}/events` | `seq`、`after_seq` 使用 canonical decimal string |

Gateway 不再把这些响应的 `seq` 编码成 JSON number，并将查询 cursor 限制在 PostgreSQL signed
BIGINT 范围。前端 Thread 历史和 Subtask 历史把 cursor 保持为字符串，按十进制长度和值比较；
numeric、前导零、非十进制和越界响应都会 fail closed。因此相邻的
`9007199254740992`、`9007199254740993` 不会在 `JSON.parse` 后合并成同一个 JavaScript number。

`/api/privacy/cases/{project_id}/export` 的 privacy-center NDJSON 原始附件不属于上述
Thread/task feed；其中 event `seq` 目前仍是 JSON number，是明确保留的域外残项。

### 22.6 前端 client 生命周期与重连

每个 `account + project` client entry 现在包含 `active + AbortController`：

- scope dispose 会先置 inactive 再 abort 所有 adapter 请求；
- 旧 generation 即使迟到也不能 yield frame 或推进 cursor；
- caller signal 与 scope signal 合并；
- reconnect metadata 的删除是 compare-and-remove：旧 consumer 只能删除自己观察到的 Run ID；
- scope 释放不再无条件清空整个 reconnect key，避免销毁新 generation 的状态；
- React cleanup 使用 deferred local detach，Strict Mode 的紧邻 remount 可以 retain；
- local detach 调用 `switchThread(null)`，只清理/中断本地 projection，绝不调用后端 stop/cancel。

新挂载的 `joinStream()` 固定从 `lastEventId="0"` 重建当前 Run。共享 session cursor 只作为诊断和
单调去重证据，不能代表新 UI 已经渲染过旧 consumer 消费的 frame。这解决了路由卸载后只接续 tail
导致 Human message、task card 或早期 token 缺失的问题。

项目持久流还显式区别四类结束路径：

- durable project client 跳过 legacy terminal preflight，已终态 Run 仍进入持久流重放；
- 已收到至少一帧但 EOF 前没有 durable terminal 时抛 `PROJECT_STREAM_INCOMPLETE`，保留重连键；
- Worker 的 `raw error -> durable stream.end(error)` 中 raw error 只作诊断，SDK 等到权威 terminal
  后仅呈现一次失败；
- terminal 只 compare-delete 自己观察到的 Run ID，旧 Run 不能删除后续新 Run 的重连键。

长会话 summarization 还暴露了 SDK handle 的投影边界。SDK 的 `messages` 可在
`RemoveMessage(__remove_all__)` 与 message-tuple 索引重建之间短暂呈稀疏数组；`toolCalls` 是可枚举
getter。原 `{...thread}` 会在 React render 时无条件执行该 getter，即使页面没有读取 `toolCalls`，
并在空槽上触发 `undefined.type`。`overlayThreadProjection()` 现在复制属性描述符以保留 getter 惰性，
只把已经压实/合并的 `messages`、受控 `values` 与本地 `stop` 覆盖为 data descriptor。

### 22.7 Artifact URL

`buildWriteFileArtifactURL()` 使用 `URL` 与 `URLSearchParams` 构造 `write-file:` URL，并先保护路径中的
百分号。`message_id`、`tool_call_id` 不再通过字符串拼接进入 query，避免 `&`、`#`、`%` 破坏
artifact identity。

## 23. 自动化测试结果

### 23.1 Backend 聚焦测试

```text
test_worker_stream_batching.py
  - 普通文本/非文件工具不 batch
  - 分片工具名、identity 切换、finish/error flush
  - namespace 绕过 root batcher
  - DeepSeek provider transport-noise

test_worker_subgraph_streaming.py
  - root/subgraph namespace
  - child fallback 不 terminalize parent

test_worker_subagent_persistence.py
  - 失败与 CancelledError re-buffer

test_m6_durable_stream_postgres.py
  - BIGSERIAL/BIGINT schema
  - signed BIGINT 高位 cursor
  - terminal 唯一性和 durable sequence

test_m6_gateway_reconnect_process.py
  - 精确 terminal cursor 返回空 200
  - 同 Thread 后续 Run 不重放旧 terminal
```

本轮已取得：

```text
worker batcher + Gateway/Scheduler import boundary：19 passed
真实 Scheduler readiness + M7 process boundary：2 passed, 0 skipped
Streaming 聚焦 PostgreSQL 组：54 passed
固定 release-gate 文件中的新增用例：13 passed, 0 skipped
```

### 23.2 Frontend

新增或扩展：

- canonical BIGINT cursor、overflow/Unicode/前导零拒绝；
- monotonic diagnostic cursor；
- fresh join 从 `0` 完整重建；
- stale client abort 与 inactive frame suppression；
- reconnect compare-and-remove；
- deferred local detach；
- root-only custom callback；
- artifact URL 特殊字符；
- terminal 去重和 route remount replay。
- durable completed-Run replay、started-without-terminal EOF、raw-error 后等待 durable terminal；
- SDK enumerable getter / sparse compaction projection 回归。

阶段性结果与最终结果：

```text
REST BIGINT 相关定向：76 passed
重连生命周期相关定向：50 passed
消息合并/压缩投影定向：68 passed
pnpm check：通过
pnpm test：178 files, 1272 passed, 0 failed, 0 skipped
```

### 23.3 完整 PostgreSQL gate 的发现与闭环

第一次复跑固定 M1–M7 20 文件门禁：

```text
266 collected
253 passed
13 failed
0 skipped
```

其中两项真实进程边界问题已定位并修复，且已在真实 PostgreSQL 上复跑为 `2 passed, 0 skipped`：

1. Scheduler advisory-lock readiness 查询未限定当前 database OID，可能把同实例其他测试库的同 key
   lock 误认为本库 ownership；
2. M7 real-process 测试调用 `_create_project_thread()` 时未适配新增的 `session_factory` 参数。

Gateway/Scheduler import graph 还发现 `deerflow.subagents.__getattr__` 内静态函数级 import 会被 AST
门禁视为可达 Worker executor；现改成真正的 `import_module()` lazy export，并通过精确上下文 allowlist
约束。

其余首次失败也没有跳过：

1. 5 个 `skills/public` 源目录曾被误删，但 packaged catalog、签名 archive、生成器和文档仍明确承诺
   21 个系统 Skill；从 `main` 恢复 11 个逐字节匹配 archive 的审查/再生成源文件；
2. MCP 历史 definition 使用 `MappingProxyType`，JSON 深拷贝需先转换顶层 `dict`；
3. 多 Credential slot 的创建顺序与 repository 历史读取排序不同，checksum 复验会自撞；验证后按
   `slot.name` canonicalize；
4. source-absence gate 不再扫描 AGENTS 约定排除的历史迁移分析文档，但 runtime roots 仍保持原门禁。

最终使用真实 PostgreSQL、随机 `deerflow_test_*` 临时库重跑固定 20 文件：

```text
266 collected
266 passed
0 failed
0 skipped
M1-M7 release stats: collected=266 passed=266 failed=0 skipped=0
```

### 23.4 Backend 全量回归

第一次全量回归还暴露了一个与事件高压执行相关的真实并发缺陷：
`PrivateWorkContext` 弱引用登记表在持有普通 `Lock` 时可能触发 GC；同一线程执行弱引用清理回调后再次
获取该锁，形成自死锁。登记表改为 `RLock`，并新增“弱引用回调同线程重入”回归测试。修复后全量套件
不再停在原位置。

随后出现的 16 个失败均发生在业务断言前，经逐项复现确认是旧测试夹具未补齐
`checkpoint_channel_mode` / `checkpoint_delta.snapshot_frequency`、伪 Checkpointer 未满足新的
fail-closed 类型边界，以及旧工具断言漏掉按设计新增的 `memory_search`。这些测试契约已同步，六组定向
复跑为 `74 passed, 1 skipped`。

最终当前工作树的完整结果：

```text
uv run pytest -q
7358 passed, 977 skipped, 0 failed

ruff format --check
1119 files already formatted

ruff check
All checks passed
```

## 24. 真实浏览器与模型验收

使用隔离空数据库、真实 Gateway/Worker/Scheduler/Frontend/Nginx、真实 DeepSeek 模型完成 10 个 Run：

| 轮次 | 真实场景 | 结果 |
| --- | --- | --- |
| A | 10 节长文、正式输出文件、首尾标记 | 成功，3 次 LLM |
| B | 不用工具回忆 A 的标记、文件和指定小节 | 成功，1 次 LLM |
| C | 单次 `write_file` 生成 16 KB 文件，但模型漏正式文件呈现 | 受控失败，Job dead，无自动副作用重试 |
| C2 | 修复 batcher 后重做大文件，并正式呈现文件 | 成功，8 次 LLM |
| D | 两个独立 Subagent + Lead 合并表 | 成功，两个 lifecycle card |
| E | 两个 Subagent 运行中离页约 8 秒，再进同一 Thread | 成功，继续执行且唯一终态 |
| F | 重连后不用工具回忆 E 的完整上下文 | 成功，1 次 LLM |
| G | 两个 Subagent 运行中离页，等数据库确认 Run 在页面外成功终态后才返回 | 成功，约 2 秒完整重放，2 次 LLM |
| H | 对 G 做无工具上下文续问，并触发长会话压缩 | 后端成功，3 次 LLM；真实暴露 SDK 稀疏投影崩溃并据此修复 |
| I | 修复后在同一已压缩会话再次实时续问 | 成功，无页面崩溃，2 次 LLM |

总计 29 次真实模型调用，输入 570,821 Tokens、输出 55,759 Tokens、合计 626,580 Tokens。

数据库交叉核验：

```text
run_events rows           = 34043
distinct seq              = 34043
seq range                 = 1..34043
sequence gaps             = 0
stream.end                = 10（每 Run 恰好 1）
每个 Run 最大 seq          = 该 Run 的 stream.end
```

完整 Run ID、每轮 Tokens、事件区间、文件大小、Subagent 生命周期和截图索引见
[evidence/07-streaming/database-evidence.md](evidence/07-streaming/database-evidence.md)。

## 25. 真实失败如何处理

Round C 没有被掩盖成“通过”：

- 文件确实写入并最终成为 ready；
- 模型没有走正式文件呈现协议；
- output delivery guard 将 Run 置为 error；
- 已有写副作用使 retry safety 变为 unknown；
- Job 进入 dead，避免重复 `write_file`；
- 仍只持久化一个 `stream.end`。

该轮同时暴露 transport-noise 会破坏大文件 batch 的真实 provider 行为。修复后使用新的 Run C2 验证，
没有修改 C 的历史终态，也没有手工把失败记录改成 success。

## 26. 剩余边界

1. 本机真实浏览器证明功能正确性，不等于生产容量认证；DB pool、sequence advisory-lock wait、
   SSE p95 和 retention 大规模删除仍应在目标部署环境压测。
2. full schema 已升级 BIGINT，但仓库明确不支持旧数据库原地升级；部署前必须用空库
   `make setup-db`。
3. durable SSE 与四个 private-work 历史 message/event feed 已避开 JS `number`；privacy-center
   NDJSON 原始附件仍将 event `seq` 输出为 JSON number。它不进入 Thread/task 客户端，但若未来由
   JavaScript 解析或作为分页 cursor 使用，必须单独版本化为 canonical decimal string。
4. PostgreSQL durable rows 没有 main bridge 的 bounded gap；retention policy 若将来删除可重连窗口内
   frame，必须同步定义明确的 stale-cursor 响应，不能静默从错误位置继续。

## 27. 2026-07-30 REST cursor 回归复验

09 Gateway/API 的前置审计发现：四个非 SSE feed 虽然已经把响应 `seq` 序列化为 canonical decimal
string，但请求参数仍由 FastAPI 先按 `int` 解析。这会把 `01`、`+1`、`1.0`、前导空格和 `-0`
宽松转换为整数，OpenAPI 也错误暴露为 `integer`，与本模块第 22.5 节的契约不一致。按“05–07
无阻塞才进入 08–11”的顺序要求，09 已暂停，本回归先完成闭环。

最终落点：

- Run messages、Thread messages、Run events、Thread events 的 cursor 查询参数保持原始
  `string | null`；
- Gateway 共享调用 SSE 已使用的 `parse_stream_cursor()`，通过后才把 `int` 传给内部
  repository/service；
- 非 canonical ASCII decimal、Unicode 数字、空串、超长数字和 signed BIGINT 越界统一返回稳定
  `422 PRIVATE_WORK_INVALID`；
- Thread messages 与 Run messages 一致，拒绝同时提供 `before_seq` 和 `after_seq`；
- OpenAPI 四类 feed 的 cursor 类型均为 `string | null`。

真实 PostgreSQL 零跳过复跑：

```text
uv run pytest tests/test_private_work_feed_router.py -q
23 passed, 0 skipped
```

浏览器使用已登录会话从同源页面真实调用四类 API，覆盖 canonical `0`、signed BIGINT 最大值、
前导零、显式正号、小数、Unicode 数字、越界值和双 cursor 冲突：

```text
11/11 browser requests passed
合法 cursor：200
非法或冲突 cursor：422 PRIVATE_WORK_INVALID
```

截图证据：
[09-rest-canonical-cursor-browser.png](evidence/07-streaming/09-rest-canonical-cursor-browser.png)。
