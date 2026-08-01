# 05. Run / Worker 模块：main 实现、dev 对照与落地边界

## 1. 分析基线与范围

- `main`：`e317f7b8d9b2afb4c3925812d4774da602c9f8f3`
- `dev` 本轮迁移前：`785be51341c1c3ddaa073b76aaa4421bee0ac136`
- main 演进区间：`3be3969f..e317f7b8`
- 范围：Run admission、执行归属、取消、lease、恢复、Worker 服务拓扑、settlement 与测试。
- Checkpoint 的表示和 Stream 的存储协议分别在独立文档分析；本篇只说明 Run/Worker 如何调用它们。

## 本轮先说清楚移植什么（2026-07-30）

本轮只迁移能落在 dev 独立 Worker + PostgreSQL Job lease 权威内的执行语义，按下列顺序实施：

| 顺序 | 移植能力 | dev 落点 | 验收 |
| --- | --- | --- | --- |
| 1 | Goal continuation 并发计数从锁内最新状态递增；用户消息竞态 stand-down 不重复计数 | `runtime/runs/worker.py` | 两个定向并发回归 |
| 2 | 子图 namespaced custom frame 不再写成父 Run 的 subagent 事件 | `runtime/runs/worker.py` | SSE 仍保留 namespace，数据库只记录 root task |
| 3 | RunJournal、subagent、workspace 内部事件写入绑定 exact scope + raw Job lease | `runtime/events/store/db.py`、`app/reliability/execution.py` | 真实 PostgreSQL 篡改 lease 后写入失败且无脏事件 |
| 4 | 本轮产生 output 时，至少一个本轮 output 必须由可信 `present_files` artifact 呈现 | `runtime/runs/worker.py` | 新/改 output、旧 artifact、workspace-only、delete-only 矩阵 |
| 5 | 确定性的 admitted snapshot/model stale 错误保持 permanent，不被通用异常转换成 retry | `app/reliability/execution.py` | 真实 PostgreSQL Run error、Job dead、无 retry |
| 6 | 真实浏览器连续多轮模型调用、刷新恢复、Worker 审计与截图 | `http://localhost:2026` | 至少三轮真实模型调用；截图写入本模块 evidence 目录 |

明确不迁移：

- main 的 Gateway `asyncio.create_task(run_agent())`、RunStore ownership/heartbeat/orphan 链；
- main 的 `run.delivery` receipt 持久化顺序；dev 继续以文件终结器、durable stream 和 Job settlement 为权威；
- delta checkpoint resume 与 edit replay rollback；它们属于 06 Checkpoint；
- stream 存储/回放协议改造；它属于 07 Streaming。

执行门槛：本模块后端与真实 PostgreSQL 门禁通过，并完成浏览器截图前，不读取或开始 06。

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

## 17. 本轮实际移植结果

### 17.1 Goal continuation 的两个并发修复

`_persist_goal_evaluation()` 在 `goal_thread_lock` 内重读当前 Goal 后，用
`max(调用方旧值, 当前 continuation_count + 1)` 得到新计数。这样较早开始、较晚取得锁的 evaluator
不会把已提交的次数覆盖回旧值。

首次 continuation 已提交后若检测到用户消息插入，第二次持久化只写
`thread_changed_before_continuation`，不再传同一个 `continuation_count`，避免新鲜值保护逻辑把同一
尝试再加一次。

### 17.2 root / subgraph 事件边界

namespaced child custom frame 仍按 `custom|<namespace>` 实时发布给 SSE 消费者，但只有 root namespace
的 task lifecycle custom frame 才进入 `_SubagentEventBuffer`。这避免 child graph 自己产生的 custom
frame 被重复解释为父 Run 的 `subagent.start/step/end`。

### 17.3 内部事件使用原始 Job lease 原子授权

新增 `LeaseAuthorizedRunEventStore`，由 `RunAgentPrivateExecutor` 注入 `RunContext`。它覆盖调用方传入的
scope，固定使用 admitted `PrivateResourceScope`，并把内存中的 `StreamLeaseProof` 传给
`DbRunEventStore.put/put_batch`。

数据库写事务内依次锁定并检查：

1. exact project、owner、thread、Run、Job；
2. project active/suspension 与 exact membership version/role；
3. Job 与 Run 都为 running；
4. Job hash 与 Run execution hash 都匹配 raw token；
5. 两个 lease 均未过期；
6. Job/Run/authorization 均未请求取消。

任何 lease loss、授权撤销或取消都会先更新 Worker boundary 状态，再拒绝事件；事件行和 sequence
预留随事务一起回滚。该链覆盖 RunJournal、subagent batch 与 private workspace-change event。

### 17.4 dev-native delivery verdict

判定只消费 `PrivateFileFinalizer` 的可信 `FinalizationResult`：

- produced outputs：`workspace_changes.created/modified` 中的 `outputs/*`；
- presented outputs：当前 Run 创建的 artifact metadata 中的 `logical_path`；
- 没有 produced output：可成功；
- 有 produced output 且两个集合有交集：可成功；
- 有 produced output 但无交集，包括只展示旧 output：Run 为 error。

