# M5 项目自动化与持久化调度专项设计

- 日期：2026-07-16
- 状态：Release candidate，等待 Task 18 独立终审
- 对应总体设计：`2026-07-12-project-first-saas-design.md`
- 对应实施计划：`../plans/2026-07-16-project-automation-m5.md`
- 前置里程碑：M1、M2、M3、M4 已完成
- 里程碑：M5 — 自动化项目化与持久化任务

## 1. 文档目的

本文档定义 DeerFlow M5 的产品边界、权限模型、持久化模型、调度语义、迁移方式、前端入口和验收门禁。

M5 不是从零建设定时任务。仓库已经存在 legacy `scheduled_tasks`、`scheduled_task_runs`、
`ScheduledTaskService`、`/api/scheduled-tasks` 和 `/workspace/scheduled-tasks`。这些实现目前只有
`user_id` 作用域，通过 legacy `start_run()` 启动；M4 cutover 后 shared `start_run()` 已 fail closed，
因此它们不能成为项目 SaaS 的自动化 authority。

M5 release candidate 已把现有 MVP 收敛为项目与 owner 双重隔离的 automation，并复用 M4 已完成的私有 Thread、run、
asset snapshot、credential materialization、文件 authority、Memory 和授权取消链路。M5 完成后，项目
成员可以创建、暂停、恢复、手动触发并查看自己的自动化；Scheduler 重启后，schedule definition、
执行 occurrence 和 terminal outcome 仍然存在。

当前候选实现已完成 final schema、scoped repositories、occurrence-before-admission、M4 private run
dispatch、crash reconciliation、project API/UI、显式 migration 与全栈门禁实现。只有 Task 18 的 fresh
总门禁和独立终审均通过后才能把本里程碑标记为完成；此状态不代表完整多用户 SaaS 可发布。

## 2. 决策优先级与 M6 边界

本规格采用用户于 2026-07-16 确认的方案 A：

- M5 交付 project/owner-scoped automation、持久化 occurrence、Scheduler claim/lease、权限重校验、
  M4 private run 复用、项目 API/UI 和 legacy automation staged migration。
- 通用 `jobs`、`job_attempts`、`dead_jobs`、独立 Worker execution lease、跨 Worker stream ownership、
  持久化 SSE reconnect、通用至少一次投递和 dead-letter 运维属于 M6。
- M5 的 Scheduler 仍是 Gateway 生命周期内的 background service；`scheduler.enabled` 只控制自动轮询。
  M6 才把 Scheduler/Worker 进程边界和运行接管做成最终形态。

该边界以根 `AGENTS.md` 和总体设计第 19 节的最新口径为准，并覆盖 M4 专项规格第 3.2 节中把通用
jobs/Worker lease 暂列为 M5 的旧说明。M5 计划必须同步修正文档，避免后续实施者再次扩大范围。

## 3. 已冻结决策

1. Automation 是 `project_id + owner_user_id` 私有资源，不在项目成员间共享。
2. Admin、Editor、Runner 通过 `automation.manage_own` 管理自己的 automation；Viewer 只能读取自己的
   definition 和 run history，不能创建、修改、暂停、恢复、触发或删除 automation。
3. Automation 始终以 owner 用户主体运行，不使用项目 service account 或 `system_admin` override。
4. 每次触发前重新解析当前 ProjectContext，并同时要求 `automation.manage_own`、
   `private_work.create` 和 `shared_assets.execute`。
5. 客户端不能提供可信 project、owner、membership、role、capability、asset version、credential grant
   或 `non_interactive` 标记。
6. 自动触发和手动触发都必须复用 `start_private_run()` 的 admission、snapshot 和 runtime 链，禁止新增
   第二套 Agent executor。
7. Scheduler 内部注入 `context.non_interactive=true`；公共 HTTP、IM 和普通 project run 请求中的同名
   字段必须丢弃。
8. Automation 只支持现有 `once` 和五字段 `cron`；时区继续使用 IANA timezone。
9. V1 固定 overlap policy 为 `skip`，不提供 queue/parallel/replace UI。
10. Cron downtime misfire 固定 coalesce：恢复后至多产生一个 overdue occurrence，然后把
    `next_run_at` 推进到当前时间之后，不突发补跑全部历史 tick。
11. `once` 在 Scheduler 停机期间过期后仍产生一个 overdue occurrence；成功、失败或 interrupted 后均不
    自动重放。
12. Scheduler 在启动 Agent run 之前先持久化 occurrence；同一 scheduled tick 或同一 manual
    idempotency key 最多只有一条 occurrence row。
