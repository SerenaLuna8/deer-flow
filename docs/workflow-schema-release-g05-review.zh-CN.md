# Workflow Schema 与发布运维 G05 评审

> 状态：Phase 0 评审与 disposable PostgreSQL 原型。本文不是生产 migration，不能作为
> `make upgrade-db` 输入。正式原子变更由 G10 在当时真实 schema head 上生成。

## 1. 基线与结论

- 评审 checkout 的真实 schema head 为 `full_schema_v9`，增量链为 v5 → v9；若 G10 开始时
  head 未变化，下一 revision 才命名为 `full_schema_v10`，否则顺延，禁止复用示例版本号。
- 当前应用只接受唯一 head，旧进程不能声明兼容新 schema，因此采用协调维护窗口，不宣称
  零停机滚动升级。
- 首批 Workflow schema 必须是一次原子可审阅的发布单元，同时同步 ORM、`full_schema.sql`、
  Alembic、schema marker、catalog signature、Job 四方合同和真实 PostgreSQL parity。
- `workflow_runtime` 的 desired/effective policy 只存在于平台管理员“系统配置”的 PostgreSQL
  版本目录；`config.yaml`、环境变量、Compose 和 Helm 不得保存 Workflow 开关、限额或 profile
  选择。部署层只供应 Worker、镜像、RBAC、NetworkPolicy、egress proxy 和信任根能力。

G05 原型已在随机 disposable `deerflow_test_*` 数据库上从 v9 fresh schema 应用，且不更新
`alembic_version`。它证明高风险 FK/CHECK/partition/index 形状可由当前 PostgreSQL 15.5 表示；
它没有证明完整 G10 migration、生产数据量锁时长、备份恢复或目标 Kubernetes 环境。

## 2. G10 原子 schema 范围

### Definition 与发布

- `workflow_definitions`、`workflow_drafts`、`workflow_versions`；
- `workflow_version_model_refs`；
- `workflow_draft_credential_grant_intents`、`workflow_version_credential_slots`、
  `workflow_credential_grants`；
- Version payload immutable，Definition 的 current pointer 必须复合 FK 回自身 Version；Draft 使用
  revision CAS，Version 使用单调 version number 和 semantic checksum。

### 独立 WorkflowRun

- `workflow_runs`、`workflow_run_jobs`、`workflow_run_snapshots`；
- `workflow_run_runtime_policy_snapshots`、`workflow_run_model_snapshots`、
  `workflow_run_code_snapshots`、`workflow_run_http_snapshots`；
- `workflow_code_sandbox_leases`、`workflow_node_effects`；
- `workflow_run_event_invariants`、按 UTC 月 RANGE 分区的 `workflow_run_events`。

`workflow_runs` 不创建隐藏 Thread 或占位 Agent。`jobs` 新增独立
`workflow_run_id/workflow_epoch/required_worker_profile_digest`，首批只启用 `workflow_run`。
Agent `run_id` 与 Workflow `workflow_run_id` 必须互斥；`workflow_run_jobs` 保存全部 epoch 映射，
`workflow_runs.current_job_id` 只指向当前 epoch，两个方向均由 project/owner/run/epoch 复合 FK
约束。自动 Job attempt 不改变 epoch。

### 既有表的最小扩展

- `jobs`：三列、`workflow_run` type/authority、复合 FK/unique、精确 profile claim index；
- `worker_nodes`：已认证 runtime profile digest 集合，不存 provider locator 或 secret；
- `system_runtime_policies/system_runtime_policy_versions`：section CHECK 增加
  `workflow_runtime`；
- system policy bootstrap/管理员 API/Audit：增加 `new_workflow_runs` effect scope 和默认关闭的
  v1 policy。

首批不创建 waits、Agent/Skill/MCP 引用、Automation target、Workflow file/artifact 表，也不注册
`workflow_automation_run`。

## 3. 高风险约束原型结果

原型和测试位于 Backend 测试模块，仅供 G05：

- WorkflowRun → Job → epoch mapping → current Job 的循环引用可以通过 nullable current pointer、
  复合 unique/FK 和确定的插入顺序闭合；错误 trace、错误 epoch、同时携带 Agent `run_id` 均被
  PostgreSQL 拒绝。
- `workflow_node_effects` 与 G04 共用唯一 disposable DDL source；不存在第二张 HTTP effect 表。
  每次转移绑定 project/owner/trace/current Job/execution epoch/attempt/Worker/raw-lease hash，raw token
  只在 Worker 内存。`status='settled'` 必须同时持有 JSON object 形状的 bounded typed outcome 与
  digest；应用层再以 strict tagged-union adapter 校验。`(run,node,activation)` 单独唯一，同一
  activation 请求材料漂移必须 conflict，不能生成第二次 dispatch。
