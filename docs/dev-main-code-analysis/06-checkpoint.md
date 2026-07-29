# 06. Checkpoint 模块：表示、授权与 dev 落地边界

## 1. 分析基线与范围

- `main`：`e317f7b8d9b2afb4c3925812d4774da602c9f8f3`
- `dev`：`8a91e95799c9b345d9540c7e201b33c603e7870c`
- main 演进区间：`3be3969f..e317f7b8`
- 范围：checkpoint channel 表示、materialization、兼容门、state mutation、第三方补丁，以及 dev 的 project-scoped saver 授权包装。

本篇必须先固定两个正交概念：

```text
main full/delta       = 状态如何编码、重放和物化
dev scoped checkpointer = 谁可以读写哪一个 project/owner/thread
```

表示优化不能替代授权；授权 wrapper 也不自动提供 delta 表示。

## 2. 结论

main 在传统 full checkpoint 外新增了 `DeltaChannel` 模式，目的在于减少随消息历史增长而重复写入的
checkpoint 体积。它通过 process-frozen mode/cadence、metadata marker、materialized graph accessor
和 fail-closed compatibility gate，支持 **full -> delta** 单向演进；delta thread 被 full process
打开时必须拒绝。

dev 当前没有 delta 表示，但已经有更重要的 SaaS 安全边界：
`ProjectScopedCheckpointer` 将 raw LangGraph saver 包在
`PrivateResourceScope(project_id, owner_user_id)`、membership revalidation、Thread row lock 和
checkpoint metadata marker 内。任何 main delta 能力只能装在该 wrapper 之下，并同时覆盖 Gateway
chat-control 与独立 Worker；绝不能用 main 的 raw accessor 替换 scoped saver。

## 3. main 源码地图

| 职责 | 路径 | 关键符号 |
| --- | --- | --- |
| 配置 | `backend/packages/harness/deerflow/config/database_config.py` | `CheckpointChannelMode`, `CheckpointDeltaConfig` |
| mode/cadence 门 | `backend/packages/harness/deerflow/runtime/checkpoint_mode.py` | freeze/inject/compatibility functions |
| materialized accessor | `backend/packages/harness/deerflow/runtime/checkpoint_state.py` | `CheckpointStateAccessor`, `build_state_mutation_graph()` |
| state/reducer | `backend/packages/harness/deerflow/agents/thread_state.py` | `DeltaThreadState`, `merge_message_writes()` |
| LangGraph 补丁 | `backend/packages/harness/deerflow/checkpoint_patches.py` | 两个 `ensure_*_patch()` |
| graph 构造 | `backend/packages/harness/deerflow/agents/lead_agent/agent.py` | mode/cadence freeze 和 schema 选择 |
| 通用 agent 工厂 | `backend/packages/harness/deerflow/agents/factory.py` | `create_deerflow_agent()` delta guard |
| Gateway state API | `backend/app/gateway/services.py` 与 routers | accessor 构造、history/update/branch |
| Run rollback/resume | `backend/packages/harness/deerflow/runtime/runs/worker.py` | rollback point、linear delta resume |

## 4. main 配置与进程约束

```python
CheckpointChannelMode = Literal["full", "delta"]

class CheckpointDeltaConfig(BaseModel):
    snapshot_frequency: int = 10  # >= 1

class DatabaseConfig(BaseModel):
    checkpoint_channel_mode: CheckpointChannelMode = "full"
    checkpoint_delta: CheckpointDeltaConfig
```

mode 与 `snapshot_frequency` 都是 restart-required：

```python
freeze_checkpoint_channel_mode(mode)
freeze_checkpoint_snapshot_frequency(snapshot_frequency)
```

同一进程第二次请求不同值会抛 `CheckpointModeReconfigurationError`。共享同一 checkpoint
数据库的所有进程也必须配置一致；进程内 freeze 无法自动发现另一个进程的 cadence 配置。

重要细节：

- metadata 只写 `deerflow_checkpoint_channel_mode="delta"`；
- cadence 不写 metadata，因为 cadence已编译进 graph channel table；
- absence of marker 表示 legacy/full；
- 旧版 `counters_since_delta_snapshot.messages` 也被识别为 delta；
- 旧 flat config `checkpoint_delta_snapshot_frequency` 自动迁到
  `checkpoint_delta.snapshot_frequency`，nested explicit value 优先。

## 5. main 精确接口

### 5.1 mode gate