13. M5 不承诺 Agent side effect exactly-once。run admission 已发生后遇到进程崩溃，occurrence 记为
    `interrupted`，不自动重放可能已经执行过副作用的 run。
14. 只有在 M4 run row 尚不存在时，过期的 `launching` lease 才能安全回到 `queued` 并重试 admission。
15. Scheduler 自动轮询使用 PostgreSQL transaction lock、`FOR UPDATE SKIP LOCKED` 和 occurrence lease；
    PostgreSQL 是唯一权威状态。
16. `scheduler.max_concurrent_runs` 对 scheduled/manual automation 的 `queued/launching/running`
    occurrence 共同生效；即使误启动多个 poller，admission 也通过同一数据库 transaction lock 串行计数。
    M5 的受支持运行拓扑仍是单 Gateway，多 Gateway runtime ownership 属于 M6。
17. `scheduler.enabled=false` 只停止自动 poll，不删除、不暂停或改写 definition；项目页面显示明确的
    operator-disabled 状态，手动 trigger 在 Gateway runtime ready 时仍可用。
18. `fresh_thread_per_run` 每次创建确定性 project-private Thread；`reuse_thread` 只能引用同项目、同 owner
    的现有 active Thread。
19. 每个 automation 持久化逻辑 Agent 引用 `agent_asset_id + agent_scope`；每次 run admission 解析当时可用
    的 published/bound closure，并由 M4 保存精确 version/grant snapshot。
20. 更新 automation 不回写历史 occurrence；queued occurrence 在更新事务中取消，launching/running
    occurrence 存在时更新、删除、暂停返回稳定 `409`。
21. Automation mutation 使用 `version` 乐观并发；不存在“最后写入获胜”。
22. 成员退出/移除、降级为 Viewer、项目 suspended 或 pending deletion 时，停止新 occurrence admission、
    取消 queued occurrence、冻结 definition，并沿用 M4 cancellation marker 终止已失权 active run。
23. 30 天内重新加入时 definition 可解冻但保持 paused，不自动恢复执行；物理清理仍不属于 M5。
24. Automation title、prompt、Thread 内容和 run output 都是私有内容，不进入治理审计、普通日志或迁移摘要。
25. Legacy automation migration 使用 maintenance window、显式 owner/project/agent map、dry-run、幂等
    ledger、final constraint 和 singleton cutover marker，不长期双写。
26. Legacy automation source 只有 PostgreSQL `scheduled_tasks` 和 `scheduled_task_runs`；M5 不扫描
    filesystem、Memory 或 connection source。
27. M5 project API cutover 后 legacy `/api/scheduled-tasks*` 和 legacy automation UI 保留到 M7，但统一
    fail closed，不得猜 default project。
28. Project automation 前端 query key 和 runtime link 同时按 account 与 project 隔离；scope 切换先
    cancel 再 clear。
29. Static demo 不暴露 project automation 入口，也不发 project automation 请求。
30. M5 完成只把总体进度更新为 5/8；M6–M8 未完成时仍不得把系统描述为完整多用户 SaaS。

## 4. 目标与非目标

### 4.1 目标

- 为 scheduled task definition 和 occurrence 建立非空 project/owner scope、复合外键和作用域仓储。
- 把 automatic/manual trigger 接入 M4 private run admission，并保持 non-interactive 运行。
- 在 run admission 前重新验证 membership、role/capability、project state、Thread 和 Agent availability。
- 为 scheduled tick 建立原子 occurrence reservation、幂等键、launch lease、重启 reconciliation 和 terminal
  history。
- 提供 project-scoped list/create/read/update/pause/resume/trigger/delete/history API。
- 在 `/projects/{project_slug}/automations` 提供 account/project-scoped 页面和项目菜单入口。
- 迁移已有 legacy automation 到显式 owner/project/agent scope，并关闭 legacy authority。
- 交付真实 PostgreSQL isolation、claim race、migration 和 Frontend cache isolation 门禁。

### 4.2 非目标

- 通用 jobs、job attempt、dead job、Worker heartbeat 或 dead-letter UI。
- 独立 Worker/Scheduler 进程拓扑和跨进程 run ownership。
- Agent run 在进程崩溃后的自动重放。
- 持久化 SSE reconnect、`Last-Event-ID` 或跨 Worker stream takeover。
- 配额、usage ledger、治理审计查询或平台运营面板。
- 通用数据库备份恢复、删除墓碑重放或灾难恢复演练。
- 物理清理冻结/软删除 automation 数据。
- 自定义 overlap/misfire/retry policy、秒级 cron、日历规则或 webhook trigger。
- 项目成员共享 automation、项目 service account 或跨项目复制。
- 邮件、Slack 等通知投递。
- 删除 legacy route/source；物理删除属于 M7。

