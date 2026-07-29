# 05. Run / Worker 模块：main 实现、dev 对照与落地边界

## 1. 分析基线与范围

- `main`：`e317f7b8d9b2afb4c3925812d4774da602c9f8f3`
- `dev`：`8a91e95799c9b345d9540c7e201b33c603e7870c`
- main 演进区间：`3be3969f..e317f7b8`
- 范围：Run admission、执行归属、取消、lease、恢复、Worker 服务拓扑、settlement 与测试。
- Checkpoint 的表示和 Stream 的存储协议分别在独立文档分析；本篇只说明 Run/Worker 如何调用它们。

## 2. 首要结论：两条分支不是同一种 Worker

main 的
`backend/packages/harness/deerflow/runtime/runs/worker.py::run_agent()` **不是独立 Worker
进程**。它是 Gateway 进程拥有的异步协程，由
`backend/app/gateway/services.py::start_run()` 在同一事件循环内通过
`asyncio.create_task()` 启动。main 的 `owner_worker_id` 实际表示拥有该任务的 Gateway runtime
instance。

dev 才是最终 SaaS 拓扑：

```text
Gateway 只准入、查询、回放
Worker 独立进程独占 Agent graph 执行
Scheduler 只准入到期 Automation
PostgreSQL Job + lease 是跨进程执行权威
```

所以 main 的 Gateway-owned run task 不能合并进 dev。main 可移植部分仅限
`run_agent()` 内部与 SaaS 权威无关的纯执行修复，并且必须继续由
`RunAgentPrivateExecutor` 在已 claim 的 Worker lease 内调用。

## 3. main 源码地图

| 职责 | 路径 | 关键符号 |
| --- | --- | --- |
| HTTP/internal 启动 | `backend/app/gateway/services.py` | `start_run()`, `launch_scheduled_thread_run()` |
| Run 生命周期 | `backend/packages/harness/deerflow/runtime/runs/manager.py` | `RunManager`, `RunRecord`, `RunStartOutcome`, `CancelOutcome` |
| Agent 执行协程 | `backend/packages/harness/deerflow/runtime/runs/worker.py` | `run_agent()` |
| 持久化模型 | `backend/packages/harness/deerflow/persistence/run/model.py` | `RunRow` |
| SQL repository | `backend/packages/harness/deerflow/persistence/run/sql.py` | `RunRepository` |
| store 契约 | `backend/packages/harness/deerflow/runtime/runs/store/base.py` | `RunStore` |
| Gateway singleton 装配 | `backend/app/gateway/deps.py` | RunManager/bridge/checkpointer 生命周期 |
| ownership 配置 | `backend/packages/harness/deerflow/config/run_ownership_config.py` | `RunOwnershipConfig` |

## 4. main 精确接口

### 4.1 启动入口

```python
async def start_run(
    body: RunCreateRequest,
    thread_id: str,
    request: Request,
) -> RunRecord
```

该函数同时完成输入清洗、thread owner 检查、model allowlist、graph config、持久化准入和任务 attach。

```python
async def run_agent(
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    *,
    ctx: RunContext,
    agent_factory: Any,
    graph_input: dict,
    config: dict,
    stream_modes: list[str] | None = None,
    stream_subgraphs: bool = False,
    interrupt_before: list[str] | Literal["*"] | None = None,
    interrupt_after: list[str] | Literal["*"] | None = None,
) -> None
```

尽管文件名是 `worker.py`，这里没有消息队列 claim loop、Worker 进程注册或 Job handler。

### 4.2 `RunManager`

关键公开方法：

```python
create_or_reject(thread_id, assistant_id=None, *,
                 on_disconnect=..., metadata=None, kwargs=None,
                 multitask_strategy="reject", model_name=None,
                 user_id=None) -> RunRecord
try_start(run_id) -> RunStartOutcome
set_status(run_id, status, *, error=None, stop_reason=None, ...)
set_status_if_not_cancelled(run_id, status, ...)
cancel(run_id, *, action="interrupt") -> CancelOutcome
reserve_thread_operation(thread_id, *, kind, user_id=None)
reconcile_orphaned_inflight_runs(*, error, before=None, stop_reason=None)
start_heartbeat()
stop_heartbeat(timeout=5.0)
shutdown(timeout=5.0)
```

`RunRecord` 同时持有 durable 字段与进程内控制对象：

- `run_id`, `thread_id`, `status`, `operation_kind`
- `multitask_strategy`, `model_name`, `user_id`
- `owner_worker_id`, `lease_expires_at`
- `cancel_action`, `cancel_requested_at`
- `task: asyncio.Task | None`
- `abort_event`, `abort_action`, `ownership_lost`, `finalizing`

