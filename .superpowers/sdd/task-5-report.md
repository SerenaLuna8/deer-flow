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
- `backend/packages/harness/deerflow/skills/skillscan/__init__.py`
- `backend/packages/harness/deerflow/skills/skillscan/orchestrator.py`
- `backend/tests/test_shared_asset_skill_service.py`
- `backend/tests/integration/test_m3_skill_assets_postgres.py`
- `backend/tests/test_skillscan_native.py`
- `backend/AGENTS.md`
- `.superpowers/sdd/task-5-report.md`

## Self-review

- Project repository public methods do not accept a bare project ID. Every project SQL path fixes trusted
  membership ID/project/user/version/status plus active project state; wrong scope, stale context,
  cross-project and absent rows all return `AssetNotFound`.
- Input collection is copied before the first database await. Paths normalize to sorted NFC POSIX relative paths;
  an NFC + casefold identity rejects host-filesystem aliases and ancestor collisions before materialization.
  Absolute/drive/traversal/NUL/trailing separator, duplicate, symlink and executable media types are rejected.
  Every segment also rejects Win32 trailing dot/space, ADS/illegal/control characters and reserved device names,
  independent of the current host filesystem. Root `SKILL.md` and the inclusive 100 MiB boundary are tested.
- Every file row persists content, media type, size and SHA-256. Version checksum contains only sorted normalized
  path, file SHA and size, so file ordering cannot alter identity.
- Preview/create/publish perform file-system parser/validator/scan work through `asyncio.to_thread`. M3 shared
  assets force the existing SkillScan on even if the legacy global kill switch is disabled; CRITICAL findings,
  scanner exceptions and non-empty analyzer/read errors fail closed. Persistence contains only allow/warn, rule
  IDs and severity counts, never evidence.
- Before invoking the existing parser or validator, a no-log raw frontmatter preflight rejects non-string keys
  and malformed `required-secrets` / `secrets-autonomous`. Its SafeLoader rejects duplicate keys recursively at
  every mapping level before any shadowed value can reach parser logs or persistence. Only canonical
  name/optional metadata persists; no secret value, credential or grant is created.
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

## Formal Task 5 review follow-up

正式 reviewer 返回的 6 个 finding 均逐项验证为当前代码的真实问题，并分别完成 RED→GREEN：

1. **Host temporary-filesystem aliases**
   - RED：大小写 alias、NFC/NFD alias 与大小写 ancestor collision 共 `3 failed`；Linux 上字面路径
     不冲突，但 macOS case-insensitive / normalization filesystem 可在 scan 前覆盖文件。
   - GREEN：存储路径先转 NFC，并以 NFC + casefold POSIX identity 检测重复与 ancestor；聚焦
     `4 passed`，包含原 order-stable checksum regression。

2. **SkillScan analyzer/read errors**
   - RED：monkeypatch `_scan_text_file` 抛错时 preview 仍 allow；新增完整结果 API 的 harness test
     首先因 import 不存在而 RED。
   - GREEN：新增 `enforce_static_scan_result()` 返回完整 `ScanResult`；旧
     `enforce_static_scan()` 继续返回 findings list。M3 对任意 `scanner_errors` 返回稳定 422；新旧
     API 聚焦 `4 passed`。

3. **Executable Mach-O magic coverage**
   - RED：application/octet-stream 的 32-bit little-endian `CE FA ED FE` 与 little-endian fat
     `BE BA FE CA` 两项未拦截，输出 `2 failed, 6 passed`。
   - GREEN：补齐完整 4-byte Mach-O 判定表并保留 ELF/PE 检测，覆盖两种 endian 的 32/64-bit
     与 fat magic；连同 archive/nested archive regression 为 `10 passed`。

4. **Raw secret parser-log leak**
   - RED：非法 required-secret name 出现在 parser warning；malformed `secrets-autonomous` 被 warning
     后静默转换且未返回 422。
   - GREEN：parser/validator 前先无日志检查 raw YAML key 与 secret-control shape/name/optional；
     caplog、exception 均不含 raw value。显式 `null` control 也经额外 RED→GREEN 固定拒绝。