```python
inject_checkpoint_mode(config: dict[str, Any], mode) -> None
checkpoint_metadata_uses_delta(metadata: Any) -> bool
checkpoint_tuple_uses_delta(checkpoint_tuple: Any) -> bool
state_snapshot_uses_delta(snapshot: Any) -> bool
raise_if_snapshot_incompatible(snapshot, mode) -> None
ensure_checkpoint_mode_compatible(checkpointer, config, mode) -> None
async aensure_checkpoint_mode_compatible(checkpointer, config, mode) -> None
```

- 读路径先由 graph 物化，再对 `StateSnapshot.metadata` 检查；
- 写路径必须提前 `get_tuple/aget_tuple` 检查，因为写入无法撤回；
- delta process 对 legacy full 放行；
- full process 对 delta 抛 `CheckpointModeMismatchError`。

### 5.2 `CheckpointStateAccessor`

```python
CheckpointStateAccessor.bind(graph, checkpointer, *, store=None, mode="full")
get(config)
async aget(config)
history(config, *, limit=None)
async ahistory(config, *, limit=None)
update(config, values, *, as_node=None)
async aupdate(config, values, *, as_node=None)
```

每个入口复制 config，注入内部 mode/metadata，再通过 compiled graph 读取或写入。delta 下不能把
raw saver 的 `checkpoint["channel_values"]` 当作完整状态，因为消息可能只是 delta sentinel，
真实值需要 reducer 重放 pending/channel writes 后才能得到。

`build_state_mutation_graph(as_node, mode, state_schema=None, *,
snapshot_frequency=None)` 构建只有一个 finish node 的 graph，供 rollback、context compaction、
branch/state replace 使用。它应用 reducer 但不安排 Agent node。

通用 `create_deerflow_agent()` 在同时收到 `checkpoint_channel_mode="delta"` 和真实 checkpointer 时
直接拒绝：该工厂没有 main lead-agent 路径的 marker/compatibility gate。只有完成整套 gate 装配的
调用方才能把 delta graph 接到持久化 saver。

### 5.3 reducer/schema

```python
merge_message_writes(
    state: list[AnyMessage],
    writes: Sequence[Any],
) -> list[AnyMessage]

delta_messages_field(snapshot_frequency=10)
get_thread_state_schema(mode, snapshot_frequency=None) -> type
adapt_state_schema_for_mode(schema, mode, snapshot_frequency=None) -> type
normalize_middleware_state_schemas(middleware, mode, snapshot_frequency=None)
```

`merge_message_writes()` 在线性 pass 中保留 LangGraph `add_messages` 的行为：

- 输入 coercion；
- 按 message ID replace；
- `RemoveMessage(id=...)`；
- 不存在 ID 的 remove 报错；
- `REMOVE_ALL_MESSAGES` 清空并应用其后的同批 writes；
- null write 与 public reducer 一致地拒绝。

`DeltaThreadState` 只把 `messages` 换成
`DeltaChannel(merge_message_writes, snapshot_frequency=N)`；其他 ThreadState 字段不随意改表示。
custom middleware 的 `state_schema` 也必须适配，否则同一 graph 内会出现 channel 类型漂移。

## 6. main 状态生命周期

### 6.1 full

```text
graph super-step
  -> reducer 得到完整 messages
  -> saver checkpoint 保存完整 channel value
  -> 下一次读取直接取得完整列表
```

消息越长，重复写入越多。

### 6.2 delta

```text
graph 构造
  -> messages channel = DeltaChannel(reducer, N)
run config
  -> metadata marker = delta
每个 super-step
  -> 保存增量 write / channel version
  -> checkpoint channel_values 对该 channel 可为 sentinel
每 N 次 write
  -> 形成新的完整 snapshot seed
读取
  -> graph.get_state/aget_state
  -> saver 取最近 seed + 后续 writes
  -> merge_message_writes 物化完整 messages
```

N 越大，checkpoint 写放大越小，但读取时需重放的增量上界越大。N 是性能/空间参数，不改变最终 reducer
语义。

### 6.3 full -> delta

1. legacy full checkpoint 没有 delta marker；
2. delta process 允许读取；
3.最近 full blob 作为物化 seed；
4.新的 delta writes 叠加；
5.首个 delta checkpoint 写 marker；
6.从此 full process 打开该 thread 会 fail closed。

反向 delta -> full 不受支持。若直接切换，full graph 可能把 sentinel/partial 值误当完整状态，因此 gate
在消费者拿到状态前拒绝。

### 6.4 branch、edit、rollback、resume

