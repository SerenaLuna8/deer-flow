# M4 Task 3 Review Fix Wave

## STATUS

PASS — 修复 `task-3-review.md` 的 2 Critical、5 Important、1 Minor；未实现
Task 4 run/event/feedback scope、Task 11 project router 或其他后续任务。

## RED

首组 7 项按 capability/frozen/compensation/sync-coroutine 分类先形成有效 RED：

```text
7 failed in 1.59s
```

失败分别证明 Viewer-owned delete 被拒、ambiguous root checkpoint 未清理、cleanup
失败时 authority row 被物理删除、branch authority hook 无 rollback、SQL/Memory harness
可命中 frozen/deleted row，以及 owner loop 的 sync misuse 遗留未 await coroutine。

跨 worker serialization 使用 pauseable saver 与真实 PostgreSQL transaction 形成第二组 RED：

```text
4 failed in 1.32s
```

`put/delete`、`put_writes/delete`、`get/membership revoke`、`list/membership revoke`
都复现 authorize transaction 已结束、raw IO 可与 delete/revoke 交错。

production composition 使用真实 `langgraph_runtime`、production
`make_thread_store` 和 `AsyncPostgresSaver` 形成 Critical RED：

```text
2 failed in 1.20s
```

- `POST /api/threads` 在无 trusted create authority 时返回旧 500，而不是稳定
  `409 PRIVATE_WORK_CUTOVER`；
- legacy delete 后真实 `_raw_checkpointer` 中 checkpoint 仍存在。

## IMPLEMENTATION

### Critical production legacy boundary

- harness 新增不依赖 `app.*` 的
  `LegacyThreadCreateAuthorityUnavailable`；Gateway 将其映射为固定
  `409 PRIVATE_WORK_CUTOVER`，不回显内部 authority/config 信息。
- `start_run()` 在 checkpoint lookup、run admission 和 graph launch 前要求 durable
  Thread authority。production factory 不猜 default project/Agent；缺 authority 时立即
  cutover conflict。checkpoint preflight 失败会补偿本次刚创建的 test-double authority row。
- legacy delete 经 `get_checkpointer(request)` 获取 production `_raw_checkpointer`，先写
  invisible tombstone/pending，再删除 raw checkpoint；失败保留 `retry_required`，成功保留
  `complete`，不再物理删除唯一 authority row。
- unscoped `check_access()` 先按 Thread identity 检查 tombstone/frozen state，再决定 missing-row
  permissive compatibility；已存在的 inactive row 永远不能退化成“未跟踪 legacy row”。

### Capability and concurrency

- whole-Thread delete 要求 `PRIVATE_WORK_READ_OWN`，所以 Viewer 可删除自己的既有数据；
  create/branch/put/put-writes 仍要求 `PRIVATE_WORK_CREATE`，跨 owner 仍统一 404。
- scoped get/list/put/put-writes 在一个 transaction 内按
  project → membership → Thread `FOR UPDATE` 锁序 revalidate，并把锁保持到 raw saver IO
  与 marker validation 完成；多 worker delete/revoke 无法跨越已通过的 authorization window。
- delete 使用同一锁序标记 tombstone；writer 先行时 delete 等 writer，delete 先行时新 writer
  看不到 active Thread。raw saver 使用独立 pool/table，测试中未出现连接或锁死锁。

### Frozen harness and compensation

- SQL harness 的 scoped get/search/check/update/delete 全部排除 `frozen_at`；Memory double 同步
  active deleted/frozen/version/checkpoint-delete-status 语义。
- create/branch raw write 失败或结果不确定时，先通过 scoped delete 清 target raw checkpoint；
  branch 同时调用 authority rollback hook。两项都成功才 purge compensated tombstone；任一失败
  保留 invisible `retry_required` row，阻止 UUID 被其他 scope 重用。
- 删除旧的 row-only `compensate_create()` 入口，避免未来绕过 raw/authority cleanup state machine。

### Production saver and static boundary

- 随机 PostgreSQL test DB 上使用真实 `make_checkpointer()` / `AsyncPostgresSaver` 覆盖 async 与
  sync get/get_tuple/list/put/put_writes/delete、普通异线程、异线程内已有 event loop、marker
  mismatch 和 owning-loop rejection。
- static gate 改为 AST 扫描完整 `app/private_work` 与 `app/projects` package，拒绝 direct/alias
  import、attribute/dynamic calls to `get_checkpointer` / `get_run_context`；另扫描完整 `app/**`
  direct raw app-state access，只有 `app/gateway/deps.py` 在 exact allowlist。
- sync bridge 接收 coroutine factory，完成 owner-loop checks 后才创建 coroutine，消除
  `RuntimeWarning: coroutine was never awaited`。

## GREEN

逐类 focused GREEN：

```text
7 passed in 1.53s
4 passed in 1.31s
2 passed in 1.09s   # production lifespan/factory create-run-delete composition
2 passed in 0.82s   # real AsyncPostgresSaver + AST boundary
```

Task 3 + legacy expanded focused gate（随机 `deerflow_test_*`，0 skip）：

```text
246 passed, 1 warning in 15.87s
```

Task 1/2 schema/context/import firewall + Gateway lifespan/recovery/checkpointer/auth gate
（随机 `deerflow_test_*`，0 skip）：

```text
171 passed, 2 warnings in 10.37s
```

既有 Memory ThreadMeta isolation regression 另跑 `8 passed in 0.18s`。

warning 仅为既有 Starlette/httpx deprecation。

## QUALITY

- PostgreSQL-only；没有 SQLite、RLS 或上游 checkpoint schema 修改。
- PostgreSQL tests 只连接 `/tmp` disposable server，并由 fixture 创建/清理随机
  `deerflow_test_*` database。
- changed Python files 最终 `ruff format --check` 为 15 files already formatted，
  `ruff check` 为 All checks passed；`git diff --check` 通过。
- `backend/AGENTS.md` 已同步 cross-worker lock、Viewer delete、legacy tombstone/cutover 和
  compensation state machine。

## CONCERNS

- `test_runtime_lifecycle_e2e.py` 的六条 legacy run E2E 属于 M4 中间态已知断点：Task 1 final
  schema 已要求 `runs.project_id NOT NULL`，而 Task 4 scoped run admission 与 Task 11 project
  router 尚未实现。Task 3 production factory 不允许猜 project/Agent，因此这些旧 E2E 现在在
  Thread create 处稳定返回审查要求的 `409 PRIVATE_WORK_CUTOVER`，没有用 test fixture 绕过或
  修改 production path；Task 4/11 应以 project-scoped E2E 替代它们。
- scoped saver 为 correctness 把数据库 row locks 保持到 raw saver IO 完成；raw saver 使用独立
  pool/table且当前 concurrency/production saver gates 无死锁，但后续容量测试应观察慢 saver 对
  persistence pool 占用时间的影响。