- Code lease 在 `(workflow_run_id,node_id,activation_id,attempt)` 上唯一。Provider acquire 前的
  `provisioning` 必须没有 cleanup locator；`running` 必须持有 owner-private ciphertext；
  `cleanup_pending` 若源自 running 必须保留 AEAD locator，若源自 acquire-before-locator 崩溃则
  locator 必须为 NULL、但持有 server-owned reconciliation hash 并按 exact label reconcile。
  两种 pending 都阻断新 attempt；`destroyed` 必须已有 `destroyed_at` 且 locator 已清除。
- 事件 invariant 行在插入时加锁，强制 contiguous seq 和唯一 terminal；正文表可独立按时间
  分区。
- `workflow_runtime` section 可与现有 append-only policy/current pointer 的 deferrable FK 共存。
- `ix_jobs_workflow_claim` 能服务 `workflow_run + queued + exact profile` claim 形状。

验证命令（从 `backend/`）：

```bash
uv run --env-file ../.env pytest -q tests/test_workflow_schema_g05_postgres.py
```

当前证据：`3 passed`，零 skip；每个用例创建并销毁随机 `deerflow_test_*` 数据库，开发库未执行
DDL。

## 4. Migration 顺序与锁评审

建议 G10 migration 在单个维护窗口按以下顺序执行：

1. 停止 Gateway、Worker、Scheduler，确认无新 Job/Run admission；
2. 建立备份和可验证恢复点；
3. 创建全新 Workflow tables、event invariant 与分区；
4. 为 `jobs`/`worker_nodes` 添加 nullable 或带常量默认的安全列；
5. 添加新 FK/CHECK/unique/index，并验证现有 Agent Job 数据；
6. 扩展 system-policy section，使用既有内置 bootstrap principal 原子插入默认关闭的
   `workflow_runtime` current/version；若旧 catalog 缺 section、pointer、principal 或 checksum
   异常，migration fail closed，禁止运行时修补；
7. 更新 schema marker，执行 fresh/upgrade catalog parity 与 `make check-db`；
8. 启动新进程，但 Workflow admission 仍关闭。

锁边界：新表和新分区不扫描旧业务表；`jobs` 的 CHECK/FK/index 需要维护窗口。G10 应优先采用
`ADD ... NOT VALID` → `VALIDATE CONSTRAINT` → 短事务切换旧约束的方式缩短强锁，但最终
catalog 必须与 fresh schema 完全一致。若索引仍在主 migration 事务内创建，则不能使用
`CONCURRENTLY`；由于旧进程已停止，可接受普通创建，但必须在目标数据量副本记录实际耗时。

## 5. 发布、恢复与 forward-fix 清单

发布：

1. 关闭新 admission，Worker drain；
2. 备份并做恢复点抽查；
3. 停止旧 Gateway/Worker/Scheduler；
4. `make upgrade-db`，然后 `make check-db`；
5. 启动新进程，确认 Gateway/Worker materialize 同一 `workflow_runtime` revision/checksum；
6. 确认 Worker attested profile digest 与管理员 desired profile 相交；
7. 分别通过真实 Code isolation 和 HTTP controlled-egress gate；
8. 管理员只在“系统配置 → Workflow 运行环境”中对 canary 项目开启 admission，再逐步放量。

恢复：

- migration 前失败：事务回滚，修复后重试；
- schema 已提交但新应用失败：Workflow 保持关闭，优先 forward-fix；旧应用不兼容新 head，禁止
  直接回切；
- 必须回退时：停止全部新进程，从已验证备份恢复整个数据库，再启动旧版本；不做 downgrade
  migration；
- 保留仍有非终态 Run 引用的旧 compiler contract；不删除 Version、Run、Event、effect 或
  cleanup-pending lease；
- 恢复后先验证 Agent Run/Job/Automation 回归，再重新执行 Workflow canary。

## 6. G10 必须补齐的证据

- fresh install 与 v9（或届时真实旧 head）upgrade 的 catalog signature 完全相等；
- migration 对已有 Agent/Automation/Memory Job 零语义退化；
- `workflow_runtime` fresh bootstrap、upgrade seed、partial/corrupt catalog 均 fail closed；
- 目标数据量副本上的约束验证、索引创建、分区创建和维护窗口耗时；
- backup/restore drill、Worker drain、旧 compiler retention 与 canary 记录；
- Docker Compose/Kubernetes/Helm 只证明能力供给，不成为第二份产品配置 authority。