## 5. 当前实现与目标结构

### 5.1 实施前 legacy 基线

```text
/workspace/scheduled-tasks
  -> /api/scheduled-tasks
  -> ScheduledTaskRepository(user_id)
  -> ScheduledTaskService embedded in Gateway
  -> launch_scheduled_thread_run()
  -> legacy start_run()
```

基线缺口：

- definition 只有 `user_id`，没有 project、membership、Agent asset 或复合约束；
- run history 没有 project/owner，父子关联没有外键；
- Scheduler 通过 legacy `start_run()` 执行，M4 cutover 后 fail closed；
- task row lease、occurrence create、run admission 和 `next_run_at` 推进不在一个可靠状态机内；
- crash after launch window 可能重复或丢失 bookkeeping；
- Frontend query key 是全局 `['scheduled-tasks']`，不能隔离 account/project；
- Viewer 与 project capability 没有进入 API/UI；
- legacy task 的 fresh-thread Agent target 不明确。

### 5.2 M5 目标路径

```text
Project page / project API
  -> server-issued PrivateWorkContext
  -> ProjectAutomationService
  -> scoped repositories
  -> PostgreSQL scheduled_tasks + scheduled_task_runs

Embedded Scheduler poll
  -> reserve due occurrence transactionally
  -> claim queued occurrence lease
  -> re-resolve ProjectContext for owner
  -> create/reuse project-private Thread
  -> start_private_run(non_interactive, deterministic run_id)
  -> M4 run/checkpoint/event/file/Memory authority
  -> scoped completion reconciliation
```

## 6. Authority、上下文与模块边界

### 6.1 HTTP authority

Project automation router 位于：

```text
/api/projects/{project_id}/automations
```

Router 使用 M4 已有的 `private_work_context` dependency 解析 server-issued `PrivateWorkContext`。读取方法要求
`private_work.read_own`；mutation 额外要求 `automation.manage_own`。create/resume/manual trigger 在业务事务中
同时重校验 `private_work.create` 和 `shared_assets.execute`。

Router 不直接持有 session、不按裸 task ID 查询，也不接受 body 中的 owner/project/member/capability。

### 6.2 Scheduler authority

Scheduler 从 occurrence parent row 读取 server-persisted `project_id + owner_user_id`，然后在同一数据库事务中：

1. 加载 project 和 current membership；
2. 通过 `resolve_project_context_in_transaction()` 生成真实 `ProjectContext`；
3. 转换为 issued `PrivateWorkContext`；
4. 要求三个执行 capability；
5. 验证 definition version、frozen/deleted/status、Thread 和 Agent target；
6. 记录本次 resolved `membership_id + membership_version`。

任何一步失败都产生 scoped terminal occurrence，不 fallback 到 default project、internal default user 或 legacy
Thread。

### 6.3 模块职责

- `deerflow.persistence.scheduled_tasks`：final ORM、scope predicate 和 definition repository。
- `deerflow.persistence.scheduled_task_runs`：occurrence repository、claim/lease、idempotency 和 history。
- `app.automations.service`：用户 CRUD、schedule validation、state transition 和 capability revalidation。
- `app.automations.dispatcher`：occurrence reservation/claim、Thread preparation、private run launch 和 completion。
- `app.automations.cutover`：schema/marker readiness 以及 legacy/project route guard。
- `app.gateway.routers.project_automations`：strict HTTP contract 和错误映射。
- `app.scheduler.service`：poll lifecycle；只调用 dispatcher，不包含 HTTP 或 legacy authorization。

Harness 不能 import `app.*`。Schedule calculation 可以继续位于 harness；ProjectContext、private run launch 和
cutover orchestration 必须留在 app 层。

## 7. Final PostgreSQL 模型

M5 使用线性 staged revisions：

- `0012_project_automation_expand`
- `0013_project_automation_finalize`

### 7.1 `scheduled_tasks`

保留现有 string ID 以无损迁移 legacy URL/reference；新 ID 继续使用 `task-<uuidhex>`。final columns：

- `id varchar(64)` primary key；
- `project_id uuid not null`；
- `owner_user_id varchar(36) not null`，替代 `user_id`；
- `thread_id varchar(64) null`；
- `context_mode varchar(32) not null`；
- `agent_asset_id uuid not null`；
- `agent_scope varchar(16) not null`；
- `title varchar(255) not null`；
- `prompt text not null`；
- `schedule_type varchar(16) not null`；
- `schedule_spec json not null`；
- `timezone varchar(64) not null`；
- `status varchar(16) not null`；
- `overlap_policy varchar(16) not null`，M5 只允许 `skip`；
- `next_run_at timestamptz null`；
- `last_run_at timestamptz null`；
- `last_outcome varchar(24) null`；
- `last_error_code varchar(64) null`；
- `run_count bigint not null default 0`；
- `version bigint not null default 1`；
- `frozen_at timestamptz null`；
- `deleted_at timestamptz null`；
- `created_at/updated_at timestamptz not null`。

