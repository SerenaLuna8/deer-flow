# 09. Gateway API 模块：旧 RunManager 增量与项目私有 Gateway 对照

## 1. 分析边界与结论

本文分析 `main@e317f7b8` 在 Gateway 线程、Run、SSE、replay、并发和错误处理上的最终实现，
并对照 `dev@8a91e957` 的 Project-first、Worker-only Gateway。

必须先区分两类结论：

- **已确认实现缺口/风险**：源码中能直接证明 `dev` 缺少某项行为或安全条件；
- **待复现/待审计项**：`main` 修复过旧架构的问题，但 `dev` 已经换了存储、事务或接口，
  不能仅凭提交标题断言 `dev` 也有同一缺陷。

结论先行：

1. `main` 的 edit-and-rerun、settled checkpoint lineage 和 Run duration 投影是可以移植的行为，
   但必须落入 `ProjectChatControlService`、`ProjectScopedCheckpointer` 和 project-private API。
2. `main` 的 Gateway `RunManager`、Gateway 后台 graph task、Memory/Redis stream、跨 Gateway
   进程取消和旧 lease recovery **不能合并**。`dev` 已用 PostgreSQL Job lease、独立 Worker 和
   durable RunEventStore 替代。
3. `main` 的 create-thread insert race、metadata merge、trace precedence、read-limit 修复不能
   一概记作 `dev` 缺陷：当前 `dev` 已有 optimistic version、正数 Query bounds 和不同 trace
   注入链；create 的幂等语义也不同。它们应分别验证，而不是先修。
4. Gateway 在 `dev` 中只负责认证、project context、事务准入、查询和 durable SSE 回放；
   任何移植都不能让 Gateway 再执行 Agent graph。

基线：

- 共同祖先：`3be3969f8fc3f2d2b6d36ef5c26fa5593d916f2a`
- `main`：`e317f7b8d9b2afb4c3925812d4774da602c9f8f3`
- `dev`：`8a91e95799c9b345d9540c7e201b33c603e7870c`

## 2. 源码地图

### 2.1 `main` 最终实现

| 层 | 文件 | 关键入口 |
| --- | --- | --- |
| 应用 | `main:backend/app/gateway/app.py` | lifespan、依赖装配、router |
| 请求组装 | `main:backend/app/gateway/services.py` | `normalize_input()`、`build_run_config()`、`start_run()`、`sse_consumer()` |
| Run schema | `main:backend/app/gateway/run_models.py` | `RunCreateRequest` |
| Thread API | `main:backend/app/gateway/routers/threads.py` | create/search/get/patch/delete/state/history/branch/goal/compact |
| Thread Run API | `main:backend/app/gateway/routers/thread_runs.py` | create/stream/reconnect/wait/cancel/messages/token usage/replay |
| Run API | `main:backend/app/gateway/routers/runs.py` | 按 Run ID 的读取和控制 |
| Replay 算法 | `main:backend/app/gateway/checkpoint_lineage.py` | parent lineage、settled base、legacy fallback |
| Trace | `main:backend/app/gateway/trace_middleware.py` | `X-Trace-Id` 请求上下文 |
| Run 管理 | `main:backend/packages/harness/deerflow/runtime/runs/manager.py` | Gateway 进程中的 Run 生命周期 |
| Run 持久化 | `main:backend/packages/harness/deerflow/runtime/runs/store/base.py`、`main:backend/packages/harness/deerflow/runtime/runs/store/memory.py` | Run row 和查询 |
| Stream | `main:backend/packages/harness/deerflow/runtime/stream_bridge/base.py`、`main:backend/packages/harness/deerflow/runtime/stream_bridge/memory.py`、`main:backend/packages/harness/deerflow/runtime/stream_bridge/redis.py` | Memory/Redis 回放 |

`main` 的公共路由根仍是 `/api/threads` 和 `/api/runs`。

### 2.2 当前 `dev` 权威实现