- state replacement 不能直接 raw `put` 一份字典；应通过 mode-matched graph 和 reducer；
- reducer channel 的 replace value 由 `Overwrite(...)` 包装；
- `graph_reducer_channels()` 同时识别 `BinaryOperatorAggregate` 和 `DeltaChannel`；
- rollback 先保存 materialized state 与 raw pending writes；
- 从旧 checkpoint resume 是 lineage fork，main 将选择的 materialized state 线性写成新 head，
  避免 abandoned sibling writes 再混入当前 delta history；
- checkpoint write 与 active Run 使用 thread operation reservation 串行。

## 7. main 第三方兼容补丁

### 7.1 InMemorySaver delta history

`ensure_inmemory_delta_history_patch()` 修复 full -> delta 首个 super-step：
上游 `InMemorySaver` 遇到 legacy full blob 时错误跳过同 checkpoint 的 pending writes，导致迁移后第一条
新消息消失。补丁让 InMemorySaver 委托 `BaseCheckpointSaver` 的正确 ancestor walk。

保护措施：

- idempotent flag；
- 只有上游仍有 override 时 patch；
- 验证版本为 LangGraph `1.2.9`；
- 更高版本告警，要求重新审查；
- 赋值失败时不让 import 崩溃。

### 7.2 empty reducer first-write Overwrite

`ensure_binop_overwrite_first_write_patch()` 修复
`BinaryOperatorAggregate` 空 channel 的第一条 `Overwrite(value)` 被原样保存成 wrapper。
这会使 `sandbox/goal/todos/promoted` 等 Union channel 后续读取出现
`'Overwrite' object is not subscriptable`。

补丁先做行为 probe；只有 bug 仍存在时才 override，第二个 Overwrite 仍按上游语义报
`INVALID_CONCURRENT_GRAPH_UPDATE`。

## 8. main 测试

| 测试 | 主要契约 |
| --- | --- |
| `test_checkpoint_mode.py` | freeze、marker、full/delta compatibility |
| `test_checkpoint_state.py` | accessor、history、state-only graph、Overwrite |
| `test_delta_channel_state.py` | reducer 等价性、线性复杂度、cadence |
| `test_delta_channel_checkpointers.py` | memory/SQLite/PostgreSQL materialization 与迁移 |
| `test_checkpoint_patches.py` | 两个上游缺陷 probe/patch |
| `test_gateway_checkpoint_mode.py` | Gateway read/write gate |
| `test_threads_checkpoint_mode.py` | thread state/history/branch API |
| `test_run_worker_delta_resume.py` | 旧 checkpoint fork 的线性 resume |
| `test_app_config_reload.py` | mode/cadence 配置与 restart requirement |
| checkpoint benchmark tests | full/delta 写体积、materialization 和 cadence 指标 |

## 9. main 关键提交的实现演进

| 提交 | 实际变化 |
| --- | --- |
| `42baed8c` | 引入 dual mode、DeltaThreadState、metadata gate、materialized accessor，并改造 Gateway/worker 所有 state consumers |
| `8c19a2eb` | 把 message delta fold 改成 ID index + 单 pass，保留 remove/replace 公共 reducer 语义，避免历史增长导致二次复杂度 |
| `d1aeea2c` | 通过 behavior probe 修复 empty BinaryOperatorAggregate 的 first-write Overwrite wrapper |
| `244ce773` | 旧 checkpoint resume 改为 materialized linear head，避免 delta sibling writes 污染 |
| `c48de5e7` | cadence 从常量升级为 `checkpoint_delta.snapshot_frequency`，并加入 process freeze、graph cache key 和测试 |
| `713ee544` | 避免 checkpoint 保留不必要的 base64 image payload，降低状态体积 |
| `e01173d8` | 增加 production/benchmark 数据，量化而非假设 delta 收益 |
| `3c8b82c5` | checkpoint mutation 与 Run 通过 durable thread reservation 串行 |

## 10. dev 的 project-scoped Checkpoint

### 10.1 源码地图

| 职责 | dev 路径 | 关键符号 |
| --- | --- | --- |
| scoped factory/wrapper | `backend/app/private_work/checkpointer.py` | `ProjectScopedCheckpointer`, `_ScopedCheckpointSaver` |
| private context | `backend/app/private_work/context.py` 等 | `PrivateWorkContext`, `PrivateResourceScope` |
| thread repository | `backend/packages/harness/deerflow/persistence/private_work/` | `PrivateThreadRepository` |
| Worker 注入 | `backend/app/reliability/execution.py` | `RunAgentPrivateExecutor.execute()` |
| Gateway chat controls | private-work routers/services | branch/edit/regenerate/state operations |
| raw saver bootstrap | persistence/checkpointer modules | PostgreSQL saver，仅基础设施层可直接持有 |