约束：

- unique `(project_id, owner_user_id, id)`；
- FK project、owner 和 `(project_id, owner_user_id)` active/history membership identity；
- optional composite FK `(project_id, owner_user_id, thread_id)` 到 `threads_meta`；
- FK `(agent_asset_id, agent_scope)` 到共享 Agent identity；
- `reuse_thread` 必须有 thread，`fresh_thread_per_run` 的 thread 必须为空；
- `agent_scope in ('system','project')`；
- schedule/context/status/overlap allowlist check；
- `version >= 1`、`run_count >= 0`。

Task row 不再保存裸 `last_run_id/last_thread_id`。最近结果从 scoped occurrence history 读取，避免未约束的
跨项目指针。

### 7.2 `scheduled_task_runs`

该表表示 durable occurrence，不是 M6 generic job attempt：

- `id varchar(64)` primary key；
- `project_id uuid not null`；
- `owner_user_id varchar(36) not null`；
- `task_id varchar(64) not null`；
- `task_version bigint not null`；
- `occurrence_key char(64) not null`；
- `manual_idempotency_hash char(64) null`；
- `scheduled_for timestamptz not null`；
- `trigger varchar(16) not null`；
- `status varchar(20) not null`；
- `thread_id varchar(64) null`；
- `run_id varchar(64) null`；
- `resolved_membership_id uuid null`；
- `resolved_membership_version bigint null`；
- `launch_attempt_count integer not null default 0`；
- `lease_owner varchar(128) null`；
- `lease_expires_at timestamptz null`；
- `next_attempt_at timestamptz null`；
- `error_code varchar(64) null`；
- `error_message text null`，只允许安全公共文案；
- `started_at/finished_at/created_at/updated_at timestamptz`。

约束：

- composite FK `(project_id, owner_user_id, task_id)` 到 parent task；
- optional composite FK 到 private Thread；
- `run_id` 非空时，完整 composite FK `(project_id, owner_user_id, thread_id, run_id)` 到 M4 runs；
- unique `(project_id, owner_user_id, task_id, occurrence_key)`；
- manual idempotency hash 的 scoped partial unique；
- `run_id is null or thread_id is not null`；
- status/trigger allowlist、non-negative attempts 和 membership version check；
- active occurrence partial index覆盖 `queued/launching/running` 查询；
- history list index为 `(project_id, owner_user_id, task_id, created_at desc, id desc)`。

Occurrence statuses：

```text
queued -> launching -> running -> success | failed | interrupted
   |          |
   |          +-> queued       # 仅 run row 尚不存在且 lease 过期
   +-> skipped | cancelled | rejected
```

### 7.3 Migration control tables

- `automation_migration_runs`
- `automation_migration_ledger`
- `automation_cutover_state` singleton

Ledger domains 固定为 `scheduled_tasks` 和 `scheduled_task_runs`。Cutover stages 固定为
`empty_install -> migration_ready -> cutover_complete`；`cutover_complete` 必须同时具备 source probe、ledger、
final schema probe 和完成时间。

### 7.4 M4 guard 对后续 revision 的兼容

当前 `PrivateWorkCutoverGuard` 把 Alembic current revision 精确等于 `0011_private_artifact_tombstone` 作为
开放条件。M5 head 前进到 `0013` 后不能因此重新关闭 M4 API。

M5 必须把 revision 判断改为“current revision 是 required revision 的线性后代”，并在 startup 预计算
revision ancestry；request path 不同步读取 migration files。M4 marker/schema requirement 不降低，未知分支或
无法证明 ancestry 时 fail closed。Automation guard 同样要求 current revision 是 `0013` 或其后代。

## 8. Definition 生命周期与并发

### 8.1 Definition 状态

```text
enabled <-> paused
enabled/paused -> completed | failed | cancelled   # once terminal
any active state + membership/lifecycle revoke -> frozen + paused
delete -> deleted_at set + paused
```

`frozen_at` 和 `deleted_at` 是独立 authority flags。Scheduler 只处理：

- `status='enabled'`
- `frozen_at is null`
- `deleted_at is null`
- `next_run_at is not null`

### 8.2 乐观并发

PATCH、pause、resume 和 delete 都必须携带 `expected_version`。Repository update 的完整 predicate 包含
project、owner、id、version、not deleted。成功后 `version += 1`；失配返回 `409 AUTOMATION_VERSION_CONFLICT`。