`RunRow` 有 partial unique active-run index，保证同 thread 的 pending/running 互斥。

### 4.3 ownership 配置

```python
class RunOwnershipConfig(BaseModel):
    lease_seconds: int = 30       # >= 5
    grace_seconds: int = 10       # >= 0
    heartbeat_enabled: bool = False
```

heartbeat 周期是约 `lease_seconds / 3`；orphan sweep 每三个周期运行一次。跨 Gateway 时依赖 UTC
时钟，`grace_seconds` 同时是 clock-skew budget。

## 5. main 完整调用链

### 5.1 准入与 task attach

```text
HTTP run endpoint
  -> start_run(body, thread_id, request)
       validate secrets / stream modes / model allowlist
       check thread owner
       normalize graph input or Command(resume)
       build_run_config + checkpoint selector validation
       inject authenticated runtime context
       async with goal_thread_lock(thread_id)
         -> RunManager.create_or_reject(...)
              -> _admit_thread_operation(...)
                   local self._lock
                   local inflight check
                   RunStore.create_thread_operation_atomic(...)
                   DB partial unique active-run constraint
                   local register
         -> worker = run_after_metadata(record)  # coroutine object
         -> record.task = asyncio.create_task(worker)
  -> return RunRecord
```

源码明确要求 durable admission 与 task attach 之间**不能出现 await**。attach 失败会关闭 coroutine，
并用 `fail_start_if_pending()` 终结尚未启动的 Run。

`run_after_metadata()` 可等待非致命 thread metadata 初始化，但无论超时/取消都进入
`run_agent()`，由其统一 startup barrier 做取消和 stream finalization。

### 5.2 执行

```text
Gateway event loop task
  -> run_agent()
       normalize stream modes
       create RunJournal
       wait_for_prior_finalizing(thread_id, run_id)
       RunManager.try_start(run_id)        # pending -> running CAS
       checkpoint mode compatibility check
       publish metadata
       build Runtime context / tracing / journal callback
       build agent graph
       capture rollback point
       agent.astream(...)
       classify cancel / fallback / guard / delivery
       set_status_if_not_cancelled(...)
       finally:
         flush internal events
         persist completion/delivery receipt/duration
         publish stream terminal/end
         update thread metadata/title
         cleanup RunManager record
```

这是一条 Gateway 内部调用链，不存在“Gateway 写 Job，独立 Worker claim Job”步骤。

### 5.3 multitask strategy

- `reject`：local inflight 或 DB unique 冲突 => `409`；
- `interrupt`：在 store transaction 中 claim/终结旧 active row，再登记新 Run；本机旧 task 收到 abort；
- `rollback`：同样先取得 replacement admission，旧 task 终结时恢复 pre-run checkpoint；
- 非 Run 的 checkpoint write 通过 `reserve_thread_operation()` 使用同一 durable active-row
  互斥，防止与 Run 并发写同一 thread。

local lock 从“检查”一直持有到“store insert + local register”，关闭“DB 已插入但本机又报冲突”的泄漏窗口；
跨进程由 partial unique index 和 transaction lock 解决。

## 6. main lease、取消与恢复语义

### 6.1 lease

heartbeat 开启时，新 Run 写入 `owner_worker_id` 和 `lease_expires_at`。只有 durable renew 成功后，
内存中的 confirmed deadline 才推进。

`_renew_leases()`：

1. 收集本实例 pending/running records；
2. 以“最后确认 deadline 剩余时间”为当前 renew 调用 timeout；
3. deadline 前 transient error 只记录并重试；
4. 到期仍不能确认、renew 被 store 拒绝或 renew 完成时旧 deadline 已过：
   `_mark_ownership_lost()` 设置 abort/fence；
5. renew 返回 durable cancel action 时，本实例在所有 lease 都尝试后再 signal local task。

因此“数据库短暂失败”与“已无法证明执行权”有不同语义；后者 fail-stop。

### 6.2 跨 Gateway 取消

- cancel action 以 durable first-writer-wins 记录；
- owner Gateway 的 heartbeat 读取 cancel intent，再设置本地 abort；
- owner 不可达时，取消方只改变 durable intent，不伪造自己拥有 graph task；
- terminal CAS 使用 `set_status_if_not_cancelled()`，已持久化取消优先于迟到 success/error。

### 6.3 orphan recovery

peer reconciliation 对过期 lease 做条件 claim，避免 candidate scan 后 heartbeat 又续租的竞态。
它会把 orphan Run 标成 error/terminal 并补 stream 结束，**不会重新执行 Agent graph**。
这是 main 的恢复语义：终止悬挂状态，而不是 exactly-once resume。

