# Task 2 报告：成员 repository 与业务不变量

## 结果

Task 2 已实现成员列表、角色变更、移除和主动退出的 service/repository 边界。所有成员 mutation 都在 repository-owned transaction 中先锁 project、再锁目标 membership；最后一名 active Admin、membership version、跨项目资源隐藏和 membership 生命周期元数据均由该事务边界保护。

`MembershipView` 按协调结论精确包含：`membership_id`、`user_id`、`account_email`、`role`、`status`、`version`、`joined_at`。列表只允许 active 项目成员读取，并只返回 active membership。

## RED

先新增：

- `backend/tests/test_project_membership_service.py`
- `backend/tests/test_project_membership_repository_postgres.py`
- `backend/tests/test_project_context.py` 中 left/removed resolver 回归用例

运行：

```bash
cd backend
UV_CACHE_DIR=/tmp/uv-cache uv run pytest \
  tests/test_project_membership_service.py \
  tests/test_project_membership_repository_postgres.py -q
```

结果：预期失败，2 个测试文件均在收集阶段失败；`ProjectLastAdmin` 和成员模块尚不存在。没有先写生产实现。

## GREEN

本代理可安全执行的 focused 验证：

```bash
cd backend
UV_CACHE_DIR=/tmp/uv-cache uv run pytest \
  tests/test_project_membership_service.py \
  tests/test_project_membership_repository_postgres.py \
  tests/test_project_context.py -q
```

结果：`15 passed, 8 skipped`。8 个 skip 都是因为当前非授权测试进程没有 `POSTGRES_TEST_URL`。

相关项目回归：

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest \
  tests/test_project_capabilities.py \
  tests/test_project_service.py \
  tests/test_projects_router.py \
  tests/test_project_membership_service.py \
  tests/test_project_context.py -q
```

结果：`37 passed, 5 skipped, 1 warning`；warning 是既有 Starlette/httpx deprecation。

格式与静态检查：

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run ruff check \
  app/projects/membership_models.py \
  app/projects/membership_repository.py \
  app/projects/membership_service.py \
  app/projects/errors.py \
  tests/test_project_membership_service.py \
  tests/test_project_membership_repository_postgres.py \
  tests/test_project_context.py
```

结果：`All checks passed!`；同一文件集合 `ruff format --check` 通过。

## 真实 PostgreSQL 覆盖

`backend/tests/test_project_membership_repository_postgres.py` 使用既有 `migrated_postgres_database_url`，由 fixture 创建随机 `deerflow_test_*` 数据库，覆盖：

- role/remove/leave 同时递增目标 membership `version` 和 project `membership_version`；
- project `FOR UPDATE` 发生在 membership `FOR UPDATE` 之前；
- 两名 Admin 并发自降级只有一个成功，数据库仍有一名 active Admin；
- stale expected version 无写入；
- change/remove 使用跨项目 membership ID 均为 `ProjectNotFound`；
- removed membership 不再出现在成员列表；
- left/removed 立即不能解析 `ProjectContext`；
- `ended_at`、30 天 `retention_until`、`ended_by_user_id`、`end_reason` 正确持久化。

本代理尝试按协调路径从 `.env` 给 `POSTGRES_TEST_URL` 注入本机测试数据库，但 unsandboxed 审批拒绝了“从 `.env` 读取 DATABASE_URL 并执行数据库 mutation”；按安全策略没有绕过。主代理需要在显式授权环境运行：

```bash
cd backend
source ../.env
POSTGRES_TEST_URL="$DATABASE_URL" uv run pytest \
  tests/test_project_membership_service.py \
  tests/test_project_membership_repository_postgres.py \
  tests/test_project_context.py -q
```

## 文件

新增：

- `backend/app/projects/membership_models.py`
- `backend/app/projects/membership_repository.py`
- `backend/app/projects/membership_service.py`
- `backend/tests/test_project_membership_service.py`
- `backend/tests/test_project_membership_repository_postgres.py`

修改：

- `backend/app/projects/errors.py`
- `backend/tests/test_project_context.py`

`backend/app/projects/context.py` 已经只解析 active project + active membership，无需修改生产代码；本任务增加真实 PostgreSQL 回归测试锁定 left/removed 的 404 行为。

## 自审

- mutation 的调用顺序固定为 capability 检查 → transaction → project lock → actor context active/version 重新读取 → target membership lock（含 project scope）→ expected version → last Admin 检查 → mutation/read-back → commit。actor 检查刻意放在取得 project 锁之后，避免锁等待期间沿用旧 statement snapshot 的授权预筛选结果。
- project 行锁串行化同一项目的所有成员 mutation，因此 Admin 计数不会在两个治理事务之间形成并发丢失更新；测试以双 session `asyncio.gather` 固定该竞态。
- target membership 查询始终包含 `project_id`，跨项目 ID 不先做 unscoped lookup，不泄露资源存在性。
- actor scope 同时绑定 context 的 user/project/membership/version 和 active 状态；自降级、自退出或自移除后旧 context 因 membership version/status 变化立即失效。
- 降级、移除、退出均覆盖最后 Admin；角色保持为同一值不是状态变更，因此只 read-back，不递增 version。
- mutation read-back 位于同一 transaction；任何 DBAPI failure 由 transaction rollback 后映射为无敏感信息的 `ProjectDatabaseUnavailable`。
- 未实现 membership 重新激活或重新加入；该能力保留给 Task 3。
- 没有修改 Task 2 列表外的架构/路由/前端文件；Task 9 负责最终 README/AGENTS 和里程碑文档同步。

## 顾虑

- 唯一未在本代理环境完成的门禁是真实 PostgreSQL GREEN；测试已写全但必须由主代理显式授权执行后才能宣称 Task 2 完整通过。
- `account_email` 是当前 users schema 唯一稳定账户标识，按协调结论供 active 项目成员列表使用；未来若增加 display name，应由后续 schema/API 变更替换显示层，而不是在本任务猜测派生值。