Mutation 先锁 task，再检查 occurrence：

- 有 `launching/running` occurrence：update/pause/delete 返回 `409 AUTOMATION_ACTIVE_RUN`；
- 只有 queued occurrence：在同事务把 queued 标记 `cancelled`，再执行 mutation；
- terminal history 不阻止 mutation。

### 8.3 Pause、resume 和 delete

- Pause 清空当前 `next_run_at`，但保留 schedule definition；取消 queued occurrence。
- Resume 从“当前时间”重新计算 next tick；cron 不补跑 pause 期间 tick，once 已过期则返回 `409`。
- Delete 是 soft delete；definition 和 history在 retention/purge 里程碑前继续存在于数据库，但产品列表默认
  不返回。
- Manual trigger 不改变 cron `next_run_at`，也不消费未来 once tick。

## 9. Occurrence reservation、claim 与 misfire

### 9.1 自动 reservation

每次 poll 在 PostgreSQL transaction 内：

1. 获取固定 advisory transaction lock，串行化 global concurrency count 和 reservation；
2. 统计全部 scope 中 `queued/launching/running` occurrence 数量；
3. 计算剩余 `max_concurrent_runs` budget；
4. 使用 `FOR UPDATE SKIP LOCKED` 选择 due definition；
5. 对每个 definition 以原 `next_run_at` 生成 scheduled occurrence key；
6. 同事务 insert occurrence 并推进 parent `next_run_at`；
7. 如果同 task 已有 active occurrence，insert terminal `skipped` history 而不是 launch；
8. commit 后 dispatcher claim queued occurrence。

Cron misfire coalesce 以 poll 时刻为计算基准：只为当前已到期窗口创建一条 occurrence，新的
`next_run_at` 必须严格晚于 poll time。Unique occurrence key 使 poll 重试不产生第二条历史。

### 9.2 Manual reservation

Manual endpoint 要求 UUID `Idempotency-Key` header。服务端只保存 SHA-256 hash，不保存原值。相同 scope、
task 和 key 重试返回同一 occurrence；不同 key 在已有 active occurrence 时返回 `409` 且不创建 skipped
history。

Manual trigger 经过同一个 advisory lock 和 global concurrency admission，不绕过 scheduler cap。

### 9.3 Launch lease

Dispatcher claim queued occurrence时写：

- `status='launching'`
- unique `lease_owner`
- `lease_expires_at`
- incremented `launch_attempt_count`
- deterministic candidate `thread_id/run_id`

Fresh Thread ID 和 run ID 从 occurrence ID 使用固定 UUIDv5 namespace 派生。Retry 使用同一 ID，避免 crash
window 产生另一个 private root。

### 9.4 Restart reconciliation

Scheduler start 时先 reconciliation：

- `launching` lease 过期且 M4 run row 不存在：回到 queued；
- run row terminal：把 terminal outcome复制到 occurrence；
- run row pending/running 且属于重启前的单 Gateway进程：通过 scoped M4 repository把 run和occurrence都
  标记 interrupted，不自动重放；
- occurrence running 但 run row 不存在：标记 failed with `AUTOMATION_RUN_MISSING`；
- terminal occurrence 保持不可逆，不被 startup sweep 降级。

M5 不再使用“启动时把所有 active scheduled_task_runs 一律 interrupted”的 legacy 全表 sweep。

## 10. Private Thread 与 run 启动

### 10.1 Fresh Thread

Dispatcher 使用 server-issued context 和 task 的 Agent ref 调用 M4 `PrivateThreadService`：

- deterministic Thread ID；
- display name 从 task title 生成，不包含 prompt；
- metadata 只包含 automation/task-run ID 和 trigger，不含 project/owner/capability；
- create conflict 时读取同 scope Thread，并验证 Agent ref 与 metadata 匹配；不匹配则 fail closed。

### 10.2 Reuse Thread

Create/update 时验证 Thread 属于同一 project/owner、未 frozen/deleted，并且 persisted Agent ref 与 task ref
一致。Trigger 时再次验证。Thread 正在运行时 scheduled occurrence 按 overlap policy 记为 skipped；manual
trigger 返回 `409`。

### 10.3 Run admission

新增内部 helper `start_scheduled_private_run()`，只做 request construction 和 scheduler-only context 注入，
最终仍调用 `start_private_run()`。M4 run admission 扩展一个 internal-only deterministic `run_id` 参数；公共
API model 不暴露该字段。

Run metadata 只写：

- `scheduled_task_id`
- `scheduled_task_run_id`
- `scheduled_trigger`