### 6.4 shutdown

`RunManager.shutdown()` 停止 heartbeat/orphan task，向本地 Run 发取消，在有界 timeout 内 drain task，
再允许 checkpointer/bridge teardown。没有“将正在执行 Run 归还 Job queue”的动作，因为 main 无 Job queue。

## 7. main 持久化与状态机

核心 durable 字段：

```text
pending -> running -> success | error | interrupted
```

额外控制：

- `finalizing` 是进程内/持久化协作阶段，阻止 replacement 在 rollback/delivery 尚未完成时破坏顺序；
- `operation_kind` 把正常 Run 和 checkpoint write reservation 放在同一互斥域；
- `owner_worker_id + lease_expires_at` 表示当前 Gateway instance 的执行租约；
- `cancel_action + cancel_requested_at` 表示 durable 取消意图；
- completion、token、delivery、duration 等由 RunManager/store 分阶段更新。

main 可以使用 memory/SQLite/PostgreSQL application store。只有 PostgreSQL partial unique index、行锁和
lease CAS 能提供真正跨进程语义；memory store 仅进程内。

## 8. main 测试

| 测试 | 主要契约 |
| --- | --- |
| `test_run_manager.py` | local 状态机、multitask、取消、cleanup |
| `test_run_repository.py` | SQL CAS、active unique、lease/cancel fields |
| `test_multi_worker_run_ownership.py` | 跨 Gateway admission、renew、cancel、takeover、race |
| `test_multi_worker_postgres_gate.py` | 真实 PostgreSQL ownership gate |
| `test_gateway_run_recovery.py` | startup orphan recovery 与 stream terminal |
| `test_gateway_run_drain_shutdown.py` | shutdown drain 顺序和 bounded timeout |
| `test_run_worker_rollback.py` | startup barrier、rollback、fallback、context |
| `test_run_worker_delivery.py` | terminal delivery receipt、lease loss fencing |
| `test_wait_disconnect_handling.py` | disconnect cancel/continue |
| `test_run_worker_delta_resume.py` | worker 对 checkpoint resume 的调用契约 |

## 9. main 关键提交的实现演进

| 提交 | 实际变化 |
| --- | --- |
| `3bc3af25` | 给 RunRow/RunStore 增加 owner+lease；local lock 覆盖 store insert；PostgreSQL partial unique/CAS 关闭多 Gateway 双启动 |
| `8a78c264` | remote cancel 写 durable intent，由 owner heartbeat 投递到本地 task |
| `b53c1ae0` | owner 不可达/lease 变化时取消退化为安全 takeover/terminal，而非跨进程直接 cancel task |
| `80c06414`, `8af760fc` | orphan scan 改为 lease-aware conditional claim，补 startup/periodic recovery |
| `090e80c1` | 以最后确认 deadline 为界；无法证明 lease 时 fence 本地执行并阻止迟到 terminal 覆盖 |
| `bb9f67aa` | admission 在调用协程被取消时严格关闭已插入 replacement，避免 pending row 泄漏 |
| `3c8b82c5` | checkpoint write 复用 thread operation reservation，与 Run durable 串行 |
| `c7538cfb` | orphan terminal 同步补流结束，客户端不再永久等待 |
| `1c753124`, `6f53fd5e` | terminal 增加 delivery receipt 与输出验证 |
| `fcbf0609`, `244ce773` | edit/rerun 与 delta resume 的 worker 恢复流程 |

## 10. dev 最终 Run / Worker 实现

### 10.1 源码地图

| 职责 | dev 路径 | 精确符号 |
| --- | --- | --- |
| Gateway 准入 | `backend/app/private_work/run_admission.py` | `PrivateRunAdmissionService.admit()` |
| 独立 Worker 入口 | `backend/app/worker/app.py` | `run_worker()`, `main()` |
| claim loop | `backend/app/worker/service.py` | `WorkerService`, `JobLeaseAuthority`, `JobSettlement` |
| 私有执行 | `backend/app/reliability/execution.py` | `RunAgentPrivateExecutor.execute()` |
| Job adapter | `backend/app/reliability/execution.py` | `PrivateRunJobHandler._begin/__call__/_settlement` |
| side-effect boundary | `backend/app/reliability/execution.py` | `PrivateRunExecutionBoundary` |
| stream lease adapter | `backend/app/reliability/execution.py` | `LeaseAuthorizedStreamBridge` |
| Job repository/state | `backend/app/reliability/jobs.py` 及 persistence private-work repository | claim/heartbeat/settle |
| Worker registry | private-work persistence/repository | register/heartbeat/draining/remove |