### 10.2 精确接口

```python
ProjectScopedCheckpointer(
    raw_saver: BaseCheckpointSaver,
    session_factory,
    *,
    quota=None,
)

for_context(context: PrivateWorkContext) -> _ScopedCheckpointSaver
```

异步 saver surface：

```python
aget_tuple(config)
aget_tuple_already_authorized(config, *, session)
aget(config)
alist(config, *, filter=None, before=None, limit=None)
aput(config, checkpoint, metadata, new_versions)
aput_already_authorized(config, checkpoint, metadata, new_versions, *, session)
aput_writes(config, writes, task_id, task_path="")
adelete_thread(thread_id, *, expected_version=None)
```

同步 `get_tuple/get/list/put/put_writes/delete_thread` 通过
`run_coroutine_threadsafe()` marshal 回创建 wrapper 的 owner loop；若从同一个 owner loop 调同步方法则
fail closed，避免 deadlock。

## 11. dev 授权调用链

### 11.1 普通读取/写入

```text
server-issued PrivateWorkContext
  -> ProjectScopedCheckpointer.for_context()
  -> _ScopedCheckpointSaver
  -> extract thread_id from configurable
  -> strip client private fields / client marker
  -> _locked_active(thread_id, capability, boundary operation)
       Worker path: PrivateRunExecutionBoundary.before_checkpoint_*
       其他路径: PrivateWorkRevalidator.require(..., lock=True)
       PrivateThreadRepository.get(scope, thread_id, lock=True)
  -> raw saver operation
  -> validate deerflow_private_scope marker
  -> public result or private 404/unavailable
```

marker 精确内容：

```json
{
  "project_id": "...",
  "owner_user_id": "..."
}
```

写入时 server 覆盖客户端 marker；读取与 list 的每个 item 都验证 marker。marker 是防误绑定/碰撞的
完整性证据，真正授权仍来自 capability、membership 和 scoped Thread row lock。

### 11.2 caller-held transaction

- `aget_tuple_already_authorized()` 要求当前 session 已在 transaction；调用者负责已持有 exact scoped
  authority lock，方法再做 marker 验证。
- `aput_already_authorized()` 除要求 transaction 外，还在同一 transaction 内重复
  membership/capability 与 Thread lock，然后 raw write + reread marker。

这些窄接口用于“最后 head 检查 + checkpoint CAS/write”必须留在一个数据库锁周期内的流程。

### 11.3 pending writes

`aput_writes()` 总是先通过 boundary/revalidator 和 scoped Thread lock。
它只有在 matching checkpoint tuple 已存在时才能验证 marker；LangGraph 合法地可能先写 pending writes、
后写 checkpoint row，因此首次 pending write 没有 marker 可读。

准确语义是：

```text
首次 pending writes：
  授权/成员/Thread lock 有效
  server 清洗后的 thread config 有效
  尚无 checkpoint marker 可验证

后续已有 tuple：
  再额外验证 marker
```

不能错误宣称每次 `aput_writes()` 都验证了 marker，也不能因此说首次写完全无授权。

### 11.4 删除

```text
transaction:
  revalidate + lock Thread
  expected_version CAS
  mark Thread deleted
  release ready-file quota
  mark private files deleted
  mark artifacts deleted
commit
-> raw saver.adelete_thread(thread_id)
-> success: checkpoint_delete_status=complete
-> failure: checkpoint_delete_status=retry_required
```

业务对象先不可见，再删除 raw checkpoint。跨两个存储步骤不可能完全原子，因此显式 durable
`retry_required` 是恢复协议的一部分。

## 12. 表示与授权的精确差异

| 维度 | main delta/full | dev scoped saver |
| --- | --- | --- |
| 问题 | 状态编码与重放 | 项目资源授权与隔离 |
| key | thread/checkpoint namespace | project + owner + thread authority |
| marker | `deerflow_checkpoint_channel_mode` | `deerflow_private_scope` |
| marker 目的 | 防错误模式物化 | 防 raw checkpoint 误绑定到错误 scope |
| 读写入口 | compiled graph accessor | scoped BaseCheckpointSaver wrapper |
| 恢复 | seed + delta writes | membership/lease revalidation + retry/delete status |
| 当前 dev 状态 | 尚未采用 | 已是生产安全边界 |