| 层 | 文件 | 关键入口 |
| --- | --- | --- |
| Project router | `dev:backend/app/gateway/routers/private_work.py` | `/api/projects/{project_id}/private-work` 全部私有接口 |
| Strict schema | `dev:backend/app/gateway/private_work_schemas.py` | `PrivateRunCreateRequest`、稳定 validation error |
| HTTP run adapter | `dev:backend/app/private_work/http_runtime.py` | `start_private_run()`，只准入不执行 |
| Context | `dev:backend/app/projects/context.py` | `resolve_project_context()` |
| Private context | `dev:backend/app/private_work/context.py` | issued-only `PrivateWorkContext` |
| Thread service | `dev:backend/app/private_work/thread_service.py` | 项目 Thread 事务边界 |
| Thread repository | `dev:backend/app/private_work/thread_repository.py` | scope 查询、版本更新 |
| Chat controls | `dev:backend/app/private_work/chat_controls.py` | Goal/compact/branch/regenerate/suggest |
| Admission | `dev:backend/app/private_work/run_admission.py` | Run+snapshot+job+quota+audit 原子准入 |
| Run service | `dev:backend/app/private_work/run_service.py` | scoped get/list/cancel/delete |
| Run repository | `dev:backend/app/private_work/run_repository.py` | PostgreSQL Run 记录 |
| Checkpointer | `dev:backend/app/private_work/checkpointer.py` | scope marker、线程锁、读写校验 |
| Event store | `dev:backend/packages/harness/deerflow/runtime/events/store/base.py`、`dev:backend/packages/harness/deerflow/runtime/events/store/db.py` | durable message/event |
| SSE | `dev:backend/packages/harness/deerflow/runtime/events/stream.py` | `PostgresStreamBridge`、cursor |
| Worker | `dev:backend/app/worker/service.py` | Job claim、lease、唯一 graph executor |
| 错误 | `dev:backend/app/private_work/error_mapping.py` | 稳定 `{code,message,request_id}` |

## 3. 两条 Gateway 运行链不能混合

### 3.1 `main` 的链

```text
POST /api/threads/{thread}/runs[/stream]
  -> RunCreateRequest
  -> services.build_run_config()
  -> services.start_run()
  -> Gateway RunManager 创建/登记 Run
  -> Gateway asyncio task 执行 graph
  -> MemoryStreamBridge / RedisStreamBridge
  -> Gateway SSE consumer
```

取消、活跃 Run 冲突、checkpoint 写锁和 orphan recovery 都围绕“Gateway 进程拥有执行任务”
建立。`8a78c264`、`3c8b82c5`、`c7538cfb` 是这套模型中的重要修复，但不是可以放进
`dev` 的新组件。

### 3.2 `dev` 的链

```text
HTTP /api/projects/{project_id}/private-work/...
  -> authenticated user
  -> resolve_project_context(project_id, user_id)
  -> PrivateWorkContext.from_project()
  -> strict request + strip client authority
  -> start_private_run()
  -> PrivateRunAdmissionService.admit()
     -> project/membership FOR UPDATE
     -> optional inbound connection/conversation FOR UPDATE
     -> Thread FOR UPDATE
     -> exact Agent/Skill/MCP snapshot
     -> Run + Job + quota + audit in one transaction
  -> 返回 store_only RunRecord
  -> 独立 Worker claim PostgreSQL Job lease
  -> Worker 再校验 scope/capability/asset closure
  -> Worker-only graph execution
  -> RunEventStore 先持久化 frame，再通知
  -> Gateway PostgresStreamBridge 回放
```

`start_private_run()` 明确返回 `store_only=True`。它组装 `PrivateRunCreate`、去除
`context.private_scope` 后持久化、redact config secrets，然后调用 admission。
没有任何 `create_task(run_agent)`。

## 4. 请求、权限和错误契约

### 4.1 Strict private request

`PrivateRunCreateRequest` 使用 `extra="forbid"`，接受：

```py
assistant_id: str | None
input: dict | list | str | None
command: dict | None
config: dict
context: dict
metadata: dict
multitask_strategy: Literal["reject", "interrupt", "rollback"]
checkpoint: PrivateRunCheckpoint | None
on_disconnect: Literal["cancel", "continue"]
stream_mode: list[PrivateRunStreamMode]
stream_resumable: bool
stream_subgraphs: bool
```

但服务端只允许最终准入为 `multitask_strategy == "reject"`。`strip_client_authority_fields()`
递归移除 project、owner、role、capability、Agent/Skill/MCP、private scope 等字段。
图的 `input` 和 `command` 可以合法出现 `role/user_id/project_id` 作为业务数据；
admission 在保留其形状的同时，绝不把它们当 authority。

