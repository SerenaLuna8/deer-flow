# M4 Task 3 Report: Thread 与 checkpoint 双重作用域

## STATUS

PASS — 已实现 project/owner 双重作用域 Thread repository、完整 sync/async scoped
checkpointer、Thread authority service、显式 legacy trusted adapter 与 Gateway scoped saver
factory；未实现 Task 4 run/event/feedback snapshot，也未提前实现 Task 11 项目 routes。

## RED

三套目标测试在生产模块尚不存在时形成有效 RED：

```text
$ cd backend && POSTGRES_TEST_URL=postgresql://postgres@127.0.0.1:55439/postgres \
  UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run pytest \
  tests/test_private_thread_repository.py \
  tests/test_project_scoped_checkpointer.py \
  tests/test_private_thread_service.py -q
19 failed in 3.44s
```

失败均为预期缺失边界：`app.private_work.thread_repository`、
`app.private_work.checkpointer`、`app.private_work.thread_service` 和
`TrustedUnscopedThreadMetaStore` 尚不存在。首次尝试曾因测试 helper 使用错误的
`tests.support.*` import 在 collection 阶段产生 3 errors；修正为仓库既有的 `support.*`
约定后才记录上述有效 RED。

Task 1 finalize schema 的 checkpoint delete 状态先独立验证 RED：unit contract 与真实
PostgreSQL insert 均证明旧约束只接受 `failed`、拒绝用户确认的 `retry_required`。替换 ORM 与
0009 constraint 后两项各自 1 passed。

自审又发现 Viewer 删除权限缺口，新增 Viewer-owned Thread 测试得到有效 RED：

```text
E   Failed: DID NOT RAISE <class 'app.private_work.errors.PrivateWorkForbidden'>
1 failed in 0.56s
```

将 scoped delete 的能力要求从 `private_work.read_own` 提升为
`private_work.create` 后，该测试与 scoped saver suite 共 9 passed。

## IMPLEMENTATION

- `PrivateThreadRepository` 的 active create/get/search/check-access/patch/delete/compensate
  SQL 直接包含 `project_id + owner_user_id + deleted_at`，active 读取同时排除 frozen row；
  mutation 使用 `version` compare，global thread ID collision 折叠为固定 conflict。
- harness `ThreadMetaStore` 增加可选 `PrivateResourceScope` project path；SQL create 必须同时
  提供 final-schema Agent authority，scope path 的 get/search/update/delete 均在 SQL predicate
  中包含 project/owner/deleted。legacy 无 scope 路径只从
  `TrustedUnscopedThreadMetaStore` 暴露。
- `ProjectScopedCheckpointer` 覆盖 sync/async get/get_tuple/list/put/put_writes/delete，所有操作
  先 revalidate membership/capability 和 scoped Thread；put 覆盖 client marker 并回读验证，
  get/list 同时核验 server thread ID 与精确 project/owner marker。
- delete 在 raw saver 前先把 Thread 标记 deleted + pending；raw 删除失败记录
  `retry_required` 并保持 Thread 不可见，成功记录 `complete`。
- `PrivateThreadService` 实现 create/search/get/patch/delete/branch。create 顺序为 project lock
  → membership lock → create capability → project/system Agent executable → Thread row → root
  checkpoint；root 失败补偿物理删除 row。branch 只调用 PostgreSQL authority copy hook，不读
  host Thread 目录。
- Gateway production lifespan 只写 `app.state._raw_checkpointer`，项目侧只暴露
  `get_project_checkpointer(...).for_context(...)`。legacy getter 对旧 `state.checkpointer` 的 fallback
  仅用于隔离 TestClient/旧 external embedding；项目模块静态测试禁止调用 legacy getter。
- `checkpoint_delete_status` final constraint 以 `retry_required` 替换 `failed`。

## LEGACY REGRESSION ADJUSTMENT

Task 1 final schema 已要求 Thread 的 project、owner、Agent 全部非空，所以旧
`test_thread_meta_repo.py` 直接创建 owner/project/Agent 缺失 row 的 36 个测试在 baseline 会触发
`NotNullViolation`。没有放宽 schema 或在生产猜默认 authority；fixture 改为显式 seed users、
project、memberships 与 project Agent，并在构造 `TrustedUnscopedThreadMetaStore` 时明确注入
project/Agent/membership-version authority。原有 create/get/update/delete/owner/metadata/json-filter
覆盖全部保留。两条 final schema 已不可能成立的 owner-null 断言改为：trusted adapter 拒绝
ownerless create，strict access 对未创建 row 返回 false。

## GREEN

三套 Task 3 目标 suite：

```text
19 passed in 3.12s
```

brief 指定的 focused + legacy gate：

```text
128 passed, 1 warning in 11.60s
```

late authority changes 后重跑 Task 1 schema、Task 2 context/error/import firewall、Task 3、harness
project path 与 legacy regressions 的真实 PostgreSQL gate：

```text
184 passed, 1 warning in 15.70s
```

Gateway lifespan/recovery/multi-worker/config/stateless legacy 回归：

```text
46 passed, 1 warning in 1.14s
```

以上 PostgreSQL 测试使用 `/tmp` disposable PostgreSQL 14.19、随机
`deerflow_test_*` database，`POSTGRES_TEST_URL` 明确设置，0 skip。warning 仅为既有
Starlette `httpx` deprecation。

## QUALITY

```text
$ cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run ruff check <changed Python files>
All checks passed!
```

`ruff format` 已应用于 changed Python files；最终 `ruff format --check` 与 `git diff --check`
在提交前再次执行。

## DOCUMENTATION

- `backend/AGENTS.md` 已记录 Thread SQL scope、optimistic version、trusted legacy adapter、
  marker 验证、delete retry 状态、Gateway raw/scoped saver 边界、create 锁序与 branch hook。
- 本任务未新增用户可访问的项目 routes，因此没有更新 README；Task 11 暴露项目 API 时再同步
  用户文档。

## CONCERNS

- `TrustedUnscopedThreadMetaStore` 的 final-schema create 必须由 trusted caller 显式配置
  project/Agent authority；当前 production factory 不猜默认值。完整项目 create path 将由 Task 11
  接入 `PrivateThreadService`，legacy migration/cutover 仍由 Task 10 完成。
- branch authority copy 当前只有 Task 8 接入点；本任务没有复制 file rows，也没有触碰 host
  workspace。
- 已迁移且仍带旧 `failed` constraint 的个人开发数据库需要重新从此未发布 M4 migration baseline
  初始化；本任务处于冻结 M4 branch、尚未发布 revision，故直接修正 0009 contract，没有新建历史
  compatibility revision。