5. **Event-loop blocking after DB load**
   - RED：真实 PostgreSQL publish/load 参数化 heartbeat 均在主线程 SHA gate 超时，`2 failed`。
   - GREEN：publish reconstruction 与 load reconstruction + 两次 checksum hash 全部包装在
     `asyncio.to_thread`。测试用 `threading.Event` 确定性 gate：只有 event-loop heartbeat 能释放
     hash，且断言每次 SHA thread ID 都不等于主线程；输出 `2 passed`。静态 blocking-I/O detector
     对 `skill_service.py` 输出 `No static blocking IO event-loop risk findings`。

6. **Non-string YAML key**
   - RED：`true: x` 进入现有 validator 后在 join/sort 抛裸 `TypeError`。
   - GREEN：同一 raw preflight 在 parser/validator 前拒绝非字符串 top-level key，稳定映射为
     request-scoped `AssetValidationFailed`。

复审修复后的最终 PostgreSQL gate：

```text
.......................................................................  [100%]
71 passed in 22.24s
```

受影响 harness parser/validation/SkillScan、installer、manage tool、custom router、blocking-I/O 与
Task 5 unit focused suite：

```text
........................................................................ [ 34%]
........................................................................ [ 68%]
...................................................................      [100%]
211 passed, 1 warning in 4.31s
```

## Second formal Task 5 review follow-up

第二轮正式 reviewer 的 3 个 P1 继续逐项完成 RED→GREEN：

1. **Official 64-bit universal Mach-O magic**
   - RED：direct application/octet-stream preview 与 nested archive 各漏掉 FAT_MAGIC_64
     `CA FE BA BF`、FAT_CIGAM_64 `BF BA FE CA`，合计 `4 failed, 16 passed`。
   - GREEN：两个 magic 进入统一 executable 判定；direct 与 nested tests 同时覆盖 ELF、PE、
     32/64-bit Mach-O、fat32、fat64 全集，输出 `20 passed`。

2. **Recursive duplicate YAML keys and persistence**
   - RED：nested metadata mapping 和 required-secret list-item mapping 的 duplicate 均 last-key-wins，
     unit 输出 `2 failed`。真实 PostgreSQL case 用前一个含 forbidden value 的 `required-secrets`
     再 shadow 为 canonical key，create-version 未抛 422 并会落库。
   - GREEN：专用 SafeLoader 在每个 mapping constructor 中先 flatten merge，再检查所有 key；任何
     duplicate/unhashable key 在 parser/validator 前映射为无 input detail 的 422。unit safety set
     `5 passed`；真实 PostgreSQL `1 passed`，并断言该 Skill 的 version/file row 都为 0，caplog 与
     exception 不含 raw value。

3. **Win32-safe path segments**
   - RED：`run.py`/`run.py.` alias、trailing space、segment trailing dot/space、NTFS ADS、Win32
     illegal/control chars，以及带 extension/大小写变体的 CON/PRN/AUX/NUL/COM1-9/LPT1-9 共
     `23 failed, 1 passed`；仅非 reserved COM10/LPT0/CONSOLE compatibility case 通过。
   - GREEN：每个 normalized segment 独立校验上述规则，不依赖 host tempfile alias；连同既有
     POSIX unsafe path、NFC/casefold alias 和 order-stable checksum regression 为 `34 passed`。

第二轮修复后的最终 PostgreSQL gate：

```text
........................................................................ [ 72%]
............................                                             [100%]
100 passed in 22.90s
```

Task 5 unit 与受影响 parser/validation/SkillScan regression：

```text
........................................................................ [ 50%]
......................................................................   [100%]
142 passed in 0.53s
```

## Concerns

- Per task scope, the full backend suite was not run; evidence covers Task 5 unit/integration, Task 1 schema,
  Skill parser/validation, adjacent context/Agent service, changed-file Ruff and format.
- Task 5 does not add HTTP routers, execution resolver caching, MCP service or credential grant materialization;
  those remain later milestone work.
- SkillScan warning log behavior remains owned by the reused harness component. Durable shared-asset rows store
  only redacted decision/rule/severity metadata as required.
- 211-test harness focused suite 有 1 条既有 Starlette/httpx deprecation warning；无 test failure，且与
  本次 SkillScan API 和 Task 5 变更无关。