Completion callback 不能只信 metadata。它必须以 server-known private scope + run ID 查询有 composite FK 的
occurrence；metadata 仅用于快速定位。任何 scope mismatch 都忽略并记录不含私有内容的安全告警。

### 10.4 Completion

- Agent run success -> occurrence `success`；
- run error/timeout -> `failed`；
- user/authorization/process interrupt -> `interrupted`；
- cron parent 保持 enabled，更新 last outcome；
- once parent变为 completed/failed/cancelled；
- completion update使用 terminal-only compare-and-set，重复 callback 幂等。

## 11. 权限撤销与生命周期

M2 membership/lifecycle transaction 的 post-commit private-work hooks 扩展 automation domain：

- 事务内把该 scope 的 enabled definition 设置 paused + frozen；
- queued occurrence 设置 cancelled；
- launching/running occurrence关联的 M4 run写 authorization cancellation marker；
- local RunManager cancel 仍只是 commit 后 best-effort 加速；
- Scheduler 在 Thread create、run admission 和任何副作用前再次读取 cancellation/membership state。

重新加入 30 天窗口内：

- 清除 frozen_at；
- definition 保持 paused；
- owner 手动 resume 时重新验证 Agent binding、Thread 和 once 时间；
- history 不改变。

Viewer 只通过 read API 查看自己的 definition/history。隐私导出和物理提前删除不在 M5 automation 页面
新增半套能力；后续统一 privacy center 承担。

## 12. Project API contract

Base path：`/api/projects/{project_id}/automations`。

### 12.1 Read

- `GET /readiness`
- `GET /`
- `GET /{task_id}`
- `GET /{task_id}/runs?limit=&offset=`
- `GET /threads/{thread_id}`

Read response 是 strict public model，不返回 owner email、membership internals、lease owner、idempotency hash
或 prompt 以外的 runtime kwargs。Prompt 本身只返回给 owner，Viewer 仍是 owner，因此可读。

### 12.2 Mutation

- `POST /`
- `PATCH /{task_id}`
- `POST /{task_id}/pause`
- `POST /{task_id}/resume`
- `POST /{task_id}/trigger`
- `DELETE /{task_id}`

Create payload 包含 title、prompt、context mode、optional thread、Agent ref、schedule type/spec、timezone。
Mutation payload包含 `expected_version`。Trigger 使用 `Idempotency-Key` header。

### 12.3 Readiness

Readiness response至少包含：

- `status: ready | migration_required | unavailable`
- `scheduler_enabled: boolean`
- `project_private_work_ready: boolean`
- `automation_cutover_ready: boolean`
- stable public `code`

Readiness 是只读解释端点，不因 cutover incomplete 自身被 guard 关闭。

### 12.4 Error semantics

- cross-project/cross-owner/not found：`404 AUTOMATION_NOT_FOUND`；
- current project内缺 capability：`403 AUTOMATION_FORBIDDEN`；
- version/active run/once expired/manual overlap：稳定 `409` code；
- schedule/timezone/Idempotency-Key invalid：`422`；
- global concurrency limit：`429 AUTOMATION_CONCURRENCY_LIMIT`；
- project API尚未 cutover：`409 AUTOMATION_CUTOVER`；
- runtime/database暂不可用：`503 AUTOMATION_UNAVAILABLE`。

错误包含 request ID，不返回 SQL、prompt、title、provider error、credential 或 internal lease owner。

## 13. Frontend

### 13.1 Route 与入口

新增：

```text
/projects/[project_slug]/automations
```

Project shell 只有在以下条件同时满足时显示入口：

- compile-time `PROJECT_AUTOMATION` 已开启；
- M4 private work readiness ready；
- M5 automation readiness ready；
- `private_work.read_own` capability 存在。

Mutation controls 逐项检查 `automation.manage_own`，create/resume/trigger 同时检查 create/execute capability。
UI 不从 role 推导。

### 13.2 Client 与 cache ownership

Project automation 使用独立 `core/project-automations` API/hooks/types，不让 project page fallback 到 legacy
`core/scheduled-tasks`。所有 key 以：

```text
['account', accountId, 'project', projectId, 'automations', ...]
```

开头。Project/account切换时先 cancel，再清除 query、mutation 和 local selection。迟到响应必须在 generation
和 scope 检查后才能 commit。

### 13.3 页面行为

页面复用现有 schedule input、cron parser 和 recipes 的纯展示/校验能力，但拆分 904 行 legacy page，建立可
测试的 project workbench 组件：

- list/filter；
- create/edit dialog；
- pause/resume/manual trigger/delete；
- run history和 private Thread link；
- scheduler disabled banner；
- migration/unavailable retry state；
- Viewer read-only state；
- timezone/once/cron validation；
- active/version conflict刷新。