### 10.2 Gateway 准入链

```text
authenticated project Run request
  -> PrivateRunAdmissionService.admit()
       sanitize client-controlled fields
       transaction:
         lock Project / Membership / Thread
         require capabilities
         resolve exact Agent
         resolve exact enabled Skills
         resolve exact MCP grants + Credential versions
         create immutable Run snapshot
         create Run row
         create Job row
         reserve concurrent quota
         append audit
       commit
  -> Gateway 返回 admission/result；不执行 graph
```

Run、snapshot、Job、quota 与 audit 在一次 transaction 中形成准入闭包。Gateway 没有
`asyncio.create_task(run_agent(...))`。

### 10.3 独立 Worker 链

```text
python -m app.worker.app
  -> run_worker()
       build PostgreSQL services/checkpointer/stream bridge
       build RunAgentPrivateExecutor
       build PrivateRunJobHandler
       WorkerService.run(stop_event)
         -> register worker node
         -> claim Job up to capacity
         -> mark_running
         -> JobLeaseAuthority heartbeat
         -> PrivateRunJobHandler._begin()
              lock/revalidate project + membership
              begin_execution(job_id, raw lease token)
              check existing durable terminal
              load exact persisted snapshot
              inspect checkpoint and prepare retry takeover
         -> RunAgentPrivateExecutor.execute()
              materialize exact Agent/Skill/MCP/Credential runtime
              create PrivateRunExecutionBoundary
              create project-scoped checkpointer
              create LeaseAuthorizedStreamBridge
              call harness run_agent()
         -> stop heartbeat
         -> JobSettlement.commit()
              lease-token CAS
              settle Run + Job
              release quota
              audit terminal
```

`RunAgentPrivateExecutor` 是 production 中唯一把 private Run 接到 harness `run_agent()` 的 adapter。
它内部创建的 `RunManager()` 只管理本次 graph 的内存状态/usage/abort，不承担准入、Job claim 或跨 Worker
ownership。

## 11. dev 权威与故障语义

### 11.1 Job lease

- Worker claim 获得 raw lease token；
- 数据库只保存 token hash；
- raw token 仅在 Worker 内存与授权调用链中存在；
- heartbeat 和 settlement 都要求 exact `(job_id, token)`；
- lease 丢失后，Worker 不得写 terminal 或继续 side effect；
- detached/uncooperative handler 不能续租，Worker capacity 会 fail-stop，而不是把槽位交给另一个并发执行。

### 11.2 side-effect boundary

`PrivateRunExecutionBoundary` 在以下边界前重新验证 project/membership 和 lease：

- model call
- tool call
- MCP dispatch
- sandbox write/exec/restore
- checkpoint read/write
- stream frame/terminal
- file finalization

不可幂等外部调用如果执行结果未知，会标记 `AmbiguousExternalSideEffect`，settlement 使用
`SIDE_EFFECT_STATE_UNKNOWN`，不把它当作普通可安全重试失败。

### 11.3 retry / takeover

新 Worker claim retry attempt 后：

1. `_begin()` 先查 durable stream terminal；已有 terminal 则直接据此 settlement，不重复 graph；
2. 加载 exact admitted snapshot，不按当前 catalog 重新解析；
3. 读取最新 checkpoint ID；
4. `prepare_checkpoint_takeover()` 决定是否从 checkpoint 接管；
5. resume 时删除客户端旧 selector，固定 root namespace，graph input 不重复注入；
6. settlement 仍由当前 raw lease token CAS。

## 12. main 与 dev 的精确差异

| 维度 | main | dev |
| --- | --- | --- |
| graph 执行位置 | Gateway event loop background task | 独立 Worker 进程 |
| 准入权威 | RunManager + RunStore active row | Gateway transaction 创建 Run snapshot + Job + quota + audit |
| 执行 claim | 无 Job claim | PostgreSQL Job lease |
| owner 字段 | Gateway instance identity | Worker node + Job attempt/raw token authority |
| 恢复 | expired Run 标 error，不重执行 | Job retry 可按 exact snapshot/checkpoint takeover |
| 资产 | 运行时解析当前 config | admission 时冻结精确 Agent/Skill/MCP/Credential |
| scope | user/thread owner | account/project/membership/owner |
| side-effect fencing | Run lease 主要围绕 terminal/task | 每个副作用边界 revalidate lease + auth |
| Scheduler | 可复用 Gateway launch path | 只原子准入 occurrence/Run/snapshot/Job |
| 取消 | durable Run cancel intent + owner Gateway | Job/Run cancel marker，由 lease holder 协作停止和 settlement |
| settlement | Gateway RunManager 写 Run status | Worker lease-token CAS 原子结算 Run/Job/quota/audit |