两类 marker 可以同时存在于 metadata，字段和验证顺序不冲突。

## 13. 已确认风险

### 13.1 main

1. cadence 不写 checkpoint metadata；跨进程配置不一致无法靠 checkpoint 自证，只能靠部署一致性。
2.直接读取 raw `channel_values` 会在 delta 下得到 sentinel/partial state。
3. LangGraph monkey patch 依赖上游内部行为；升级后必须重新跑 probe 和所有 saver 矩阵。
4.只支持 full -> delta；误切回 full 会让整个 thread fail closed，需要明确运维回滚方案。
5. custom middleware schema 漏做 delta adaptation 时，graph channel 语义不一致。

### 13.2 dev

1.首次 `aput_writes` 没有可验证 marker；安全依赖“所有业务路径只拿 scoped wrapper”、server-issued
   context、Thread lock 和 thread ID 约束，source-absence test 必须持续守住 raw saver import。
2.删除的业务 tombstone 与 raw saver delete 分两阶段；`retry_required` backlog 需要可观测和重试 worker。
3.同步 saver 从 owner loop 调用会失败；新代码不能在 async Worker 内误用 sync surface。
4.当前 full 表示在超长会话下写放大明显，但不能以性能理由绕过 scoped wrapper。

## 14. delta 移植到 dev 的精确落点

建议分层实现：

1. 在 harness 保留 main 的
   `checkpoint_mode.py`、`checkpoint_state.py`、`thread_state.py` reducer 与受 guard 的 patch。
2. raw PostgreSQL saver 继续只由基础设施装配；先包
   `ProjectScopedCheckpointer.for_context()`，再绑定 mode-matched compiled graph。
3. Worker 的
   `backend/app/reliability/execution.py::RunAgentPrivateExecutor.execute()` 必须注入同一
   mode/cadence 和 scoped saver。
4. Gateway 的 branch/edit/regenerate/context-compaction/state/history 必须全部用
   `CheckpointStateAccessor` + scoped saver，不得 raw read。
5. `aput_writes()` 的 pending-write-before-checkpoint 测试必须在 delta 下重跑，确认 marker与 delta
   metadata 都不被 client filter/serializer 丢失。
6. retry takeover 要用 materialized latest state 和 exact private Run snapshot，不能只复制 raw blobs。
7. 所有 Gateway/Worker 实例一次性切到 delta；配置校验应在启动 readiness 中暴露 mode/cadence。

## 15. 禁止直接合并

- 禁止用 main raw `CheckpointStateAccessor` 绕过 `ProjectScopedCheckpointer`。
- 禁止把 mode marker 当成 project authorization。
- 禁止把 private-scope marker 当成 delta/full compatibility marker。
- 禁止从客户端接受或信任任一 marker。
- 禁止在 delta 下直接消费 raw `channel_values`。
- 禁止只升级 Worker、不升级 Gateway chat controls，或反之。
- 禁止支持未设计的 delta -> full 热回滚。
- 禁止把 cadence 热加载；compiled graph 已绑定 channel 参数。
- 禁止因首次 pending write 尚无 marker而取消 membership/Thread lock。
- 禁止在 project 模块直接 import/raw instantiate checkpointer。

## 16. 建议测试矩阵

| 轴 | 场景 |
| --- | --- |
| 表示 | full、delta N=1/2/10/自定义大值 |
| saver | InMemory（仅单测）、真实 PostgreSQL；若保留 SQLite 则单独验证 |
| 迁移 | empty、legacy full -> first delta write、长 history、pending writes、restart |
| 拒绝 | delta thread 用 full read/write/history/branch 均 fail closed |
| reducer | add/replace/remove/remove-all/null、重复 ID、multi-write super-step |
| state mutation | update、branch、edit、regenerate、rollback、context compaction |
| retry | Worker crash 后 exact checkpoint takeover，不重放旧 input |
| scope | 同 thread ID 跨 project/owner marker mismatch；membership revoke；project frozen/deleted |
| pending write | checkpoint 前 writes 允许但授权有效；已有 tuple 时 marker 必验 |
| loop bridge | async surface、异线程 sync surface、owner-loop sync misuse |
| delete | raw delete success/failure、`retry_required`、writer 与 delete row-lock 串行 |
| 多进程配置 | Gateway/Worker mode/cadence 一致；任一不一致 readiness 失败 |
| upstream patch | 当前 LangGraph、升级版本、bug present/absent probe |
| source absence | project runtime 不得直接 import 或持有 raw saver |