Chat 内的 scheduled-task link 改为 project route，并从当前 project private access 派生 URL。Legacy workspace
route 在 M5 cutover 后只显示迁移完成/fail-closed说明，直到 M7 删除。

### 13.4 安全

- Prompt 不写 localStorage、URL query、analytics 或 toast；
- error toast 只显示服务端安全 public message；
- static demo 无菜单、无 direct route data request；
- Viewer 不渲染 mutation control；
- direct URL仍依赖服务端 capability和 scope，不把隐藏按钮作为安全边界。

## 14. Staged migration 与 cutover

### 14.1 Owner/Agent map

新增 `make migrate-automations ARGS="..."`。Map 为每个 legacy owner 显式提供：

```json
{
  "<owner-uuid>": {
    "project_id": "<active-project-uuid>",
    "fresh_thread_agent": {
      "asset_id": "<agent-uuid>",
      "scope": "system"
    }
  }
}
```

`reuse_thread` task 从 M4 private Thread派生 Agent ref，并要求 project与 map完全一致；fresh-thread task 使用
`fresh_thread_agent`。目标 owner 必须是该项目 active Admin/Editor/Runner，Agent 必须在项目可执行且 scope
一致。Map不能把同一 owner的 task拆到多个项目；V1不支持跨项目选择式迁移。

### 14.2 Dry-run

Dry-run：

- 只读 source rows；
- 校验 M4 cutover ready、revision、map、membership、Thread/run relation 和 Agent target；
- 输出行数、状态聚合和截断 hash；
- 不输出 task title、prompt、legacy user ID、Thread/run ID 或 schedule payload；
- 发现任一 unmapped owner、跨 scope relation、unsupported status 或 digest conflict 时整体失败。

### 14.3 Execute

维护窗口停止 Gateway、Scheduler、channel/embedded writers 后：

1. 证明 operator 已在仓库外完成数据库备份；M5 CLI 不伪装成通用 backup/restore。
2. 对 `0011` target应用 `0012` expand；新增 nullable scope/control structures，不开放 project API。
3. 锁定 source，重新验证 dry-run fingerprint。
4. 写 migration run/ledger并回填 task scope、Agent ref、version/frozen flags。
5. 按 parent scope迁移 task runs；只保留能够通过 M4 composite relation验证的 Thread/run pointer。
6. 对 legacy skipped/pre-admission history中不存在的随机 Thread ID置空；source fingerprint和转换结果 digest写
   ledger，不把随机 ID暴露到报告。
7. 写 `migration_ready` marker。
8. 应用 `0013` finalize：在任何 destructive DDL前验证 ledger、row count、scope和 relation probe，然后删除
   已由 `0012` 新增并完成回填的 legacy `user_id`、固化 `owner_user_id`，安装
   NOT NULL/check/composite FK/index。
9. 运行 final probe并写 `cutover_complete`。
10. 执行 `make check-db`、M1–M5 PostgreSQL gate 和 Frontend isolation smoke后重新开放服务。

Execute幂等：cutover complete后重跑返回 no-op；expand/staging中断可在相同 source fingerprint/map digest下继续；
fingerprint或 map改变则 fail closed。

### 14.4 Fresh install

Fresh database通过 final ORM `create_all`/Alembic head创建 final schema。只有确认 legacy automation source为空、
M4 marker complete并完成空域 probe后，才写 `empty_install/cutover_complete`。普通 Gateway startup不能猜 map、
自动回填或跨越 staged boundary。

### 14.5 Legacy guard

- M5 project API仅在 final schema + automation marker + M4 marker全部 ready时开放；
- expand后 legacy mutation冻结，防止 inventory漂移；
- cutover后所有 legacy scheduled-task API统一返回 `409 AUTOMATION_CUTOVER`；
- Scheduler在 marker incomplete时不 poll legacy rows；
- M7之前保留代码用于受控错误和回滚窗口，不继续作为 authority。

## 15. 配置与运维

保留现有配置：

- `scheduler.enabled`
- `poll_interval_seconds`
- `lease_seconds`
- `max_concurrent_runs`
- `min_once_delay_seconds`

M5 不增加用户可调 retry/overlap/misfire policy。`lease_seconds` 只覆盖 occurrence admission，不代表 Agent run
execution lease。

运维文档必须说明：

- Scheduler仍嵌入 Gateway；M5受支持拓扑为单 Gateway。数据库 lock/claim可以避免误启动多个poller时重复
  reservation，但不提供多 Gateway runtime ownership或接管；