## 13. 已确认风险

### 13.1 main

1. `worker.py` 命名容易让维护者误以为已有独立 Worker；实际 graph 与 HTTP 共用 Gateway event loop/process。
2. orphan recovery 只 terminalize，不 resume；长任务在 Gateway crash 后必须重新发起。
3. heartbeat 默认关闭；多 Gateway 若未显式启用 ownership 配置，不具备期望的 lease 语义。
4. memory stream bridge 与多 Gateway 不兼容，需 Redis；这属于 main 拓扑的额外运行条件。
5. ownership 主要保护 Run 状态，不能替代 dev 每个 project side-effect 的 capability revalidation。

### 13.2 dev

1. harness `RunManager()` 名称与真正 Job authority 并存，后续代码若误用其 store admission 会形成双权威。
2. uncooperative handler 在 lease 丢失后会占住/停止容量，这是刻意的安全 fail-stop，但需要运维告警与进程重启策略。
3. retry 必须同时满足 exact snapshot、checkpoint takeover 与无已存在 terminal；任一顺序变化都可能重复模型或副作用。
4.所有新增 harness side effect 必须挂到 `PrivateRunExecutionBoundary`；仅在 handler 开头验证一次不够。

## 14. 可移植落点

main 的纯执行改进应落到：

- `backend/packages/harness/deerflow/runtime/runs/worker.py::run_agent()`；
- 由 `backend/app/reliability/execution.py::RunAgentPrivateExecutor.execute()` 继续唯一调用；
- bridge 必须保持 `LeaseAuthorizedStreamBridge`；
- checkpointer 必须保持 `ProjectScopedCheckpointer`；
- private runtime 必须来自 admitted snapshot。

可考虑移植：

- stale fallback ID 过滤；
- root/subgraph terminal 分类修复；
- rollback/continuation 内与 storage authority 无关的 reducer 修复；
- delivery receipt 的纯内容判定；
- 有边界的 internal event batching。

每项都必须在 dev Worker lease 丢失、membership 撤销、retry takeover 场景下重测。

## 15. 禁止直接合并

- 禁止把 main `start_run()->asyncio.create_task(run_agent())` 带入 dev Gateway。
- 禁止让 Scheduler 执行 graph。
- 禁止用 main `RunManager.create_or_reject()` 替代 `PrivateRunAdmissionService.admit()`。
- 禁止在 private production path 启用第二套 RunStore ownership/lease。
- 禁止把 main `owner_worker_id` lease 当成 dev Job raw-token lease。
- 禁止按当前 catalog 重建 retry runtime；必须使用 exact admitted snapshot。
- 禁止在 lease 丢失后由 harness local status 覆盖 durable Run/Job。
- 禁止让 `RunAgentPrivateExecutor` 之外的路径调用 private graph。
- 禁止把 raw lease token 写数据库、日志、事件或 checkpoint。

## 16. 建议测试矩阵

| 场景 | 期望 |
| --- | --- |
| Gateway admission | 一次事务生成 Run/snapshot/Job/quota/audit；graph 未被调用 |
| 并发准入 | 同 project/thread 的互斥与允许策略；跨 project 不互相阻塞 |
| Worker capacity | 活动 handler 永不超过配置值 |
| claim/heartbeat | raw token 正确时续租；hash/attempt/worker 不匹配立即失权 |
| late cancel | heartbeat 后 cooperative stop；cancel 优先于迟到 success |
| lease loss | model/tool/MCP/checkpoint/stream/file 下一边界失败；不 settlement |
| crash retry | 无 terminal + 有 checkpoint 时接管；不重放 graph input |
| durable terminal | retry claim 发现 terminal 后只 settlement，不再次执行 |
| exact snapshot | catalog/credential 更新或删除后，旧 admitted closure 按既定失败语义处理 |
| ambiguous side effect | 不自动安全重试，公开码固定，audit 不含 raw error |
| drain | 正常 inflight 完成；超时 cancel 本地 task 但不伪造 terminal |
| fleet loss | registry/heartbeat 失权停止 claim，并 drain 当前任务 |
| process boundary | Worker module 不 import Gateway；Gateway/Scheduler 不 import graph executor |
| isolation | account/project/owner 交叉矩阵，外部得到 404/403，内部不泄露资源存在性 |
| release gate | 固定真实 PostgreSQL gate 0 skip，覆盖 Gateway restart、Worker takeover、quota/audit |
