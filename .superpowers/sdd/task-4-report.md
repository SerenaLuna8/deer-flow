# M3 Task 4 Report: Agent typed repository 与 version lifecycle service

## STATUS

PASS — 已实现 context-scoped `AgentRepository` 与事务化 `AgentService`，覆盖 project/system
scope、typed version payload、dependency closure、optimistic publish、archive/suspend、visible
listing、version history 和稳定错误映射；未实现 Skill/MCP service、router 或运行时 resolver。

## RED

首次按 brief 运行两份 Task 4 测试时，5 个测试都因目标模块尚不存在而按预期失败：

```text
$ cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache \
  POSTGRES_TEST_URL='<redacted>' uv run pytest \
  tests/test_shared_asset_agent_service.py \
  tests/integration/test_m3_agent_assets_postgres.py -q
FFFFF
E   ModuleNotFoundError: No module named 'app.shared_assets.agent_service'
5 failed in 2.14s
```

后续每个新增边界继续单独确认 RED：

- package typed exports：`AttributeError: module 'app.shared_assets' has no attribute 'AgentService'`。
- 已存在但尚无 version 的 Agent history：错误返回 `AssetNotFound`，预期空 tuple。
- project mutation context TOCTOU：并发 membership version 更新未被阻塞，测试报
  `Failed: DID NOT RAISE DBAPIError`。
- 独立 review 发现的 repository cross-project write bypass：caller-supplied fake `AgentRow`
  可以把 version 写到另一项目 Agent，测试报 `Failed: DID NOT RAISE AssetNotFound`。
- `AgentPayload` collection alias：dependency closure 通过后在下一次 await 中修改原 list，draft
  固定到了错误的跨项目 version，断言显示 dependency UUID 不一致。

以上失败均来自目标行为尚未实现，不是 fixture、测试拼写或 Task 1 schema 绕过。

## GREEN

最终 focused suite 与必要的 Task 1 schema regression：

```text
$ cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache \
  POSTGRES_TEST_URL='<redacted>' uv run pytest \
  tests/test_shared_asset_agent_service.py \
  tests/integration/test_m3_agent_assets_postgres.py \
  tests/test_m3_shared_assets_schema_postgres.py -q
.............................                                            [100%]
29 passed in 19.15s
```

Changed-file Ruff 与格式：

```text
$ cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run ruff check \
  app/shared_assets/agent_repository.py app/shared_assets/agent_service.py \
  app/shared_assets/__init__.py tests/test_shared_asset_agent_service.py \
  tests/integration/test_m3_agent_assets_postgres.py
All checks passed!

$ cd backend && UV_CACHE_DIR=/tmp/deer-flow-uv-cache uv run ruff format --check \
  app/shared_assets/agent_repository.py app/shared_assets/agent_service.py \
  app/shared_assets/__init__.py tests/test_shared_asset_agent_service.py \
  tests/integration/test_m3_agent_assets_postgres.py
5 files already formatted
```

## Changed files

- `backend/app/shared_assets/agent_repository.py`
- `backend/app/shared_assets/agent_service.py`
- `backend/app/shared_assets/__init__.py`
- `backend/tests/test_shared_asset_agent_service.py`
- `backend/tests/integration/test_m3_agent_assets_postgres.py`
- `backend/AGENTS.md`
- `.superpowers/sdd/task-4-report.md`

## Self-review

- Project repository 的 public project 方法不接收裸 `project_id`；project lookup 在 SQL 中固定
  `scope='project'`、`project_id=context.project_id`、membership ID/user/version/status 与 active
  project。错误 scope、跨项目、陈旧 context 和不存在统一为 `AssetNotFound`。
- Project mutation 先按固定顺序对 project/membership 取 share lock，再对 Agent 取 update
  lock，避免多语句 publish/create-version 中途失去 trusted context。真实 PostgreSQL 回归确认
  membership invalidation 在事务结束前超时，事务结束后完整 mutation 可继续。
- `create_project_version()` 不再信任 caller-supplied ORM asset；它使用 context-scoped
  `asset_id` 重新查询并锁定 Agent，同时验证 `version.agent_id`，跨项目绕过零写入。
- System Agent 写路径只接受 `SystemAssetGovernanceContext`，并只允许 active system published
  Skill/MCP version；Project Agent 只允许同项目 active published dependency，或本项目 enabled
  binding 精确固定的 system published version。
- Dependency closure 在 draft 创建和 publish 时都会重验并锁 dependency/binding。测试覆盖
  project Skill/MCP、system Skill/MCP、未绑定、绑定禁用、archived、suspended 与跨项目情况。
- Publish 在一个事务中完成 Agent lock、expected version、draft workflow、dependency closure、
  workflow transition、current pointer 移动和 optimistic version 增量；真实并发测试只有一个
  publish 成功，另一个稳定 `AssetConflict`。
- `AgentPayload` 的 collection 在第一次数据库 await 前复制为 tuple snapshot；checksum、refs 与
  typed view 复用同一份已校验内容。published row 与 dependency refs 继续由 Task 1 PostgreSQL
  trigger 保证不可变，raw payload UPDATE 按预期失败。
- 空 history 对已存在 Agent 返回空 tuple；错误 scope/cross-project history 仍先 scoped lookup 后
  返回 404。Project `list_visible()` 返回本项目 Agent 加 enabled system Agent binding。
- Archive 和 suspend 使用 optimistic asset version；project suspend 只允许 Admin capability，
  system lifecycle 只允许 governance context。
- 独立 reviewer 首轮给出 1 Critical、2 Important；cross-project write 与 payload alias 已通过
  RED→GREEN 修复，MCP binding publish-time revalidation 已补测试。复审结论为 PASS，无剩余
  Critical 或 Important finding。

## Concerns

- 按任务约束未运行全量 backend suite；只运行 Task 4 focused tests、必要 Task 1 schema
  regression 和 changed-file Ruff。
- Task 5/6 尚未提供 Skill/MCP service，因此 integration tests 通过原始 SQL 只在随机
  `deerflow_test_*` 数据库中准备 published dependency 与 binding fixture；没有触碰业务库。
- Task 4 不实现执行时 resolver。suspended dependency 在本任务的 create/publish closure 中立即
  不可用；已有 published Agent 的运行时 suspended fail-closed 由后续 M4 resolver 继续落实。