### 4.2 Project scope

所有 private repository 查询至少带：

```text
project_id + owner_user_id
```

Thread、Run、checkpoint、message、event、file、artifact 和 feedback 都不能退化成仅
`thread_id` 或 `run_id` 查询。

权限错误语义：

- 非成员、旧 membership、错误 owner、错误 scope：公共 404；
- 当前成员但缺 capability：403；
- 并发/版本/active Run 冲突：409；
- quota：429；
- PostgreSQL/依赖暂时不可用：503；
- 429/503 附 `Retry-After: 1`。

`PrivateWorkRoute` 把 Pydantic validation failure 也映射成相同私有错误结构，不泄漏内部
字段路径或异常文本。

### 4.3 Checkpoint scope

`ProjectScopedCheckpointer.for_context(context)` 只接受 issued context，并在 metadata 注入：

```json
{
  "deerflow_private_scope": {
    "project_id": "...",
    "owner_user_id": "..."
  }
}
```

读回时 `_validate_marker()` 同时验证：

- 请求 Thread ID；
- persisted marker；
- 当前 project/owner；
- capability 和 membership 仍有效。

移植 replay 算法时只能在这个 wrapper 内读父 checkpoint，不能用 `main` 的裸 saver。

## 5. Run admission、并发和幂等

### 5.1 `PrivateRunAdmissionService.admit()`

签名：

```py
async def admit(
    context: PrivateWorkContext,
    thread_id: str,
    request: PrivateRunCreate,
    *,
    server_context: PrivateRunAdmissionServerContext | None = None,
) -> AdmittedPrivateRun
```

关键不变量：

1. `require_issued_private_work_context(context)`；
2. request 必须是精确 `PrivateRunCreate` 类型；
3. strategy 必须是 `reject`；
4. 固定锁序：Project/Membership → inbound authority → Thread → Run/Job/Snapshot/Quota/Audit；
5. 同 `run_id` 只在请求、Thread、metadata、kwargs 完全一致且 Job 存在时幂等返回；
6. 同 Thread 已有 pending/running/finalizing Run 时冲突；
7. admitted snapshot 是 exact immutable closure；
8. 事务成功后才允许 Worker 看到 Job。

这比 `main` 的 Gateway 内 `RunManager.create_or_reject()` 更强，也更适合多进程/多 Pod。

### 5.2 Cancel/delete

`PrivateRunService` 在 project/owner scope 下重新取锁和 revalidate：

- cancel 写持久 Job cancel request，Worker 在 lease 边界观察；
- delete 只允许 terminal Run；
- quota settlement、audit 和终态必须保持事务一致；
- Gateway 不直接取消另一个进程中的 asyncio task。

因此 `main@8a78c264` 的“跨 live Gateway workers 取消”是旧架构修复，不能覆盖此链。

## 6. Durable SSE 和 replay

### 6.1 `dev` 当前协议

`private_work.py` 中：

- `_durable_private_sse_consumer()` 从 `RunEventStore` 分页；
- `parse_stream_cursor()` 解析 `Last-Event-ID`；
- cursor 必须是规范非负/正数语义，不能接受模糊字符串；
- 每个查询都绑定 project、owner、thread、run；
- 超出保留窗口返回明确 out-of-range 错误；
- terminal frame 持久化后，重连仍能结束；
- Gateway restart 不丢历史。

前端按 event ID 单调消费。这里已经覆盖 `main` 的大部分“replay gap”目标，而且比
Memory/Redis stream 的 transient history 更强。

### 6.2 `main@1cd5dea3` 的适用性

`1cd5dea3` 为旧 StreamBridge 增加 replay history gap 信号。不能直接移植 store 或桥接器；
应该做的是确认 `dev` 现有以下测试持续通过：

- cursor 在保留窗口之前；
- cursor 大于 high watermark；
- cursor 重复/回退；
- terminal 后重连；
- Gateway restart；
- wrong project/owner/thread。

若这些已通过，该提交属于“dev 已由不同架构覆盖”，不是待移植项。

## 7. Replay prepare：已确认的缺口