- 配置关闭时 definition仍可管理，automatic poll停止；
- migration维护窗口、owner/agent map、外部备份证明、dry-run/execute和失败恢复；
- 安全日志只含 task/run count、status、request ID和截断 hash；
- 不能把 M5 occurrence lease描述成 M6 Worker可靠队列。

## 16. 测试与发布门禁

### 16.1 Backend unit

- schedule/timezone/once/cron/coalesce；
- definition state machine和version conflict；
- owner/project scope predicates覆盖 list/get/search/update/delete/history；
- manual idempotency；
- scheduled occurrence unique reservation；
- overlap skip；
- claim lease expiry和terminal irreversibility；
- deterministic Thread/run IDs；
- client `non_interactive`丢弃、scheduler-only注入；
- completion scope validation；
- scheduler-disabled行为；
- M4 revision ancestry guard。

### 16.2 Real PostgreSQL

至少使用两个项目、同项目两名成员和一名项目外用户，覆盖：

- cross-project/cross-owner list、ID probe、pagination、mutation和history均404；
- Viewer read-only；Admin不能读取其他成员 automation；system_admin无 private override；
- composite FK拒绝跨 scope task/Thread/run关系；
- 两个 scheduler并发 claim同一 tick只产生一条 occurrence；
- concurrent manual同 idempotency key返回同一 occurrence；
- global cap在并发 transaction下不超限；
- membership downgrade/removal/pending deletion冻结并取消；
- fresh/reuse Thread run通过 M4 exact asset snapshot；
- restart reconciliation不重复 admission后 run；
- `0011 -> 0012 -> 0013` migration、幂等重跑、fingerprint/map conflict和 fail-before-DDL；
- M1–M4 gate在 head前进后保持通过。

新增固定 CI 文件：

- `backend/tests/integration/test_m5_project_automation_postgres.py`
- `backend/tests/integration/test_m5_automation_migration_postgres.py`

并把二者加入 `.github/workflows/project-foundation-postgres-tests.yml`。CI缺 `POSTGRES_TEST_URL` 必须在 pytest
前硬失败；本地可明确 skip但不能作为 M5完成证据。

### 16.3 Frontend

- account/project query key；
- cancel-before-clear和迟到响应；
- route/menu/readiness/capability gate；
- Viewer read-only；
- create/edit/pause/resume/trigger/delete/history；
- manual idempotency header；
- scheduler-disabled、migration、409/429/503 state；
- direct URL 404/403；
- static demo无入口和请求；
- project chat automation link不回退 legacy route。

### 16.4 Full gate

- Backend full pytest、blocking-I/O、Ruff和format；
- M1–M5真实 PostgreSQL integration；
- Frontend check、unit和Playwright；
- `make doctor`、`make check-db`、migration dry-run；
- fresh install和legacy migration各一条smoke；
- 单次独立审查，Critical/Important finding全部关闭；
- `git diff --check`和文档一致性扫描。

## 17. 文档同步

实施 M5 时同步更新：

- `README.md`、`README_zh.md`：项目 Automation用户行为和scheduler config；
- root/backend/frontend `AGENTS.md`：M5 authority、scope、poll/lease、UI/cache和M6边界；
- 总体 SaaS设计：M5状态和5/8进度；
- M4专项规格：把通用 jobs/Worker lease旧归属修正为M6；
- Makefile/help：`migrate-automations`；
- 运维文档：automation migration/cutover runbook；
- release gate workflow：M1–M5 PostgreSQL文件。

## 18. 完成标准

满足以下全部条件后才把 M5 标记为已完成：

- scheduled task definition和occurrence全部具有非空 project/owner scope；
- 所有业务访问通过server-issued context和scoped repository；
- database composite constraint拒绝跨 scope parent/Thread/run关系；
- automatic/manual trigger在admission前持久化唯一 occurrence；
- Scheduler每次触发重新解析membership、capability、project state和Agent target；
- automatic run只通过M4 private run path执行，client不能伪造non-interactive；
- crash reconciliation不会自动重放已admit的Agent run；
- project API/UI、Viewer、readiness和account/project cache isolation完成；
- staged migration、owner/agent map、ledger、final schema和cutover marker完成；
- legacy automation authority在cutover后fail closed；
- M4 guard在新Alembic head下保持开放且fail closed语义不降低；
- M1–M5 PostgreSQL gate、Backend、Frontend和安全测试全绿；
- 文档、运维runbook和配置说明同步；
- 独立审查无未关闭Critical或Important finding。

完成后总体进度更新为 M1、M2、M3、M4、M5 已完成（5/8，62.5%），同时继续声明：通用
Worker/持久化 SSE、配额/审计/平台管理/通用备份恢复、legacy cleanup和完整发布验收仍未完成，系统不得作为
完整多用户 SaaS发布。