终结器已经提交的 ready 文件不会因为 delivery error 被删除。这里没有引入 main 的
`put_if_absent(run.delivery)`、staged terminal 或 Gateway RunStore authority。

### 17.5 permanent failure 保持 terminal

`RunAgentPrivateExecutor.execute()` 现在让 `PermanentExecutionError` 原样到达
`PrivateRunJobHandler`。缺失 exact admitted model 等确定性 drift 会终结 Job，而不会被通用异常包装成
`PRIVATE_RUN_EXECUTION_FAILED` 后进入 retry。

### 17.6 follow-up suggestion 解析 stable model ref

真实浏览器回归发现：Agent snapshot 使用稳定别名 `default` 时，主 Run 已正确解析为配置中的精确模型，
但 follow-up suggestion 辅助调用仍把字符串 `default` 直接交给 `run_oneshot_llm()`，因此每轮主 Run
成功后会记录一次 `Model default not found in config`。

`ProjectChatControlService.suggest()` 现在同样通过 `ConfiguredModelRefResolver` 解析 admitted Agent 的
`model_ref`；`default` 精确落到当前配置的第一个逻辑模型名，无法解析时安全返回空建议且不泄露模型配置。
这不改变 Run/Worker 的主执行权威，只修复浏览器验收中实际触发的同会话辅助模型调用。

## 18. 后端验证结果

本轮按红灯到绿灯执行：

- Goal 并发 stale writer：修复前得到 `1`，期望 `3`；修复后通过。
- user-message race double count：修复前得到 `2`，期望 `1`；修复后通过。
- namespaced child custom persistence：修复前同时落入 `child-task` 与 `root-task`；修复后只落
  `root-task`。
- stale internal event：修复前 `DbRunEventStore.put()` 不接受 lease；实现后真实 PostgreSQL 上 live
  write 成功、篡改 lease hash 后抛 `StreamWriteLeaseLost`，且 stale event 不存在。
- delivery：修复前“新 output 未展示”仍为 success；修复后为 error，其余矩阵保持预期。

当前结果：

```text
05 相关单元/非 PostgreSQL：314 passed
RunEventStore（包含真实 PostgreSQL 用例）：17 passed
Run/Worker + file finalizer + authorization 真实 PostgreSQL：95 passed
Project chat controls（真实 PostgreSQL）：7 passed
Ruff format/check：passed
```

## 19. 真实浏览器与外部模型门禁

测试入口为 `http://localhost:2026` 的默认项目，实际登录用户为本地验收账号，实际模型为
`DeepSeek V4 Pro`。同一 Thread
`5a24cadc-3963-4578-ae40-dce2a9414bcc` 连续执行四轮，不使用 mock：

| 轮次 | 验证内容 | 最终结果 | Token |
| --- | --- | --- | --- |
| 1 | `314 + 95`，不调用工具 | `409` | 输入 8,939 / 输出 38 / 总计 8,977 |
| 2 | 依赖上一轮结果再加 1 | `410` | 输入 8,970 / 输出 125 / 总计 9,095 |
| 3 | 写入新 output，并强制 `present_files` | `文件已展示` | 输入 18.3K / 输出 244 / 总计 18.6K |
| 4 | 依赖前三轮上下文汇总 | `409,410,文件已展示` | 输入 9,346 / 输出 185 / 总计 9,531 |

第 3 轮创建并展示
`/mnt/user-data/outputs/run-worker-05-e2e.txt`，浏览器 artifact 面板读取到精确内容
`RUN-WORKER-05-DELIVERY`。刷新页面后四轮消息、token、artifact 均从 durable state 恢复。

第 4 轮 Run `9f056503-28a1-452b-bcef-62bf5a8dddd4` 的数据库只读核验结果为：

```text
Run.status=success
Run.finalization_status=complete
Job.status=succeeded
Job.attempt_count=1
LLM calls=1
Input/Output/Total=9346/185/9531
```

同轮结束后，follow-up suggestion 对 DeepSeek 的外部 HTTP 调用返回 `200 OK`，Gateway 新增日志中没有
`Project suggestion model call failed`、traceback 或 error。项目审计页存在对应的
`Run admitted / Run finished` 成对成功记录。

截图证据：

- [前三轮上下文结果](evidence/05-run-worker/02-first-two-context-turns.png)
- [文件交付与 artifact 内容](evidence/05-run-worker/01-multi-turn-terminal-and-artifact.png)
- [刷新后持久化结果](evidence/05-run-worker/03-after-refresh-persisted-turns.png)
- [第 4 轮跨轮上下文与 token](evidence/05-run-worker/06-fourth-context-and-model-resolution.png)
- [第 4 轮审计记录](evidence/05-run-worker/07-fourth-run-audit.png)

结论：05 Run/Worker 的后端、真实 PostgreSQL、四轮真实外部模型、工具文件交付、刷新恢复和审计门禁均已
通过，可以按顺序开始 06 Checkpoint。
