# M3 Task 5 Report: Skill 完整目录快照、scan 与发布

## STATUS

PASS — 已实现 context-scoped `SkillRepository` 与 `SkillService`，覆盖完整目录快照、路径与
media type 安全、100 MiB 上限、per-file SHA-256、order-stable checksum、现有 parser /
validator / SkillScan 复用、secret requirement 脱敏、optimistic publish、archive/suspend、
published-only runtime load、system binding 与安全错误映射。

## RED

首次按 brief 运行 Task 5 unit/integration tests 时，19 个 unit tests 因目标 service 尚不存在而
失败；5 个 PostgreSQL tests 因当时未提供 URL 明确 skip，未被计为绿色证据：

```text
19 failed, 5 skipped
E   ModuleNotFoundError: No module named 'app.shared_assets.skill_service'
```

配置真实 PostgreSQL 后，首轮 5 个 integration tests 均在创建 version child rows 时触发
`published version child rows are immutable`。根因是同一次 flush 中 Task 1 trigger 看不到 parent
draft 状态；改为先 flush parent，再写完整 file snapshot 后恢复预期 draft 语义。

后续安全边界均先单独确认 RED：

- draft file 通过 `DELETE + INSERT` 替换成 `inode/symlink` 后 publish 未拒绝；新增从数据库当前
  rows 重建、规范化、hash/size 校验与重新 scan。
- 带 `value`、非法 env 名、重复名或非 boolean optional 的 `required-secrets` 会被现有 parser
  静默丢弃或强制转换；共享持久化边界改为 fail-closed，聚焦 RED 为 `3 failed, 1 passed`。
- 全局 `skill_scan.enabled=false` 可绕过 executable scan；聚焦测试报
  `Failed: DID NOT RAISE AssetValidationFailed`，M3 durable snapshot 现强制调用现有 SkillScan。
- runtime `load_version_files()` 可读取 project/system draft；真实 PostgreSQL 聚焦输出
  `2 failed in 1.73s`，repository load 查询现强制 `workflow_status='published'`。
- load 与并发 suspend 间没有行锁；真实 PostgreSQL RED 证明 raw suspend 可提交。load 现共享
  锁定 project/membership、Skill/version，system binding 路径额外锁 binding。

## GREEN

最终 Task 5 focused suite 加必要的 Task 1 schema regression，使用现有 PostgreSQL URL 并由
fixture 创建、清理随机 `deerflow_test_*` 数据库：

```text
$ cd backend && POSTGRES_TEST_URL='<redacted>' UV_CACHE_DIR=/tmp/deer-flow-uv-cache \
  uv run pytest tests/test_shared_asset_skill_service.py \
  tests/integration/test_m3_skill_assets_postgres.py \
  tests/test_m3_shared_assets_schema_postgres.py -q
....................................................                     [100%]
52 passed in 20.88s
```

现有 Skill parser/validation 与邻接 shared-asset context/Agent service regression：

```text
$ uv run pytest tests/test_skills_validation.py tests/test_skills_parser.py \
  tests/test_shared_asset_contexts.py tests/test_shared_asset_agent_service.py -q
............................................................             [100%]
60 passed in 0.38s
```

Changed-file Ruff、format check 与 diff check：

```text
All checks passed!
6 files already formatted
git diff --check  # no output
```

## Changed files

- `backend/app/shared_assets/skill_repository.py`
- `backend/app/shared_assets/skill_service.py`
- `backend/app/shared_assets/__init__.py`
- `backend/packages/harness/deerflow/skills/validation.py`
- `backend/tests/test_shared_asset_skill_service.py`
- `backend/tests/integration/test_m3_skill_assets_postgres.py`
- `backend/AGENTS.md`
- `.superpowers/sdd/task-5-report.md`

## Self-review

- Project repository public methods do not accept a bare project ID. Every project SQL path fixes trusted
  membership ID/project/user/version/status plus active project state; wrong scope, stale context,
  cross-project and absent rows all return `AssetNotFound`.
- Input collection is copied before the first database await. Paths normalize to sorted POSIX relative paths;
  absolute/drive/traversal/NUL/trailing separator, duplicate, file/ancestor collision, symlink and executable
  media types are rejected. Root `SKILL.md` and the inclusive 100 MiB boundary are tested.
- Every file row persists content, media type, size and SHA-256. Version checksum contains only sorted normalized
  path, file SHA and size, so file ordering cannot alter identity.
- Preview/create/publish perform file-system parser/validator/scan work through `asyncio.to_thread`. M3 shared
  assets force the existing SkillScan on even if the legacy global kill switch is disabled; CRITICAL or scanner
  exceptions fail closed. Persistence contains only allow/warn, rule IDs and severity counts, never evidence.
- `required-secrets` accepts only canonical string names or `{name, optional}`. The service compares raw
  declarations with parser output to reject invalid/dropped/duplicate entries, stores canonical metadata only,
  and creates no credential or grant.
- Publish locks the asset/version, checks optimistic asset version and draft workflow, reloads current child rows,
  verifies path/hash/size/media/total, reparses/rescans, then compares checksum and canonical metadata before the
  workflow transition and current pointer update. PostgreSQL triggers protect published parent/child immutability.
- Runtime load accepts only published versions. Archived assets retain pinned historical load; suspended assets
  fail immediately. Project system loads require an enabled binding pinned to that exact published system Skill
  version. Shared locks prevent concurrent suspend/binding mutation from invalidating an in-flight load.
- Only known Skill slug/version uniqueness constraints map to 409. Unknown Integrity/DBAPI failures map to a
  detail-free 503, and validation/auth failures retain only stable code semantics and request ID.
- Existing validator allowlist now includes `required-secrets` and `secrets-autonomous`, matching fields already
  supported by the existing parser; parser/validation regressions pass.

## Concerns

- Per task scope, the full backend suite was not run; evidence covers Task 5 unit/integration, Task 1 schema,
  Skill parser/validation, adjacent context/Agent service, changed-file Ruff and format.
- Task 5 does not add HTTP routers, execution resolver caching, MCP service or credential grant materialization;
  those remain later milestone work.
- SkillScan warning log behavior remains owned by the reused harness component. Durable shared-asset rows store
  only redacted decision/rule/severity metadata as required.