### 7.1 当前 regenerate

`ProjectChatControlService.prepare_regenerate()`：

1. `_validate_control_authority(... reject_incomplete_run=True)`；
2. 读 project-scoped latest checkpoint；
3. 要求目标是 latest visible AI；
4. 找前一条 visible Human；
5. `_find_checkpoint_before_message()` 从 history 反转后按时间找前态；
6. 从 scoped `RunEventStore.list_messages()` 找 authoritative target Run；
7. 返回 input/checkpoint/regenerate metadata。

它已经保证项目、owner、capability、active Run 和来源 durable event。

### 7.2 已确认问题与待复现边界

`_find_checkpoint_before_message()` 当前按全局时间顺序扫描，不读取 `parent_config`，
也不跳过 pending-task checkpoint。这是**确认的实现差距**：算法本身不能区分同 Thread
的 sibling checkpoint branch。

但“当前生产数据一定会重复消息或选错 base”仍需要针对 `dev` checkpointer 的可复现测试。
准确表述应是：

- 已确认缺少 lineage/settled 安全条件；
- sibling branch 和 mid-run checkpoint 下的具体错误结果需测试证明；
- 在没有复现前，不把所有 regenerate 都宣称为已损坏。

### 7.3 `main` 最终 lineage 算法

`find_checkpoint_before_message(accessor, head, message_id, max_depth)`：

1. 先确认 target 在当前 head；
2. 从 `head.parent_config` 开始逐父读取；
3. 校验请求的 parent identity 与实际读取 identity 一致；
4. visited set 拒绝 cycle；
5. 不可寻址 parent 拒绝 dangling link；
6. 跳过 `runtime_run_duration` 的 duration-only checkpoint；
7. `checkpoint.next` 非空表示仍有 pending tasks，不能作为 replay base；
8. 找到不含目标消息且 settled 的第一祖先；
9. 只有 legacy checkpoint 缺少 parent link 时才允许 chronological fallback。

移植落点：

- 在 `dev:backend/app/private_work/` 新增不依赖 legacy Gateway 的 lineage helper；
- accessor 必须是 `_ScopedCheckpointSaver`；
- `ProjectChatControlService.prepare_regenerate()` 和未来
  `prepare_edit_regenerate()` 共同调用；
- max-depth 同时考虑 duration-only checkpoint；
- 完整保留 cycle/dangling/pending-task 测试。

### 7.4 Edit-and-rerun

当前 `dev` 路由只有：

```text
POST /api/projects/{project_id}/private-work/
     threads/{thread_id}/runs/regenerate/prepare
```

没有 `edit-regenerate/prepare` 请求/响应、service 方法和 visibility metadata。这是确认缺失。
具体契约和前端状态见 `08-frontend.md`。

服务端必须落在：

- `private_work.py` strict route；
- `ProjectChatControlService.prepare_edit_regenerate()`；
- `ProjectScopedCheckpointer`；
- `RunEventStore`/`PrivateRunRepository` scoped source Run；
- `PrivateRunAdmissionService` 的正常 submit 链。

不能在 prepare 中直接写历史；prepare 只返回一个经过授权、可重放的输入和 checkpoint，
真正新 Run 仍通过 admission。

## 8. Run duration：确认的端到端缺口

`main` 最终使用：

```py
compute_run_durations(runs) -> dict[run_id, seconds]
stamp_turn_duration_on_last_ai(messages, durations)
```

并在 thread messages、run messages、history 等 projection 上统一注入。duration 是
Run `updated_at - created_at` 的墙钟时间，每个 Run 只放在最后一条可见 AI 上。

当前 `dev` 有 Run timestamps 和前端 helper，但项目私有 messages/history API 没有等价
的统一 projection。确认缺口是“刷新后项目历史没有稳定权威 duration”；不是一定要新增
数据库列。

建议：

1. 只对 terminal、可解释的 Run 计算；
2. project/owner scoped 读取 Run；
3. `list_private_run_messages()`、`list_private_thread_messages()`、thread state/history 保持一致；
4. 不修改底层 event row；
5. 若需要 checkpoint duration-only 写入，必须持 Thread 锁并用 scoped saver；
6. 文案不能叫“思考时间”。

## 9. `main` 其他 Gateway 修复：确认与待审计分开

| `main` 提交 | 旧问题 | 当前 `dev` 证据 | 分类 |
| --- | --- | --- | --- |
| `a0acdda1` | create thread insert race 时保持幂等 | `PrivateThreadRepository.create()` 把唯一冲突映射为 409；当前 create contract 是显式 ID + 非幂等冲突 | **产品契约/待并发复现，不是确认缺陷** |
| `5ce3cecf` | concurrent metadata merge 丢字段 | `PrivateThreadRepository.patch()` 使用 `expected_version` 条件更新；当前公开 patch 只改 display name | **已由 optimistic version 避免同类 silent merge；新增 metadata patch 前需测试** |
| `0f088033` | header trace 应压过 metadata trace | `TraceMiddleware` 只从请求 `X-Trace-Id` 建立 server context；private run config 不把 metadata 当 trace authority | **当前链不同，待 trace-to-job 端到端审计** |
| `e89edb39` | 非正数 read limit | private routes 已有 `ge=1/le=...`，offset/cursor 有 `ge=0` | **已覆盖已知路由；新增路由继续使用 strict bounds** |
| `1cd5dea3` | transient stream replay gap | `dev` 是 PostgreSQL durable stream、高水位和 out-of-range | **架构替代** |
| `8a78c264` | 跨 Gateway 进程取消 | `dev` cancel 走持久 Job/Worker lease | **架构替代** |
| `3c8b82c5` | graph 与控制写 checkpoint 竞态 | `dev` Worker-only + ProjectScopedCheckpointer + Thread lock | **不同实现已覆盖；新增 replay 写仍须守锁** |
| `c7538cfb` | lease recovery 后 orphan stream 不终结 | `dev` Worker/job finalization 与 durable end frame | **不同实现，保留 recovery gate 验证** |
| `9a43d827` | replay 从未 settled/错误 branch checkpoint 开始 | 当前 chronological scan 缺少该条件 | **确认缺口，移植算法** |
| `e56481d9` | duration 在消息 API 重复/缺失 | 当前项目投影不完整 | **确认缺口，移植行为** |

这张表是本模块的缺陷分界。没有复现或源码证据的项目不应写进 bug 清单。

## 10. 关键提交演化

| 提交 | 日期 | 作用 |
| --- | --- | --- |
| `a0acdda1` | 2026-07-15 | create-thread insert race 幂等 |
| `e89edb39` | 2026-07-18 | read limit 必须为正 |
| `0f088033` | 2026-07-19 | header trace precedence |
| `1cd5dea3` | 2026-07-27 | transient stream gap 明确信号 |
| `5ce3cecf` | 2026-07-27 | 并发 thread metadata 合并 |
| `8a78c264` | 2026-07-28 | 旧架构跨 Gateway worker 取消 |
| `9a43d827` | 2026-07-28 | settled checkpoint lineage |
| `c7538cfb` | 2026-07-24 | 旧 lease recovery 后 orphan stream 终态 |
| `3c8b82c5` | 2026-07-25 | 旧 graph/control checkpoint 写串行化 |
| `e56481d9` | 2026-07-29 | 每 Run 一次 duration projection |

应按最终 patch 的不变量移植，不按时间逐个 cherry-pick；后续提交已经修正前序实现。

## 11. 精确可移植落点

### 11.1 第一优先级

1. 在 `app/private_work/checkpoint_lineage.py` 实现 scoped lineage helper；
2. 更新 `ProjectChatControlService.prepare_regenerate()`；
3. 增加 strict project-scoped edit prepare route/service；
4. 在 project message/history projection 注入一次 Run duration；
5. 为以上三项补项目/owner/错误映射测试。

### 11.2 第二优先级审计

1. 追踪 `X-Trace-Id -> Run/Job metadata -> Worker log/audit`，确认没有 client metadata 覆盖；
2. 并发两次相同 Thread create，明确 201/409 还是幂等 200 的产品契约；
3. 并发 rename/未来 metadata patch，确认 expected_version 的 409 和前端重试；
4. 检查所有新增 list endpoint 都有 `ge=1` 和上限。

### 11.3 不需要移植

- Memory/Redis StreamBridge；
- Gateway task registry；
- Gateway-owned graph execution；
- live Gateway worker cancel；
- legacy RunManager lease；
- legacy user-only owner check；
- `/api/threads` 全局路由。

## 12. 禁止直接合并

以下文件或机制禁止整块覆盖 `dev`：

- `main:backend/app/gateway/services.py` 的 `start_run()`；
- `main:backend/app/gateway/routers/thread_runs.py` 整个 router；
- `main:backend/packages/harness/deerflow/runtime/runs/manager.py`；
- `main` Memory/Redis StreamBridge；
- 旧 migrations；
- 不带 `project_id + owner_user_id` 的 repository 查询；
- 通过客户端 config/context 恢复 project/role/capability；
- Gateway 内 `asyncio.create_task()` 执行 Agent graph。

可以提取的仅是：

- checkpoint lineage 纯算法；
- edit prepare 的业务校验和 metadata 合同；
- duration 归属和投影算法；
- input bounds、trace precedence、并发测试思路。

## 13. 测试与契约

### 13.1 `main` 证据

- `backend/tests/test_thread_regenerate_prepare.py`
- `backend/tests/test_checkpoint_lineage.py`
- `backend/tests/test_thread_run_messages_pagination.py`
- `backend/tests/test_thread_run_query_validation.py`
- `backend/tests/test_threads_router.py`
- `backend/tests/test_thread_meta_repo.py`
- `backend/tests/test_trace_middleware.py`
- `backend/tests/test_multi_worker_run_ownership.py`
- `backend/tests/test_gateway_run_recovery.py`

这些测试可提供行为样本，但其 fixture 的 legacy user/RunManager/StreamBridge 不能直接复制。

### 13.2 当前 `dev` 基础

- `backend/tests/test_private_work_router.py`
- `backend/tests/test_private_work_run_router.py`
- `backend/tests/test_private_work_stream_router.py`
- `backend/tests/test_private_work_chat_controls.py`
- `backend/tests/test_private_thread_service.py`
- `backend/tests/test_private_thread_repository.py`
- `backend/tests/test_private_work_context.py`
- `backend/tests/test_private_run_authorization.py`
- `backend/tests/test_m6_private_run_gateway.py`
- `backend/tests/integration/test_m4_private_work_postgres.py`
- `backend/tests/test_trace_middleware.py`

## 14. 验证矩阵

| 场景 | 预期结果 |
| --- | --- |
| client 伪造 project/owner/role/capability | 字段被剥离，authority 只来自 issued context |
| 非成员访问存在的 Thread/Run | 404，不能判断资源存在 |
| 成员缺执行 capability | 403 |
| 同 Thread 两个 Run 同时准入 | 一个成功，一个 409；数据库无孤立 Job/Run |
| 同 run_id 同内容重试 | 返回同一 Run/Job |
| 同 run_id 不同内容 | 409 |
| admission 事务中 snapshot/quota/audit 失败 | Run 和 Job 均不残留 |
| Gateway 返回 Run 后崩溃 | Worker 仍可 claim；SSE 可由新 Gateway 回放 |
| Worker lease 丢失 | 后续 side effect 被阻止，终态流程可恢复 |
| cancel 跨 Gateway/Worker 进程 | 持久 cancel 生效，不依赖本地 task |
| Last-Event-ID 重复、回退、越界 | 稳定拒绝/去重，不静默跳帧 |
| 当前 head 存在 sibling checkpoints | replay 只沿 parent lineage |
| parent cycle/dangling | 409，不退回错误 sibling |
| parent 缺失的 legacy checkpoint | 仅此时允许 chronological fallback |
| candidate checkpoint 有 pending tasks | 不可作为 replay base |
| edit 旧轮次/失败 Run/active Goal | 409，不返回可用 checkpoint |
| 每 Run 多条 AI | duration 只投影到最后一条可见 AI |
| list limit=0/-1/超上限 | 统一 private validation error |
| `X-Trace-Id` 与 client metadata 冲突 | server header/生成值胜出，并贯穿 Job/Worker/audit |
| 并发 Thread rename | 一个成功，旧 expected_version 返回 409 |

只有在这组矩阵通过后，才能说某个 `main` 行为已经按 `dev` 架构移植；仅让 legacy
`/api/threads` 测试通过不构成验收。
