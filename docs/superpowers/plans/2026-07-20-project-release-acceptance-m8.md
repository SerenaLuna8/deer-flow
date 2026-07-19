# M8 Host Release Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不增加业务能力或第二套运行路径的前提下，为 DeerFlow 项目优先、多用户 SaaS 建立可重复、fail-closed、证据绑定的宿主机发布验收，并以每个精确认证提交的一对 fresh 完整运行和独立 0/0/0 审查关闭 M8。

**Architecture:** 在 M7 最终 PostgreSQL runtime 外增加 repo-owned acceptance package：closed Pydantic contracts 固定 stage manifest、isolation matrix、evidence 和 review report；确定性门禁先证明隔离、容量、故障与安全，再由 invocation-owned orchestrator 对随机数据库执行真实 `make setup-db` / `make start`、Chromium、DeepSeek `deepseek-v4-pro` 和完整 restore traffic switch。每个认证提交第一次完整运行只生成 `candidate_ready`；独立审查报告绑定 exact commit、manifest 和 candidate evidence 后，第二次从 fresh state 重跑全部 stage 才能生成 `final_pass`。关闭文档产生新提交后重复同一认证对。

**Tech Stack:** Python 3.12、Pydantic v2、asyncio、SQLAlchemy async、asyncpg、PostgreSQL 17、pytest/pytest-asyncio、pip-audit、detect-secrets、FastAPI、Next.js 16、React 19、TypeScript 5.8、pnpm 10、Playwright Chromium、Nginx、DeepSeek `deepseek-v4-pro`、现有 version-7 backup/restore/journal authority。

## Global Constraints

- M8 是验收里程碑；只有 RED gate 证明 M1–M7 final behavior 不满足冻结契约时才修改 product runtime，且修复必须最小化。
- 唯一认证部署路径是新的 PostgreSQL 数据库 → `make setup-db` → `make start` → 桌面 Chromium。Docker Compose、Kubernetes、Helm、Firefox、Safari/WebKit 和其他模型供应商不进入 M8 阻断门禁。
- PostgreSQL 仍是全部在线 authority；不得引入 SQLite、filesystem authority、Redis、Kafka、对象存储或 testing fallback 到 production runtime。
- 所有私有读写继续绑定 `account + project_id + owner_user_id`；Gateway 不执行 Agent graph，Worker 是唯一 graph executor，Scheduler 只 admit due Automation。
- 完整入口只在显式 `M8_LIVE_ACCEPTANCE=1` 时执行付费模型和灾备切换；普通 CI 永不读取 `DEEPSEEK_API_KEY`。
- `DEEPSEEK_API_KEY` 只能由当前进程环境或既有 gitignored secret source 解析；任何代码、YAML、测试参数、命令文本、stdout/stderr、证据、文档或 Git 都不得包含其值。
- live preflight 必须唯一解析 `ModelConfig.model == "deepseek-v4-pro"`；evidence 记录其逻辑 `ModelConfig.name` 和 provider model ID，不要求二者同名。
- 所有完整验收数据库必须由当前 invocation 创建并命名为随机 `deerflow_test_*` / `deerflow_restore_*`；不得连接、修改或删除既有业务数据库。
- 所有进程、PID/PGID、port、database、directory、file 和 inode 都写入 invocation ownership ledger；cleanup 前重新验证 identity，不使用 `make stop`、进程名 broad match、glob 或 repo-root recursive delete。
- 完整验收只接受 clean、non-detached、运行中不变化的 exact Git commit；dirty tree、commit/manifest 变化和任何 stage skip 都失败。
- `.release-evidence/<acceptance_run_id>/` 是 gitignored 本地证据根；writer 使用 closed schema 和 atomic replace，永不复制原始 stdout/stderr、浏览器 trace、模型正文或 private content。
- 固定 M1–M8 PostgreSQL gate 必须使用真实 PostgreSQL、0 skip、无新增 xfail；M1–M7 原有 22 文件保持有序前缀，新增 M8 文件只追加。
- dependency advisory、secret finding、安全 finding、matrix uncovered case、cleanup residual 或 review finding 都阻断 final pass；不允许 wildcard exclusion、severity downgrade 或 accepted-risk 关闭 M8。
- M8 不定义 latency/throughput SLO；duration、RTO、RPO 只记录事实，不设通过阈值。
- 每个 Task 1–9 先写 RED tests、最小实现、affected/full gate、独立 commit，再用 `superpowers:requesting-code-review` 审查 `HEAD^..HEAD`；Critical/Important/Minor 都必须在下一任务前归零。
- Task 10 对完整 M8 分支使用 `superpowers:requesting-code-review`；有效 finding 通过 `superpowers:receiving-code-review` 复现、修复、重跑，然后重新生成 candidate 并重新审查。
- 开始执行前使用 `superpowers:using-git-worktrees` 建立 `codex/m8-release-acceptance` 隔离工作区，基线为包含本计划提交的 `dev` HEAD。
- 每次声称通过或完成前使用 `superpowers:verification-before-completion`；M8 关闭后使用 `superpowers:finishing-a-development-branch` 选择集成方式。

## File Structure

### Acceptance contracts and evidence

- `contracts/m8_isolation_matrix.json` — actor/resource/operation/expected outcome/evidence selector 的唯一机器清单。
- `contracts/m8_secret_allowlist.json` — 仅 fixed test fake / documentation placeholder 的 exact path + rule + digest allowlist。
- `contracts/m8_threat_controls.json` — final M7 threat、preventive/detective control 和 executable evidence mapping。
- `contracts/m8_release_evidence.schema.json` — generated closed JSON schema；禁止 private/raw fields。
- `contracts/m8_review_report.schema.json` — generated independent-review closed schema。
- `backend/scripts/release_acceptance/models.py` — frozen stage、evidence、review、ownership Pydantic models。
- `backend/scripts/release_acceptance/contracts.py` — contract loader、schema generator、stable digest、matrix completeness/drift validation。
- `backend/scripts/release_acceptance/evidence.py` — atomic redacted JSON writer 和 final manifest hash sealing。
- `backend/scripts/release_acceptance/security.py` — locked dependency graph audit and value-redacted tree/diff/history/evidence scanner。

### Deterministic release gates

- `backend/tests/test_m8_acceptance_contract.py` — schema、stage order、review binding、no-extra-fields contracts。
- `backend/tests/test_m8_evidence.py` — redaction、atomic failure evidence、manifest digest、forbidden-field tests。
- `backend/tests/test_m8_isolation_matrix_postgres.py` — matrix selectors、cross-scope predicates、route/repository drift。
- `backend/tests/test_m8_capacity_postgres.py` — default quota boundaries、race、streaming upload、MCP settlement。
- `backend/tests/test_m8_security_gates.py` — threat mapping、dependency findings、Git/evidence secret scanning。
- `backend/tests/test_m8_ownership.py` — PID/database/path identity and fail-closed cleanup tests。
- `backend/tests/blocking_io/test_m8_release_acceptance.py` — command/process/file/security orchestration does not block the backend event loop。
- `backend/tests/test_m8_release_gate_postgres.py` — fixed M1–M8 Makefile/CI order and zero-skip gate contract。
- `frontend/tests/e2e/m8-isolation-matrix.spec.ts` — deterministic account/project/cache/role browser selectors。

### Host/live/recovery acceptance

- `backend/scripts/release_acceptance/preflight.py` — live switch、clean commit、config/model、toolchain、ports、DB admin authority validation。
- `backend/scripts/release_acceptance/ownership.py` — invocation ledger and exact cleanup verifier。
- `backend/scripts/release_acceptance/commands.py` — fixed command IDs、bounded summaries and immutable stage manifest。
- `backend/scripts/release_acceptance/host_stack.py` — exact `make setup-db` / `make start` process-session owner and readiness/stop API。
- `backend/scripts/release_acceptance/live_probe.py` — bounded API/database live assertions; never returns content bodies。
- `backend/scripts/release_acceptance/recovery_drill.py` — archive、post-backup tombstone、restore、switch/back-switch and RTO/RPO summary。
- `backend/scripts/release_acceptance/runner.py` — fail-fast orchestration、always-cleanup、candidate/final sealing。
- `backend/scripts/run_release_acceptance.py` — thin argparse/exit-code entrypoint。
- `backend/scripts/create_m8_review_report.py` — validates and writes an externally decided closed review report。
- `frontend/playwright.m8.config.ts` — external host stack at `http://127.0.0.1:2026`, Chromium-only, no raw artifacts。
- `frontend/tests/e2e-release/m8-host-release.spec.ts` — multi-context real host + DeepSeek + recovery browser journeys。
- `backend/tests/test_m8_host_stack.py`、`backend/tests/test_m8_live_release.py`、`backend/tests/test_m8_recovery_switch_postgres.py` — host/live/recovery orchestration gates。

### Integration and release documentation

- `Makefile` — `M8_RELEASE_POSTGRES_TESTS` ordered superset、`test-project-saas-postgres`、`release-acceptance`。
- `.github/workflows/project-saas-release-gates.yml` — deterministic M1–M8 subset, no model secret and no traffic switch。
- `backend/pyproject.toml`、`backend/uv.lock` — pinned Python auditors。
- `frontend/package.json`、`frontend/pnpm-lock.yaml` — deterministic/live M8 scripts and frozen audit graph。
- `.gitignore` — ignores `/.release-evidence/` only。
- `docs/operations/m8-host-release-acceptance.md` — per-commit two-pass operator runbook, secret handling, recovery and cleanup response。
- `AGENTS.md`、`backend/AGENTS.md`、`frontend/AGENTS.md`、`README.md`、`CHANGELOG.md`、`RELEASING.md`、master M8 spec/plan — final scope and closure evidence。

---

### Task 1: 建立 closed acceptance contracts、stage manifest 和 evidence writer

**Files:**

- Create: `backend/scripts/release_acceptance/__init__.py`
- Create: `backend/scripts/release_acceptance/models.py`
- Create: `backend/scripts/release_acceptance/contracts.py`
- Create: `backend/scripts/release_acceptance/evidence.py`
- Create: `contracts/m8_isolation_matrix.json`
- Create: `contracts/m8_secret_allowlist.json`
- Create: `contracts/m8_release_evidence.schema.json`
- Create: `contracts/m8_review_report.schema.json`
- Create: `backend/tests/test_m8_acceptance_contract.py`
- Create: `backend/tests/test_m8_evidence.py`
- Modify: `.gitignore`

**Interfaces:**

- `StageId` exact order: `preflight`, `contracts`, `postgres`, `backend`, `frontend`, `security`, `host_setup`, `chromium`, `deepseek`, `recovery`, `cleanup`。
- `AcceptanceStatus`: `failed | candidate_ready | final_pass`；`candidate_ready.review.status == "awaiting_review"`；`final_pass` requires exact `ReviewReport` binding。
- `StageEvidence` stores only command ID、timestamps、status、pass/fail/skip counts、duration and bounded code/count summary。
- `ReleaseEvidence` and `ReviewReport` use `ConfigDict(extra="forbid", frozen=True)`；nested types also forbid extras。
- `contract_digest(path)` hashes canonical JSON (`sort_keys=True`, compact separators, UTF-8, SHA-256)。
- `EvidenceWriter.write()` writes to an invocation-owned temp file, `flush + fsync`, `os.replace`, directory fsync, then records SHA-256。
- recursive evidence forbidden-key walker rejects names including `prompt`, `message`, `memory`, `output`, `content`, `payload`, `exception`, `database_url`, IDs for private/business records, secret/cookie/crypto material, and raw path fields at every nesting depth。

- [ ] **Step 1: 写 contract/evidence RED tests**

```python
def test_stage_manifest_is_closed_and_ordered():
    assert [stage.value for stage in STAGE_ORDER] == [
        "preflight", "contracts", "postgres", "backend", "frontend",
        "security", "host_setup", "chromium", "deepseek", "recovery", "cleanup",
    ]
    with pytest.raises(ValidationError):
        StageEvidence.model_validate({**valid_stage(), "stdout": "must-not-persist"})


def test_final_pass_requires_exact_review_binding(candidate):
    report = review_report(candidate, critical=0, important=0, minor=0)
    final = ReleaseEvidence.final(candidate=candidate, review=report)
    assert final.status == "final_pass"
    with pytest.raises(ReviewBindingError):
        ReleaseEvidence.final(candidate=candidate, review=report.model_copy(update={"candidate_digest": "0" * 64}))


def test_writer_rejects_private_fields(tmp_path):
    writer = EvidenceWriter(tmp_path, acceptance_run_id=uuid.uuid4())
    with pytest.raises(ForbiddenEvidenceField):
        writer.write_json("bad.json", {"summary": {"prompt": "synthetic but forbidden"}})
    assert list(tmp_path.iterdir()) == []
```

Add tests for extra nested keys、absolute paths、symlink output roots、partial-write cancellation、manifest self-hash exclusion、candidate/final transition and schema files matching `model_json_schema()` exactly。

- [ ] **Step 2: 运行 RED tests**

```bash
cd backend
uv run pytest tests/test_m8_acceptance_contract.py tests/test_m8_evidence.py -q
```

Expected: FAIL because `scripts.release_acceptance` and generated contracts do not exist。

- [ ] **Step 3: 实现最小 closed models and canonical digest**

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StageEvidence(StrictModel):
    stage: StageId
    command_id: str
    status: Literal["passed", "failed"]
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    summary: dict[str, int | bool | str]


def canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()
```

Do not permit arbitrary `summary`: validate its keys per stage with closed summary models (`TestSummary`, `SecuritySummary`, `LiveModelSummary`, `RecoverySummary`, `CleanupSummary`) and store them as a discriminated union。

- [ ] **Step 4: 生成 committed JSON schemas and seed empty authoritative contracts**

`m8_isolation_matrix.json` starts with schema version、frozen dimension enumerations and an empty `cases` list so Task 2 RED coverage fails honestly。`m8_secret_allowlist.json` starts as `{ "schema_version": 1, "entries": [] }`; no scanner exclusion is added until Task 4 inventories a deterministic fake and its exact digest。

- [ ] **Step 5: 运行 GREEN tests and schema drift check**

```bash
cd backend
uv run pytest tests/test_m8_acceptance_contract.py tests/test_m8_evidence.py -q
uvx ruff format --check scripts/release_acceptance tests/test_m8_acceptance_contract.py tests/test_m8_evidence.py
uvx ruff check scripts/release_acceptance tests/test_m8_acceptance_contract.py tests/test_m8_evidence.py
cd ..
git diff --check
```

Expected: PASS; evidence tests prove no forbidden field is serializable and generated schemas equal committed files byte-for-byte。

- [ ] **Step 6: 提交并审查 Task 1**

```bash
git add .gitignore contracts backend/scripts/release_acceptance backend/tests/test_m8_acceptance_contract.py backend/tests/test_m8_evidence.py
git commit -m "test: define M8 acceptance evidence contracts"
```

Use `superpowers:requesting-code-review` for `HEAD^..HEAD`; repair all findings before Task 2。

### Task 2: 完整化 isolation matrix 并建立 backend/frontend drift gates

**Files:**

- Modify: `contracts/m8_isolation_matrix.json`
- Modify: `backend/scripts/release_acceptance/contracts.py`
- Create: `backend/tests/test_m8_isolation_matrix_postgres.py`
- Modify: `backend/tests/integration/test_project_isolation_postgres.py`
- Modify: `backend/tests/integration/test_m2_project_governance_postgres.py`
- Modify: `backend/tests/integration/test_m3_shared_assets_postgres.py`
- Modify: `backend/tests/integration/test_m3_mcp_credentials_postgres.py`
- Modify: `backend/tests/integration/test_m4_private_work_postgres.py`
- Modify: `backend/tests/integration/test_m5_project_automation_postgres.py`
- Modify: `backend/tests/test_m6_job_repository_postgres.py`
- Modify: `backend/tests/test_m6_durable_stream_postgres.py`
- Modify: `backend/tests/test_m6_audit_integration_postgres.py`
- Create: `frontend/tests/e2e/m8-isolation-matrix.spec.ts`
- Modify: `frontend/tests/e2e/project-private-work-isolation.spec.ts`
- Modify: `frontend/tests/e2e/project-private-chat.spec.ts`

**Interfaces:**

- Every matrix case contains `case_id`, actor/account/project/membership/platform role, `resource_family`, `scope`, `ownership`, `operation`, `expected_status`, `expected_code`, `expected_db_delta`, `layers`, and nonempty `evidence_selectors`。
- Evidence selectors use closed syntax `pytest::<relative-file>::<node-id>` or `playwright::<relative-file>::<exact-title>`; selector collector proves the test exists, is collected, is not skip/xfail and actually ran in final gate。
- Frozen actor families: unauthenticated、project outsider、Admin、Editor、Runner、Viewer、different owner、different project、different account、removed/left/stale membership、pending-deletion/suspended project、system_admin with/without membership and ordinary platform user。
- Frozen resource families include auth/project/membership/invite/lifecycle, Agent/Skill/MCP/version/binding/Credential, Thread/Message/Run/RunEvent/checkpoint/file/artifact, Memory/Connection/Automation/occurrence/result, Job/dead/quota/usage/audit/retention, admin/channel, archive/journal/restore proof。
- Operations include create/list/search/page/get/export/update/delete/publish/bind/approve/run/stop/stream/reconnect/manual/automatic/retry/requeue/restore/purge。
- `discover_scoped_surface()` parses registered final FastAPI routes, repository public methods and scoped frontend client exports; every discovered surface maps to ≥1 matrix case or an exact reviewed non-private exclusion。

- [ ] **Step 1: 写 matrix schema/completeness/drift RED tests**

```python
def test_matrix_covers_every_frozen_dimension(matrix):
    assert matrix.uncovered_dimensions() == ()


def test_every_selector_collects_without_skip(matrix, pytest_collect):
    collected = pytest_collect(matrix.pytest_selectors())
    assert collected.missing == ()
    assert collected.skipped == ()
    assert collected.xfailed == ()


def test_final_scoped_surface_has_matrix_authority(matrix, final_surface):
    assert matrix.unmapped_surface(final_surface) == ()
```

PostgreSQL cases additionally assert denial causes zero row/update/event/audit side effects where contract says `expected_db_delta == 0`, and that wrong child relationships fail composite foreign keys。

- [ ] **Step 2: 运行 RED tests**

```bash
cd backend
uv run pytest tests/test_m8_acceptance_contract.py tests/test_m8_isolation_matrix_postgres.py -q
cd ../frontend
pnpm exec playwright test tests/e2e/m8-isolation-matrix.spec.ts tests/e2e/project-private-work-isolation.spec.ts --list
```

Expected: FAIL because `cases` is empty, selectors are absent and discovered private surface is unmapped。

- [ ] **Step 3: 填充 matrix using existing executable selectors**

Reuse exact existing nodes rather than duplicate broad happy paths, including:

```json
{
  "case_id": "private.run.stream.cross_owner.read",
  "actor": "different_owner",
  "account_relationship": "same_account",
  "project_relationship": "same_project",
  "membership_state": "active_runner",
  "platform_role": "user",
  "resource_family": "run_event",
  "scope": "project_private",
  "ownership": "other_owner",
  "operation": "reconnect",
  "expected_status": 404,
  "expected_code": "NOT_FOUND",
  "expected_db_delta": 0,
  "layers": ["api", "repository", "database"],
  "evidence_selectors": [
    "pytest::tests/integration/test_m4_private_work_postgres.py::test_owner_run_event_feedback_happy_path_is_scope_isolated",
    "pytest::tests/test_m6_durable_stream_postgres.py::test_stream_read_is_strictly_scoped_and_rejects_invalid_contract"
  ]
}
```

Existing selectors may cover multiple cases, but every case must add a focused assertion for its exact expected status/code/side-effect count if the old node only proves a broader condition。

- [ ] **Step 4: Add missing backend and browser denial assertions only**

For gaps, add focused tests for update/delete/export/search/pagination, mass-assignment authority fields, system-admin bounded metadata, stale membership/lifecycle changes and frontend generation invalidation. Product code changes are allowed only after the new test reproduces a real defect。

```ts
test("Link project switch drops a late previous-project response", async ({ page }) => {
  await openProject(page, "project-a");
  const delayed = delayNextThreadList(page);
  await openProject(page, "project-b");
  delayed.release();
  await expect(page.getByText("project-a-private-thread")).toHaveCount(0);
  await expect(page).toHaveURL(/\/projects\/project-b/);
});
```

All names in deterministic fixtures are synthetic and never copied into release evidence。

- [ ] **Step 5: 运行 complete matrix GREEN gate**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run python tests/support/release_gate_plugin.py \
  tests/test_m8_isolation_matrix_postgres.py \
  tests/integration/test_project_isolation_postgres.py \
  tests/integration/test_m2_project_governance_postgres.py \
  tests/integration/test_m3_shared_assets_postgres.py \
  tests/integration/test_m3_mcp_credentials_postgres.py \
  tests/integration/test_m4_private_work_postgres.py \
  tests/integration/test_m5_project_automation_postgres.py -ra
cd ../frontend
pnpm exec playwright test tests/e2e/m8-isolation-matrix.spec.ts tests/e2e/project-private-work-isolation.spec.ts
pnpm check
```

Expected: PASS with 0 PostgreSQL skips, matrix uncovered count 0, no missing/skip/xfail selector and no old-scope response rendered。

- [ ] **Step 6: 提交并审查 Task 2**

```bash
git add contracts/m8_isolation_matrix.json backend/scripts/release_acceptance/contracts.py backend/tests frontend/tests/e2e
git commit -m "test: complete M8 isolation matrix"
```

Review `HEAD^..HEAD`, especially every matrix expected status and `project_id + owner_user_id` predicate; repair all findings。

### Task 3: 固定默认容量边界和最小并发/故障正确性

**Files:**

- Create: `backend/tests/test_m8_capacity_postgres.py`
- Modify: `backend/tests/test_m6_quota_service_postgres.py`
- Modify: `backend/tests/test_m6_job_repository_postgres.py`
- Modify: `backend/tests/test_m6_worker_crash_recovery_postgres.py`
- Modify: `backend/tests/test_m6_gateway_reconnect_process.py`
- Modify: `backend/tests/integration/test_m2_project_governance_postgres.py`
- Modify: `backend/tests/integration/test_m3_mcp_credentials_postgres.py`
- Modify: `backend/app/quotas/service.py` only if a RED case proves a defect
- Modify: `backend/app/private_work/file_service.py` only if a RED case proves a defect
- Modify: `backend/app/projects/invitation_repository.py` only if its RED case proves a defect
- Modify: `backend/app/projects/invitation_service.py` only if its RED case proves a defect
- Modify: `backend/app/reliability/jobs.py` only if its RED case proves a defect
- Modify: `backend/packages/harness/deerflow/persistence/jobs/sql.py` only if its RED case proves a defect
- Modify: `backend/packages/harness/deerflow/runtime/events/store/db.py` only if its RED case proves a defect
- Modify: `backend/app/gateway/routers/private_work.py` only if its RED case proves a defect

**Interfaces and boundaries:**

- Default limits are imported from `QuotaConfig()`: members `20`, storage `5_368_709_120`, concurrent runs `3`, MCP daily `10_000`; tests fail if code and documented boundary drift。
- Concurrent Run test holds three distinct `service.reserve_new_session(seed.owner_a, "concurrent_runs", 1, source_key)` reservations behind an `asyncio.Barrier`; fourth returns `QuotaExceeded`, produces no Run/job, and succeeds after one exact compensation release。
- Member boundary creates 20 active memberships through the real invite claim/redeem service; the 21st returns quota conflict with invitation and membership state unchanged。
- File boundary streams exactly 100 MiB as 64 KiB chunks without constructing a 100 MiB bytes object; 100 MiB + 1 closes/aborts the stream and leaves no staging/file/chunk/usage row。
- Storage 5 GiB boundary is tested through authoritative quota ledger/counter state only; it must never write 5 GiB of file data。
- MCP counter starts at 9,999; call 10,000 consumes once, next admission raises before the mock external transport is called。
- Existing crash/takeover、duplicate delivery、SSE restart/cross-scope cursor and last-Admin/invite race tests gain explicit timeouts and one-terminal/zero-partial-state assertions。

- [ ] **Step 1: 写 boundary/race RED tests**

```python
@pytest.mark.anyio
async def test_default_concurrent_run_limit_is_three_and_fourth_is_atomic(seed):
    async with asyncio.timeout(30):
        service = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
        keys = [f"run:{uuid.uuid4()}" for _ in range(4)]
        reserved = await asyncio.gather(*[
            service.reserve_new_session(seed.owner_a, "concurrent_runs", 1, key)
            for key in keys[:3]
        ])
        assert sorted(result.reserved for result in reserved) == [1, 2, 3]
        with pytest.raises(QuotaExceeded):
            await service.reserve_new_session(seed.owner_a, "concurrent_runs", 1, keys[3])
        await service.release_new_session(_compensation(seed), "concurrent_runs", 1, keys[0])
        accepted = await service.reserve_new_session(seed.owner_a, "concurrent_runs", 1, keys[3])
        assert accepted.reserved == 3
```

Query `runs`, `jobs`, counters and ledger to prove the rejected attempt has no side effect。

```python
async def chunks(total: int, size: int = 64 * 1024):
    remaining = total
    while remaining:
        block = min(size, remaining)
        yield b"x" * block
        remaining -= block
```

Use `PrivateUpload(logical_path="m8-boundary.bin", media_type="application/octet-stream", chunks=chunks(100 * 1024 * 1024))` for the accepted boundary and `chunks(100 * 1024 * 1024 + 1)` for rejection; never persist the synthetic filename in release evidence。

- [ ] **Step 2: 运行 RED boundary tests**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest \
  tests/test_m8_capacity_postgres.py \
  tests/test_m6_quota_service_postgres.py \
  tests/test_m6_job_repository_postgres.py \
  tests/test_m6_worker_crash_recovery_postgres.py \
  tests/test_m6_gateway_reconnect_process.py -q
```

Expected: new M8 file fails until every boundary, side-effect assertion and timeout exists; any product failure remains RED until minimally repaired。

- [ ] **Step 3: 实现最小 defect fixes, if required**

For each product defect, keep the focused failing test, lock authoritative counter/row in the same transaction, apply full scope predicate, and compensate only with server-issued authority. Do not add a benchmark service, new quota dimension, async queue or runtime feature。

Example invariant for rejection:

```python
with pytest.raises(QuotaExceeded):
    await service.consume_new_session(scope, "mcp_calls_daily", 1, overflow_key)
assert transport.calls == 0
assert await counter_value(seed, "mcp_calls_daily") == 10_000
assert await ledger_rows(seed, source_key=overflow_key) == 0
```

- [ ] **Step 4: 运行 GREEN capacity/fault regression**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run python tests/support/release_gate_plugin.py \
  tests/test_m8_capacity_postgres.py \
  tests/test_m6_quota_service_postgres.py \
  tests/test_m6_job_repository_postgres.py \
  tests/test_m6_durable_stream_postgres.py \
  tests/test_m6_worker_crash_recovery_postgres.py \
  tests/test_m6_gateway_reconnect_process.py \
  tests/integration/test_m2_project_governance_postgres.py \
  tests/integration/test_m3_mcp_credentials_postgres.py -ra
uvx ruff format --check app tests/test_m8_capacity_postgres.py
uvx ruff check app tests/test_m8_capacity_postgres.py
```

Expected: PASS, 0 skips, bounded completion; exact durations are available to the future summary but no duration assertion is used for pass/fail。

- [ ] **Step 5: 提交并审查 Task 3**

```bash
git add backend/tests backend/app
git commit -m "test: prove M8 capacity and fault boundaries"
```

Review `HEAD^..HEAD` for transaction isolation, compensation authority, absence of large allocations and exact cleanup; repair all findings。

### Task 4: 建立 threat-control、dependency、secret 和 evidence security gates

**Files:**

- Create: `contracts/m8_threat_controls.json`
- Modify: `contracts/m8_secret_allowlist.json`
- Create: `docs/security/m8-host-threat-model.md`
- Create: `backend/scripts/release_acceptance/security.py`
- Create: `backend/tests/test_m8_security_gates.py`
- Modify: `backend/tests/test_m6_audit_redaction.py`
- Modify: `backend/tests/test_m6_audit_integration_postgres.py`
- Modify: `backend/tests/test_support_bundle.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Modify: `frontend/package.json`
- Modify: `frontend/pnpm-lock.yaml`

**Interfaces:**

- Add locked dev dependencies `pip-audit>=2.9,<3` and `detect-secrets>=1.5,<2`; `BackendDependencyAuditor` exports the locked no-dev workspace graph with `uv export --locked --no-dev --format requirements-txt` into an owned temp file, then runs pinned pip-audit against that file. Frontend audit uses installed `pnpm` and frozen `frontend/pnpm-lock.yaml` production graph。
- `ThreatControl` fields: stable threat ID、attack surface、prevention controls、detective controls、matrix case IDs、test selectors and operator response。Every threat in M8 design §8.1 appears exactly once and references existing executable evidence。
- `DependencyFinding` stores only ecosystem、advisory ID、package name、locked version and database timestamp; `effective_findings > 0` fails。Any exclusion is an exact advisory ID and only valid when the scanner proves package/version is absent from resolved production graph。
- `SecretScanner` covers tracked tree, `review_base..HEAD` diff, every reachable Git blob, `.release-evidence`, generated support bundle and only the byte ranges appended to known host runtime logs after the invocation start offsets. It uses detect-secrets plugins but never prints or stores matched text。
- Secret allowlist entry fields: `scope`, exact relative path, detector rule, `value_sha256`, reason kind `test_fake | documentation_placeholder`; no glob, regex path or wildcard digest。
- finding output contains detector rule、scope、object/path locator digest and line number only. A historical finding cannot be auto-rewritten or auto-allowlisted。

- [ ] **Step 1: 写 threat/dependency/secret RED tests**

```python
def test_every_threat_has_preventive_detective_and_executable_evidence(threats, matrix):
    assert threats.missing_required_families() == ()
    for threat in threats.items:
        assert threat.prevention_controls
        assert threat.detective_controls
        assert set(threat.matrix_case_ids) <= matrix.case_ids
        assert threat.test_selectors


def test_secret_finding_never_contains_match_value(tmp_path):
    secret_file = tmp_path / "fixture.txt"
    secret_file.write_text("synthetic-secret-shape-for-detector")
    finding = scan_file_for_test(secret_file)[0]
    encoded = finding.model_dump_json()
    assert "synthetic-secret-shape-for-detector" not in encoded
    assert finding.locator_digest


def test_allowlist_requires_exact_path_rule_and_digest():
    with pytest.raises(ValidationError):
        SecretAllowlistEntry(path="backend/tests/**", rule="*", value_sha256="*")
```

Also test private key、provider token shapes、database URLs、cookies、JWTs、nonces/ciphertexts, binary Git blobs, renamed historical blob, support bundle and evidence tree. Use generated synthetic values inside temp files, not credential-like literals committed to tracked test source。

- [ ] **Step 2: 运行 RED security tests and baseline audits**

```bash
cd backend
uv run pytest tests/test_m8_security_gates.py tests/test_m6_audit_redaction.py tests/test_support_bundle.py -q
uv run pip-audit --locked --format json
cd ../frontend
pnpm audit --prod --audit-level low --json
```

Expected: tests FAIL before the security package/contracts exist. The direct Python audit is diagnostic only; the final gate uses the exported no-dev graph. Audit commands may reveal real advisories; record only advisory IDs/package/version, then upgrade/remove affected production dependencies rather than adding accepted-risk suppressions。

- [ ] **Step 3: 实现 bounded scanner and exact exclusions**

```python
class SecretFinding(StrictModel):
    scope: Literal["tracked_tree", "review_diff", "git_history", "evidence", "support_bundle", "runtime_logs"]
    rule: str
    locator_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    line: int | None = Field(default=None, ge=1)


def locator_digest(scope: str, locator: str) -> str:
    return hashlib.sha256(f"m8-secret-locator\0{scope}\0{locator}".encode()).hexdigest()
```

Use `git ls-files -z`, `git diff --binary 3f574b89..HEAD`, `git rev-list --objects --all` and batched `git cat-file --batch` without checkout. Cap blob size, but report and fail on an unscanned oversized/non-regular object instead of silently skipping it. Feed content to detect-secrets in memory; clear buffers after hashing. Inventory current known test/document placeholders and add allowlist rows only after manual review of exact path/rule/digest。

- [ ] **Step 4: Close audit/redaction/support-bundle findings**

Run scanners over changed runtime, current history and generated support bundle. If a real credential is detected, stop the task, rotate it outside the repository, remove generated copies and report that history remediation needs explicit operator action; never invoke history rewrite automatically。

- [ ] **Step 5: 运行 full security GREEN gate**

```bash
cd backend
uv run pytest tests/test_m8_security_gates.py tests/test_m6_audit_redaction.py tests/test_m6_audit_integration_postgres.py tests/test_support_bundle.py -q
uv run python -m scripts.release_acceptance.security --scope dependencies-backend
uv run python -m scripts.release_acceptance.security --scope tracked-tree --scope git-history --review-base 3f574b89
cd ../frontend
pnpm install --frozen-lockfile
pnpm audit --prod --audit-level low
cd ..
git diff --check
```

Expected: 0 effective dependency finding, 0 unallowlisted secret finding, complete threat mapping, redacted audit/support bundle and no value echo。

- [ ] **Step 6: 提交并审查 Task 4**

```bash
git add contracts docs/security backend/scripts/release_acceptance/security.py backend/tests backend/pyproject.toml backend/uv.lock frontend/package.json frontend/pnpm-lock.yaml
git commit -m "test: establish M8 security release gates"
```

Review `HEAD^..HEAD` with special attention to scanner blind spots、allowlist precision、raw-error leakage and whether dependency exclusions actually prove graph absence; repair all findings。

### Task 5: 实现 preflight、immutable command manifest、ownership ledger 和 always-cleanup runner

**Files:**

- Create: `backend/scripts/release_acceptance/preflight.py`
- Create: `backend/scripts/release_acceptance/ownership.py`
- Create: `backend/scripts/release_acceptance/commands.py`
- Create: `backend/scripts/release_acceptance/runner.py`
- Create: `backend/scripts/run_release_acceptance.py`
- Create: `backend/tests/test_m8_preflight.py`
- Create: `backend/tests/test_m8_ownership.py`
- Create: `backend/tests/test_m8_release_runner.py`
- Create: `backend/tests/blocking_io/test_m8_release_acceptance.py`
- Modify: `backend/scripts/release_acceptance/models.py`
- Modify: `backend/scripts/release_acceptance/evidence.py`

**Interfaces:**

- `Preflight.check()` is read-only and ordered: live switch → Git clean/non-detached/exact commit → current config version and no tombstoned keys → exactly one DeepSeek config with provider ID `deepseek-v4-pro` → required secret presence boolean → tool versions → Chromium executable → free ports `2026/3000/8001` → PostgreSQL maintenance authority and least-privilege app-role requirements。
- Failure returns stable missing/error code names only (`M8_LIVE_ACCEPTANCE_REQUIRED`, `GIT_TREE_NOT_CLEAN`, `CONFIG_VERSION_MISMATCH`, `DEEPSEEK_MODEL_NOT_UNIQUE`, `DEEPSEEK_API_KEY_MISSING`, etc.); it never includes values, URLs, raw exceptions or paths。
- `CommandSpec` has fixed ID、argv tuple、cwd relative enum、timeout、environment allowlist and summary parser. No command is accepted from CLI, evidence or contract JSON。
- `OwnershipLedger` records `OwnedProcess(pid, pgid, start_identity)`, `OwnedDatabase(name, owner, marker_digest)`, `OwnedPath(relative_token, device, inode, kind, disposition)` and reserved port. `disposition` is `temporary | retained_evidence`; only the validated `.release-evidence/<acceptance_run_id>` root may be retained, while every other owned path must be removed. It is in-memory authority; persisted evidence only records resource counts, retained-evidence count and temporary residual counts。
- `ReleaseRunner.run()` pins commit/manifest digest, creates evidence root after preflight, registers SIGINT/SIGTERM, executes fixed stages, stops business stages on first failure, always executes cleanup, rechecks commit, seals failed/candidate/final status atomically。
- Async orchestration uses `asyncio.create_subprocess_exec`, async database/HTTP clients and `asyncio.to_thread` for fsync/scanner filesystem work; no long subprocess/file/network operation runs synchronously on the event loop。
- CLI supports no resume and no arbitrary database/command flags. Required environment names: `M8_LIVE_ACCEPTANCE`, `M8_REVIEW_REPORT`, `POSTGRES_ADMIN_URL`, `DATABASE_URL`, existing recovery key env and `DEEPSEEK_API_KEY`。

- [ ] **Step 1: 写 side-effect-free preflight RED tests**

```python
def test_missing_live_switch_fails_before_any_side_effect(fake_ports, fake_database):
    result = Preflight(env={}).check()
    assert result.code == "M8_LIVE_ACCEPTANCE_REQUIRED"
    assert fake_ports.bind_calls == 0
    assert fake_database.connect_calls == 0


def test_provider_model_id_is_unique_but_logical_name_may_differ(config_factory):
    config = config_factory(models=[deepseek(name="deepseek-live", model="deepseek-v4-pro")])
    result = Preflight(valid_env(), config_loader=lambda: config).check()
    assert result.model.logical_name == "deepseek-live"
    assert result.model.provider_model_id == "deepseek-v4-pro"


def test_duplicate_provider_model_id_fails_without_echo(config_factory):
    result = Preflight(valid_env(), config_loader=lambda: config_factory(models=[deepseek("a"), deepseek("b")])).check()
    assert result.code == "DEEPSEEK_MODEL_NOT_UNIQUE"
    assert "sk-" not in result.model_dump_json()
```

Add dirty tree、detached HEAD、commit changed during run、removed config fields、busy port、DB app superuser、missing binary and raw-exception redaction cases。

- [ ] **Step 2: 写 ownership/cancellation RED tests**

```python
def test_cleanup_refuses_reused_pid(ledger, process_probe):
    owned = ledger.register_process(pid=41001, pgid=41001, start_identity="first")
    process_probe.set_identity(41001, "second")
    result = ledger.stop_process(owned)
    assert result.status == "identity_mismatch"
    assert process_probe.signals == []


@pytest.mark.anyio
async def test_runner_failure_still_cleans_exact_resources(runner):
    runner.stage("postgres").raises(RuntimeError("raw private error"))
    evidence = await runner.run()
    assert evidence.status == "failed"
    assert evidence.cleanup.residual_processes == 0
    assert "raw private error" not in evidence.model_dump_json()
```

Add symlink/inode replacement、database marker mismatch、database name validation、unknown resource quarantine、only-evidence-may-be-retained、SIGTERM and cleanup failure overriding candidate/final cases。

- [ ] **Step 3: 运行 RED tests**

```bash
cd backend
uv run pytest tests/test_m8_preflight.py tests/test_m8_ownership.py tests/test_m8_release_runner.py -q
uv run pytest tests/blocking_io/test_m8_release_acceptance.py -q
```

Expected: FAIL because preflight/ownership/runner do not exist。

- [ ] **Step 4: 实现 immutable commands and identity checks**

```python
@dataclass(frozen=True, slots=True)
class CommandSpec:
    command_id: str
    argv: tuple[str, ...]
    cwd: Literal["root", "backend", "frontend"]
    timeout_seconds: int
    allowed_environment: frozenset[str]


COMMANDS = (
    CommandSpec("postgres.m1_m8", ("make", "test-project-saas-postgres"), "root", 3600, frozenset({"POSTGRES_TEST_URL"})),
    CommandSpec("backend.full", ("uv", "run", "pytest", "-q"), "backend", 3600, frozenset()),
    CommandSpec("backend.blocking_io", ("uv", "run", "pytest", "tests/blocking_io", "-q"), "backend", 1800, frozenset()),
    CommandSpec("frontend.unit", ("pnpm", "test"), "frontend", 1800, frozenset()),
)
```

Complete the tuple in Task 9 with all fixed stages. Command runner consumes stdout/stderr through bounded parsers, never stores raw lines, and kills only the exact child process group on timeout。

- [ ] **Step 5: 实现 fail-fast runner and atomic failure evidence**

`try/except/finally` must always run cleanup; a failed cleanup changes status to failed even when all business stages passed. Candidate/final seal occurs only after cleanup and a second `git rev-parse HEAD` / `git status --porcelain` check。

- [ ] **Step 6: 运行 GREEN orchestration unit gates**

```bash
cd backend
uv run pytest tests/test_m8_preflight.py tests/test_m8_ownership.py tests/test_m8_release_runner.py tests/test_m8_acceptance_contract.py tests/test_m8_evidence.py -q
uv run pytest tests/blocking_io/test_m8_release_acceptance.py -q
uvx ruff format --check scripts/release_acceptance scripts/run_release_acceptance.py tests/test_m8_*.py
uvx ruff check scripts/release_acceptance scripts/run_release_acceptance.py tests/test_m8_*.py
```

Expected: PASS; unit fakes prove no side effect before preflight, exact identity cleanup, no resume and failed/cancelled evidence redaction。

- [ ] **Step 7: 提交并审查 Task 5**

```bash
git add backend/scripts/release_acceptance backend/scripts/run_release_acceptance.py backend/tests/test_m8_preflight.py backend/tests/test_m8_ownership.py backend/tests/test_m8_release_runner.py backend/tests/blocking_io/test_m8_release_acceptance.py
git commit -m "feat: add M8 release acceptance orchestrator"
```

Review `HEAD^..HEAD` for destructive cleanup risk, command injection, environment leakage, signal races and commit-binding correctness; repair all findings。

### Task 6: 启动 exact host production stack 并完成 real Chromium multi-context journey

**Files:**

- Create: `backend/scripts/release_acceptance/host_stack.py`
- Create: `backend/scripts/release_acceptance/live_probe.py`
- Create: `backend/tests/test_m8_host_stack.py`
- Create: `frontend/playwright.m8.config.ts`
- Create: `frontend/tests/e2e-release/m8-host-release.spec.ts`
- Create: `frontend/tests/e2e-release/support/m8-api.ts`
- Create: `frontend/tests/e2e-release/support/m8-result.ts`
- Modify: `frontend/package.json`
- Modify: `scripts/serve.sh`
- Modify: `backend/tests/test_serve_nginx_stop.py`
- Modify: `backend/scripts/release_acceptance/commands.py`
- Modify: `backend/scripts/release_acceptance/runner.py`

**Interfaces:**

- `OwnedHostStack.start(database_url)` derives a random source DB URL in memory from the application URL, creates that database through `POSTGRES_ADMIN_URL` with an invocation marker and exact app role owner, then executes root `make setup-db`, `make check-db` and root `make start` with environment override。
- Ordinary `scripts/serve.sh --prod` no longer calls `stop_all` before start; existing listeners/processes fail closed. Explicit `--stop`/`--restart` keep the operator-owned broad action. Foreground INT/TERM cleanup calls `stop_started` for this invocation, not `stop_all`。
- Preflight requires ports `8001`, `3000`, `2026` free and no DeerFlow process in any discovered DeerFlow worktree before `make start`; the acceptance cleanup never calls broad `make stop`。
- `make start` is launched with `start_new_session=True`; runner records leader PID/PGID/start identity and discovers Gateway/Worker/optional Scheduler/Frontend/Nginx descendants by PGID plus exact command/port fingerprints。
- `HostStack.stop()` sends TERM then bounded KILL only to the exact verified process group; after exit it proves owned ports are free and no descendant identity remains。
- Gateway restart kills only the verified Gateway descendant and starts the final backend `make gateway` command in a new owned process group with the same ephemeral environment; Worker/Frontend/Nginx remain running。
- `playwright.m8.config.ts` has `testDir: "./tests/e2e-release"`, one desktop Chromium project, `workers: 1`, `retries: 0`, `baseURL: http://127.0.0.1:2026`, no `webServer`, `trace: "off"`, `video: "off"`, `screenshot: "off"`, line reporter and an invocation-owned output directory。
- Browser contexts create only synthetic accounts. One fresh account is initialized as `system_admin`; other accounts register normally. API helpers use same-origin `/api/*`, CSRF/session cookies from context and server-issued project/capability/asset IDs only。
- Playwright writes one closed `M8BrowserResult` JSON containing boolean/count/status fields only; no email、password、project slug、URL、UUID、response body or DOM text。

- [ ] **Step 1: 写 host ownership/process RED tests**

```python
@pytest.mark.anyio
async def test_host_stack_invokes_only_certified_setup_start_path(host, command_probe):
    await host.start("redacted-in-memory-url")
    assert command_probe.command_ids == ["host.setup_db", "host.check_db", "host.make_start"]
    assert "docker" not in " ".join(command_probe.argv)
    assert "helm" not in " ".join(command_probe.argv)


@pytest.mark.anyio
async def test_host_stop_never_calls_broad_make_stop(host, process_probe):
    await host.stop()
    assert process_probe.signalled_groups == [(host.pgid, signal.SIGTERM)]
    assert ("make", "stop") not in process_probe.commands
```

Add a shell contract asserting ordinary start has no pre-start `stop_all`, foreground `cleanup()` calls only `stop_started`, and only explicit stop/restart paths call `stop_all`. Also add startup partial failure、child identity substitution、busy port、Scheduler enabled/disabled、Gateway-only restart and residual listener cases。

- [ ] **Step 2: 写 Chromium journey RED test**

```ts
test("host release boundaries survive account and project transitions", async ({ browser }) => {
  const systemAdmin = await browser.newContext();
  const accountA = await browser.newContext();
  const accountB = await browser.newContext();
  const adminSession = await initializeAdmin(systemAdmin);
  await registerAccount(accountA);
  await registerAccount(accountB);
  const projectA = await createProject(accountA, syntheticProject("a"));
  const projectB = await createProject(accountA, syntheticProject("b"));

  await expectProjectVisible(accountA, projectA);
  await expectProjectVisible(accountA, projectB);
  await expectProjectNotFound(accountB, projectA.id);
  await expectAdminRouteVisible(systemAdmin, adminSession);
  await expectAdminRouteNotFound(accountA);
  await proveProjectTransitionDropsLateData(accountA, projectA, projectB);
  await writeBrowserResult({ boundariesPassed: 6, failures: 0 });
});
```

Expand the same journey to Admin/Editor/Runner/Viewer navigation, same-project different owner private Thread/File/Memory/Automation denial, shared asset safe status, Viewer read/export/delete allowances and create/run denial. `M8BrowserResult` counts assertions only。

- [ ] **Step 3: 运行 RED unit/list gates**

```bash
cd backend
uv run pytest tests/test_m8_host_stack.py tests/test_m8_release_runner.py -q
cd ../frontend
pnpm exec playwright test --config playwright.m8.config.ts --list
pnpm check
```

Expected: FAIL because host stack/config/journey/helpers do not exist。

- [ ] **Step 4: 实现 exact process-session host stack**

```python
process = await asyncio.create_subprocess_exec(
    "make",
    "start",
    cwd=repo_root,
    env=ephemeral_environment,
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.STDOUT,
)
owned = ledger.register_process(
    pid=process.pid,
    pgid=os.getpgid(process.pid),
    start_identity=process_probe.start_identity(process.pid),
)
```

Consume startup lines only through a bounded state parser (`Gateway started`, `Worker started`, `Frontend started`, `Nginx started`) and discard raw lines. Read readiness through same-origin `http://127.0.0.1:2026/api/projects` plus bounded process-readiness endpoints/models, not log copying。

- [ ] **Step 5: 实现 no-artifact Playwright result handoff**

`writeBrowserResult()` resolves only `M8_BROWSER_RESULT_PATH`, verifies it is inside the invocation output root and writes with `flag: "wx"`; test failure throws a stable code. The Python runner validates the closed JSON then deletes the raw Playwright output directory after summary ingestion。

- [ ] **Step 6: 运行 real host/Chromium GREEN gate without external model**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest tests/test_m8_host_stack.py -q
cd ..
M8_LIVE_ACCEPTANCE=1 uv run --directory backend python scripts/run_release_acceptance.py --stage host_setup --stage chromium
```

Expected: fresh random source DB setup/check/start succeeds; Chromium boundaries pass; cleanup reports 0 process/port/database/path residual. The stage-only command is diagnostic and cannot produce `candidate_ready` or `final_pass`。

- [ ] **Step 7: 提交并审查 Task 6**

```bash
git add scripts/serve.sh backend/tests/test_serve_nginx_stop.py backend/scripts/release_acceptance backend/tests/test_m8_host_stack.py frontend/playwright.m8.config.ts frontend/tests/e2e-release frontend/package.json
git commit -m "test: verify M8 host Chromium release path"
```

Review `HEAD^..HEAD` for use of exact root commands, browser artifact leakage, cookie/CSRF handling, role boundaries and process cleanup; repair all findings。

### Task 7: 验证 DeepSeek `deepseek-v4-pro` admission、durable stream 和 tool 闭环

**Files:**

- Modify: `backend/scripts/release_acceptance/preflight.py`
- Modify: `backend/scripts/release_acceptance/live_probe.py`
- Modify: `backend/scripts/release_acceptance/runner.py`
- Create: `backend/tests/test_m8_live_release.py`
- Modify: `frontend/tests/e2e-release/m8-host-release.spec.ts`
- Modify: `frontend/tests/e2e-release/support/m8-api.ts`
- Modify: `frontend/tests/e2e-release/support/m8-result.ts`
- Modify: `backend/tests/test_m8_preflight.py`

**Interfaces:**

- `resolve_live_model(AppConfig)` returns exactly `LiveModelRef(logical_name, provider="deepseek", provider_model_id="deepseek-v4-pro")` when exactly one model has `model == "deepseek-v4-pro"` and `use == "deerflow.models.patched_deepseek:PatchedChatDeepSeek"`。
- The system-admin browser/API setup creates a project Agent version with `model_ref=live_model.logical_name` and `tool_groups=["file:read", "file:write"]`, publishes/binds it and passes only server-returned immutable version/snapshot references to run admission。
- Fixed synthetic prompt asks the Agent to invoke `write_file` at least once and then finish. The prompt and generated path stay in Playwright process memory/test source and are never written to evidence or logs。
- Live result is determined from three authorities: Chromium observed a terminal UI state; PostgreSQL has multiple scoped stream frames and exactly one terminal; PostgreSQL has ≥1 `llm.tool.result` plus the project-private file/artifact created by the tool。
- Refresh and Gateway-only restart replay from `Last-Event-ID` without duplicate terminal; another account/project/owner receives public 404 for Thread、Run、event stream and artifact。
- `LiveModelSummary` stores provider、logical model name、provider model ID、HTTP/run outcome enum、frame count、tool-call count、terminal count、cursor count and duration only. It never contains business Run/thread/file IDs or any response body。

- [ ] **Step 1: 写 model-resolution and redaction RED tests**

```python
def test_live_model_resolves_by_provider_model_id_not_logical_name(config_factory):
    ref = resolve_live_model(config_factory(models=[deepseek(name="release-live", model="deepseek-v4-pro")]))
    assert ref.logical_name == "release-live"
    assert ref.provider_model_id == "deepseek-v4-pro"


def test_live_summary_rejects_model_body_and_business_ids():
    with pytest.raises(ValidationError):
        LiveModelSummary.model_validate({**valid_live_summary(), "content": "provider text"})
    with pytest.raises(ValidationError):
        LiveModelSummary.model_validate({**valid_live_summary(), "run_id": str(uuid.uuid4())})
```

Add zero/duplicate model config、wrong provider class、key absent、provider raw exception and secret-like stdout cases。

- [ ] **Step 2: 写 Playwright live journey RED assertions**

```ts
const live = await createPinnedLiveAgent(adminContext, project, {
  modelRef: process.env.M8_LOGICAL_MODEL_NAME!,
  toolGroups: ["file:read", "file:write"],
});
await submitSyntheticToolPrompt(adminPage, live);
await expectRunTerminal(adminPage);
await expectToolResultVisible(adminPage);
await reloadAndResumeFromLastCursor(adminPage);
await expectPrivateRunNotFound(otherAccount, live.publicHandle);
```

The `publicHandle` remains in memory and is not part of `M8BrowserResult`; browser result writes counts/outcome only。

- [ ] **Step 3: 运行 deterministic RED tests**

```bash
cd backend
uv run pytest tests/test_m8_live_release.py tests/test_m8_preflight.py tests/test_m8_evidence.py -q
cd ../frontend
pnpm exec playwright test --config playwright.m8.config.ts --list
```

Expected: FAIL until live model reference, closed summary and Playwright live test are implemented; no network model is called by these tests。

- [ ] **Step 4: 实现 bounded DB live probe**

```python
counts = await session.execute(
    text("""
        SELECT
          count(*) FILTER (WHERE category = 'stream') AS frame_count,
          count(*) FILTER (WHERE event_type = 'llm.tool.result') AS tool_count,
          count(*) FILTER (WHERE event_type = 'stream.end') AS terminal_count
        FROM run_events
        WHERE account_id=:account_id AND project_id=:project_id
          AND owner_user_id=:owner_user_id AND thread_id=:thread_id AND run_id=:run_id
    """),
    scope_params,
)
```

This query is internal and its identifiers are never serialized. Require `frame_count > 1`, `tool_count >= 1`, `terminal_count == 1`; separately query exact scoped file/artifact count and cross-scope 404 responses。

- [ ] **Step 5: 运行 paid local live GREEN diagnostic**

Before this command, the operator exports the key in the current shell without echoing it. Do not place the value on the command line。

```bash
M8_LIVE_ACCEPTANCE=1 uv run --directory backend python scripts/run_release_acceptance.py --stage host_setup --stage chromium --stage deepseek
```

Expected: PASS with `provider=deepseek`, provider model ID `deepseek-v4-pro`, frame count >1, tool-call count ≥1, terminal count 1, replay/cross-scope denial true and 0 cleanup residual. The diagnostic cannot generate final status。

- [ ] **Step 6: 运行 no-secret source/evidence scan**

```bash
cd backend
uv run pytest tests/test_m8_live_release.py tests/test_m8_security_gates.py -q
uv run python -m scripts.release_acceptance.security --scope tracked-tree --scope evidence --scope runtime-logs --review-base 3f574b89
```

Expected: PASS and 0 unallowlisted finding; neither command prints key、prompt、model text、tool args/result or business IDs。

- [ ] **Step 7: 提交并审查 Task 7**

```bash
git add backend/scripts/release_acceptance backend/tests/test_m8_live_release.py backend/tests/test_m8_preflight.py frontend/tests/e2e-release
git commit -m "test: prove M8 DeepSeek live execution"
```

Review `HEAD^..HEAD` for provider resolution, actual Worker-only execution, durable event authority, tool-call proof, cross-scope denial and secret/private-data non-persistence; repair all findings。

### Task 8: 执行 version-7 archive、post-backup tombstone、traffic switch 和 back-switch

**Files:**

- Create: `backend/scripts/release_acceptance/recovery_drill.py`
- Create: `backend/tests/test_m8_recovery_switch_postgres.py`
- Modify: `backend/scripts/release_acceptance/runner.py`
- Modify: `backend/scripts/release_acceptance/host_stack.py`
- Modify: `backend/scripts/release_acceptance/live_probe.py`
- Modify: `backend/scripts/release_acceptance/models.py`
- Modify: `backend/tests/test_m6_restore_postgres.py`
- Modify: `backend/tests/test_m7_backup_restore_postgres.py`
- Modify: `frontend/tests/e2e-release/m8-host-release.spec.ts`

**Interfaces:**

- `RecoverySwitchDrill` receives the already-owned source application DB URL/stack, an operator URL derived in memory from `POSTGRES_ADMIN_URL` and pinned to the same source database, and in-memory synthetic session authority. It does not accept arbitrary database URLs from CLI。
- It creates invocation-owned archive/journal/proof directories with inode records; backup uses existing `create_backup(BackupConfig(database_url=source_app_url, output=archive_path, key=backup_key, archive_id=archive_id))`, purge uses existing journal-first `purge_private_scope`, restore uses `Restorer(RestoreConfig(archive=archive_path, target_database_url=target_operator_url, current_database_url=source_operator_url, journal=journal, backup_key=backup_key, keyring=keyring)).restore()` and requires `restorer.owns_verified_target(result)` before registering/switching target。Restored production stack uses the least-privilege `target_app_url`, never the operator URL。
- `ExpectedInventory` contains only domain-separated digests and counts in memory. Evidence stores only counts、schema revision、archive schema version、tombstone count、public proof digest、RTO ms、RPO outcome enum and cleanup counts。
- After archive, one real private retention purge appends/fsyncs journal before deletion. A separate non-delete row is inserted after the archive; restored DB must exclude it, while journal-deleted pre-backup data must not revive。
- Traffic switch stops source stack, starts a new host stack with an environment-only restore `DATABASE_URL`, runs browser health/login/project/shared/private/live/cross-scope probe, stops it, starts source stack again and runs bounded health/login/project probe。
- It never edits `config.yaml`, `.env`, shell profiles or user database targets. Source is retained until back-switch succeeds; cleanup only drops verified owned source/restore DBs after all stacks stop。

- [ ] **Step 1: 写 recovery sequencing/ownership RED tests**

```python
@pytest.mark.anyio
async def test_recovery_switch_order_is_archive_purge_stop_restore_switch_back(fake_drill):
    await fake_drill.run()
    assert fake_drill.events == [
        "inventory", "archive", "journal_purge", "post_backup_row",
        "source_stop", "restore", "restore_probe", "restore_start",
        "browser_probe", "restore_stop", "source_start", "back_switch_probe",
    ]


@pytest.mark.anyio
async def test_unverified_restore_target_is_never_switched_or_dropped(fake_restorer, ledger):
    fake_restorer.owns_verified_target.return_value = False
    with pytest.raises(RecoveryOwnershipError):
        await run_restore_phase(fake_restorer, ledger)
    assert ledger.database_drops == []
```

Add wrong archive version、tamper、wrong key、journal gap/reorder/source mismatch、restore proof substitution、purged-data revival、post-backup row unexpectedly present、browser failure、back-switch failure、cancellation at each phase and cleanup quarantine cases。

- [ ] **Step 2: 运行 RED recovery tests**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run pytest \
  tests/test_m8_recovery_switch_postgres.py \
  tests/test_m6_restore_postgres.py \
  tests/test_m7_backup_restore_postgres.py -q
```

Expected: FAIL because full traffic switch coordinator does not exist; existing restore tests remain GREEN independently。

- [ ] **Step 3: 实现 restore handoff and RTO/RPO summary**

```python
restore_started = time.monotonic_ns()
result = await restorer.restore()
if not restorer.owns_verified_target(result):
    raise RecoveryOwnershipError("RESTORE_TARGET_NOT_OWNED")
ledger.register_verified_database(target, marker_digest=expected_marker)
await restore_stack.start(target.in_memory_url)
await browser_probe.restore_authority()
rto_ms = (time.monotonic_ns() - restore_started) // 1_000_000
```

Set RTO start at completed source stop, not archive start. `rpo_outcome` is `archive_point_confirmed` only when post-backup non-delete row is absent and all pre-backup non-purged rows are present. No threshold comparison exists。

- [ ] **Step 4: 运行 real PostgreSQL recovery GREEN gate**

```bash
cd backend
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" uv run python tests/support/release_gate_plugin.py \
  tests/test_m8_recovery_switch_postgres.py \
  tests/test_m7_backup_restore_postgres.py \
  tests/test_m6_restore_postgres.py \
  tests/test_m6_retention_purge_postgres.py -ra
```

Expected: PASS, 0 skips; test creates/drops only random owned DBs and reports no residual archive/journal/proof temp path。

- [ ] **Step 5: 运行 full local recovery diagnostic**

```bash
M8_LIVE_ACCEPTANCE=1 uv run --directory backend python scripts/run_release_acceptance.py \
  --stage host_setup --stage chromium --stage deepseek --stage recovery
```

Expected: archive schema 7, revision `0001_project_saas_baseline`, tombstone replay >0, restore browser probe and back-switch pass, RTO/RPO recorded without threshold, cleanup residual 0. This diagnostic cannot emit final status。

- [ ] **Step 6: 提交并审查 Task 8**

```bash
git add backend/scripts/release_acceptance backend/tests/test_m8_recovery_switch_postgres.py backend/tests/test_m6_restore_postgres.py backend/tests/test_m7_backup_restore_postgres.py frontend/tests/e2e-release/m8-host-release.spec.ts
git commit -m "test: verify M8 recovery traffic switch"
```

Review `HEAD^..HEAD` for archive/journal authority, restored-target ownership, data revival, switch isolation, source preservation, RTO/RPO semantics and exact cleanup; repair all findings。

### Task 9: 固定 M1–M8 release gate、CI subset、root entry 和 operator runbook

**Files:**

- Modify: `Makefile`
- Delete: `.github/workflows/project-foundation-postgres-tests.yml`
- Create: `.github/workflows/project-saas-release-gates.yml`
- Modify: `.github/workflows/backend-unit-tests.yml`
- Modify: `.github/workflows/frontend-unit-tests.yml`
- Modify: `.github/workflows/e2e-tests.yml`
- Create: `backend/tests/test_m8_release_gate_postgres.py`
- Modify: `backend/tests/support/release_gate_plugin.py`
- Modify: `backend/scripts/release_acceptance/commands.py`
- Modify: `backend/scripts/release_acceptance/runner.py`
- Create: `backend/scripts/create_m8_review_report.py`
- Create: `backend/tests/test_m8_review_report_cli.py`
- Modify: `frontend/package.json`
- Create: `docs/operations/m8-host-release-acceptance.md`
- Modify: `docs/operations/m6-backup-recovery.md`
- Modify: `RELEASING.md`
- Modify: `AGENTS.md`
- Modify: `backend/AGENTS.md`
- Modify: `frontend/AGENTS.md`

**Interfaces:**

- Preserve `PROJECT_FOUNDATION_POSTGRES_TESTS` as the immutable ordered M1–M7 22-file prefix and its diagnostic target。
- Add `M8_RELEASE_POSTGRES_TESTS = $(PROJECT_FOUNDATION_POSTGRES_TESTS)` followed only by `tests/test_m8_isolation_matrix_postgres.py`, `tests/test_m8_capacity_postgres.py`, `tests/test_m8_recovery_switch_postgres.py`, `tests/test_m8_release_gate_postgres.py`。
- `test-project-saas-postgres` is the final M1–M8 real PostgreSQL 0-skip gate; root `make test` switches to it. `print-project-saas-postgres-tests` is the only final expanded list output。
- `release_gate_plugin.py` reads exact `DEER_FLOW_RELEASE_GATE_LABEL` (`M1-M7` or `M1-M8`) and reports label、collected、passed、failed、skipped; any skip fails。
- Root `make release-acceptance` only invokes `cd backend && uv run python scripts/run_release_acceptance.py`; it does not inline stages, accept `ARGS`, run Docker/Helm or mutate environment。
- `create_m8_review_report.py` accepts candidate manifest, exact review base/range, three numeric finding counts and output path. It validates candidate/commit/manifest digests and only writes a closed report; it never decides a verdict or converts nonzero counts to pass。
- CI checks out full history, runs deterministic contract/M1–M8/backend/frontend/security gates and Chromium mock/replay tests. It explicitly has no `DEEPSEEK_API_KEY`, no `M8_LIVE_ACCEPTANCE=1` and no full recovery traffic switch。
- Runbook documents candidate/review/final operation, key environment handling without shell history value, quarantine response, credential rotation if detected, evidence retention, and explicit non-certification of Docker Compose/Kubernetes/Helm/Firefox/Safari/other providers。

- [ ] **Step 1: 写 Makefile/CI/manifest drift RED tests**

```python
EXPECTED_M8_SUFFIX = (
    "tests/test_m8_isolation_matrix_postgres.py",
    "tests/test_m8_capacity_postgres.py",
    "tests/test_m8_recovery_switch_postgres.py",
    "tests/test_m8_release_gate_postgres.py",
)


def test_m1_m8_gate_is_exact_m1_m7_prefix_plus_m8_suffix(makefile_lists):
    assert makefile_lists["M8_RELEASE_POSTGRES_TESTS"] == (
        *makefile_lists["PROJECT_FOUNDATION_POSTGRES_TESTS"],
        *EXPECTED_M8_SUFFIX,
    )


def test_ci_uses_makefile_authority_without_duplicating_test_paths(workflow):
    assert "make test-project-saas-postgres" in workflow
    assert not any(path in workflow for path in EXPECTED_M8_SUFFIX)
    assert "DEEPSEEK_API_KEY" not in workflow
    assert "M8_LIVE_ACCEPTANCE" not in workflow
```

Add root `test` dependency、help text、plugin label/zero-skip、full-history checkout、fixed command order and no Docker/Helm stage cases。

- [ ] **Step 2: 写 review-report CLI RED tests**

```python
def test_report_binds_candidate_commit_manifest_and_evidence(tmp_path, candidate_manifest):
    output = tmp_path / "review.json"
    result = run_cli(candidate_manifest, output, critical=0, important=0, minor=0)
    report = ReviewReport.model_validate_json(output.read_text())
    assert result == 0
    assert report.candidate_commit == candidate_manifest.git_commit
    assert report.stage_manifest_digest == candidate_manifest.stage_manifest_digest
    assert report.candidate_evidence_digest == candidate_manifest.manifest_sha256


def test_nonzero_report_is_valid_review_record_but_cannot_unlock_final(candidate_manifest):
    report = create_report(candidate_manifest, critical=0, important=0, minor=1)
    assert report.verdict == "findings_present"
    with pytest.raises(ReviewBindingError):
        validate_final_report(report)
```

- [ ] **Step 3: 运行 RED integration tests**

```bash
cd backend
uv run pytest tests/test_m8_release_gate_postgres.py tests/test_m8_review_report_cli.py tests/test_m8_release_runner.py -q
cd ..
make print-project-saas-postgres-tests
```

Expected: FAIL because final list/targets/workflow/report CLI do not exist。

- [ ] **Step 4: 固定 root M1–M8 list and full command manifest**

Final stage commands, in order:

```text
contracts.schemas
contracts.matrix
contracts.docs
contracts.git_diff
postgres.m1_m8
backend.full
backend.blocking_io
backend.format
backend.lint
frontend.install_frozen
frontend.unit
frontend.check
frontend.e2e_deterministic
frontend.build_production
frontend.build_static
security.python_dependencies
security.frontend_dependencies
security.tracked_tree
security.review_diff
security.git_history
host.setup_db
host.check_db
host.doctor
host.make_help
host.support_bundle
host.make_start
chromium.host_journey
deepseek.live_journey
recovery.full_switch
cleanup.evidence_log_security
cleanup.residual_audit
```

The final entry always executes every item fresh. Diagnostic `--stage` expands prerequisites but is marked `diagnostic_only=true` and cannot be sealed as candidate/final。

- [ ] **Step 5: 实现 deterministic CI workflow**

Use PostgreSQL 17 service, Python 3.12, locked `uv sync --group dev`, Node/pnpm versions from repository policy, `pnpm install --frozen-lockfile`, Playwright Chromium only and `fetch-depth: 0`. Run:

```bash
make test-project-saas-postgres
cd backend
uv run pytest -q
uv run pytest tests/blocking_io -q
uvx ruff format --check .
uvx ruff check .
uv run python -m scripts.release_acceptance.security --scope dependencies-backend
cd ../frontend
pnpm test
pnpm check
pnpm exec playwright test tests/e2e/m8-isolation-matrix.spec.ts
BUILD_MODE=production pnpm build
BUILD_MODE=static pnpm build
pnpm audit --prod --audit-level low
```

Then run tracked tree/review diff/Git history scanner with the PR merge base. CI records bounded job results only and cannot emit `candidate_ready`/`final_pass`。

- [ ] **Step 6: 写 operator runbook and scope warnings**

Document exact shell-safe sequence without key values:

```bash
make doctor
make check-db
M8_LIVE_ACCEPTANCE=1 make release-acceptance

cd backend
uv run python scripts/create_m8_review_report.py \
  --candidate-manifest "$M8_CANDIDATE_MANIFEST" \
  --review-base 3f574b89 \
  --review-range 3f574b89..HEAD \
  --critical 0 --important 0 --minor 0 \
  --output "$M8_REVIEW_REPORT_PATH"
cd ..
M8_LIVE_ACCEPTANCE=1 M8_REVIEW_REPORT="$M8_REVIEW_REPORT_PATH" make release-acceptance
```

The runbook defines `M8_CANDIDATE_MANIFEST` and `M8_REVIEW_REPORT_PATH` as operator-local paths inside `.release-evidence`; it explains resolving them from the candidate command's printed non-sensitive evidence-relative locator. It must not show an example API key、database password or business database URL。

- [ ] **Step 7: 运行 full deterministic M1–M8 gate**

```bash
POSTGRES_TEST_URL="$POSTGRES_TEST_URL" make test-project-saas-postgres
cd backend
uv run pytest -q
uv run pytest tests/blocking_io -q
uvx ruff format --check .
uvx ruff check .
cd ../frontend
pnpm test
pnpm check
pnpm exec playwright test tests/e2e/m8-isolation-matrix.spec.ts
BUILD_MODE=production pnpm build
BUILD_MODE=static pnpm build
cd ..
git diff --check
```

Expected: all PASS; M1–M8 plugin reports 0 skipped; builds complete; no live model or traffic switch runs in this deterministic command set。

- [ ] **Step 8: 提交并审查 Task 9**

```bash
git add Makefile .github backend frontend docs/operations RELEASING.md AGENTS.md backend/AGENTS.md frontend/AGENTS.md
git commit -m "test: establish M8 release acceptance gate"
```

Review `HEAD^..HEAD` for single-list authority, CI/local responsibility, absence of secrets/live stages in CI, exact review binding and operator destructive-action safety; repair all findings。

### Task 10: 运行 fresh candidate/review/final、提交 closure docs 并重新认证 exact closure commit

**Files:**

- Modify: `AGENTS.md`
- Modify: `backend/AGENTS.md`
- Modify: `frontend/AGENTS.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `RELEASING.md`
- Modify: `docs/operations/m8-host-release-acceptance.md`
- Modify: `docs/superpowers/specs/2026-07-12-project-first-saas-design.md`
- Modify: `docs/superpowers/specs/2026-07-20-project-release-acceptance-m8-design.md`
- Modify: `docs/superpowers/plans/2026-07-20-project-release-acceptance-m8.md`

**Acceptance:**

- Full review range is always `3f574b89..HEAD`, covering the approved M8 design correction, plan, implementation, gates and closure docs。
- First certification cycle on the Task 9 implementation commit must produce candidate → 0/0/0 report → final pass before status docs may say M8 completed。
- Closure docs then update M1–M8 to `8/8 (100%)` and cite the pre-closure certified implementation commit、manifest digest、commands/counts and 0/0/0 verdict that authorized the status change。
- The closure docs commit changes Git identity, so it invalidates the earlier report/evidence for HEAD. A second fresh candidate → independent full-range 0/0/0 review → fresh final pass is mandatory on the exact closure commit。
- The final post-closure manifest is the authority for exact closure commit/evidence digest; it remains gitignored because a Git commit cannot contain its own commit hash without changing that hash. No repository file changes after the final post-closure run。
- Final wording is exact: host PostgreSQL setup/start + desktop Chromium is certified; Docker Compose、Kubernetes/Helm、Firefox、Safari/WebKit and other providers are not M8-certified; no tag/push/image/chart/GitHub Release was created。

- [ ] **Step 1: Verify clean implementation candidate preconditions**

```bash
git status --short
git branch --show-current
git rev-parse HEAD
git diff --check
make doctor
```

Expected: clean `codex/m8-release-acceptance`, non-detached exact commit, current config accepted, required tools/ports/DB/model/key presence pass. Fix local config/environment outside Git if preflight reports only an operator prerequisite; never commit secrets。

- [ ] **Step 2: Run first fresh candidate acceptance**

```bash
M8_LIVE_ACCEPTANCE=1 make release-acceptance
```

Expected: every fixed stage passes fresh; status `candidate_ready`, review status `awaiting_review`, M1–M8 PostgreSQL skip count 0, matrix uncovered 0, security effective findings 0, DeepSeek/tool/stream pass, recovery/back-switch pass, cleanup residual 0. Record the evidence-relative candidate manifest locator and digest without copying its contents into Git。

- [ ] **Step 3: Request full independent review and repair loop**

Use `superpowers:requesting-code-review` for `3f574b89..HEAD`, requiring explicit review of:

- design/plan coverage and deployment/browser/provider exclusions;
- isolation matrix completeness and actual selector execution;
- account/project/owner predicates, server-issued capabilities and system-admin redaction;
- Gateway/Worker/Scheduler and lease/stream process boundaries;
- capacity atomicity, duplicate delivery and race behavior;
- dependency/secret scanner coverage and allowlist precision;
- evidence closed schema, commit/report binding and raw-error redaction;
- PID/database/path ownership, failure/cancel cleanup and no broad stop/drop;
- real DeepSeek tool/stream proof and complete recovery switch/back-switch。

For any finding count >0, use `superpowers:receiving-code-review`: add a failing focused test, minimally fix, run affected gates, commit, rerun the entire candidate command from fresh state, and request a new full-range review. Old candidate/report becomes invalid。

- [ ] **Step 4: Create exact 0/0/0 report and run first fresh final**

```bash
cd backend
uv run python scripts/create_m8_review_report.py \
  --candidate-manifest "$M8_CANDIDATE_MANIFEST" \
  --review-base 3f574b89 \
  --review-range 3f574b89..HEAD \
  --critical 0 --important 0 --minor 0 \
  --output "$M8_REVIEW_REPORT_PATH"
cd ..
M8_LIVE_ACCEPTANCE=1 M8_REVIEW_REPORT="$M8_REVIEW_REPORT_PATH" make release-acceptance
```

`M8_CANDIDATE_MANIFEST` and `M8_REVIEW_REPORT_PATH` are operator-local shell variables pointing inside `.release-evidence`; values contain no secret and are never committed. Expected: second fresh run status `final_pass`, exact commit/report/evidence binding succeeds and all cleanup residuals are 0。

- [ ] **Step 5: Update closure status and human-readable summary**

Update active docs to 8/8 only now. Record pre-closure certified implementation commit, stage manifest digest, gate counts, RTO/RPO facts and review verdict; do not include local path、UUID、business identifier、prompt/model output or secret. State no version tag/artifact publication occurred。

```bash
rg -n "7/8|87\.5%|M8.*pending|not yet.*releas" AGENTS.md backend/AGENTS.md frontend/AGENTS.md README.md CHANGELOG.md RELEASING.md docs/operations docs/superpowers/specs
rg -n "8/8|100%|宿主机|Chromium|Docker Compose|Kubernetes|Helm|Firefox|Safari" AGENTS.md README.md CHANGELOG.md RELEASING.md docs/operations/m8-host-release-acceptance.md docs/superpowers/specs/2026-07-12-project-first-saas-design.md
```

Expected: first command returns only intentional historical/context statements; second shows exact certified and non-certified scope in active docs。

- [ ] **Step 6: Commit closure docs**

```bash
git add AGENTS.md backend/AGENTS.md frontend/AGENTS.md README.md CHANGELOG.md RELEASING.md docs
git commit -m "docs: close M8 host release acceptance"
git status --short
```

Expected: clean tree at a new closure commit. Earlier final evidence is now historical authorization for the docs change, not proof for the new HEAD。

- [ ] **Step 7: Re-run fresh candidate on exact closure commit and review full range again**

```bash
M8_LIVE_ACCEPTANCE=1 make release-acceptance
```

Expected: new `candidate_ready` bound to the closure commit. Request independent review of `3f574b89..HEAD` again, including the closure summary and scope wording. Repair any finding by reverting status to pending while fixing, then repeat Steps 2–7; do not carry forward an old report。

- [ ] **Step 8: Run authoritative post-closure fresh final**

```bash
cd backend
uv run python scripts/create_m8_review_report.py \
  --candidate-manifest "$M8_CLOSURE_CANDIDATE_MANIFEST" \
  --review-base 3f574b89 \
  --review-range 3f574b89..HEAD \
  --critical 0 --important 0 --minor 0 \
  --output "$M8_CLOSURE_REVIEW_REPORT_PATH"
cd ..
M8_LIVE_ACCEPTANCE=1 M8_REVIEW_REPORT="$M8_CLOSURE_REVIEW_REPORT_PATH" make release-acceptance
git status --short
git rev-parse HEAD
```

Expected: authoritative status `final_pass`, exact closure commit and candidate/report/manifest digests match, all fixed stages fresh, 0 skip/finding/uncovered/residual, and working tree remains clean. Do not edit any tracked file after this command。

- [ ] **Step 9: Final handoff and branch integration choice**

Use `superpowers:verification-before-completion` to read the final manifest through the closed model and report only bounded counts/digests/scope. Then use `superpowers:finishing-a-development-branch`; do not tag、push、publish images/charts or create a GitHub Release unless the user separately authorizes it。

---

## Plan Self-review Checklist

- [x] Every frozen M8 design responsibility maps to one of Tasks 1–10 and at least one RED/GREEN gate。
- [x] The certified deployment/browser/provider scope is exact; Docker Compose、Kubernetes/Helm、Firefox、Safari/WebKit and other providers remain explicitly unverified。
- [x] The logical model name/provider model ID distinction is explicit and the key value never appears in source、commands、evidence or documentation。
- [x] Matrix authority covers actors、accounts、projects、owners、membership/lifecycle/platform roles、resource families and operations, with executable no-skip selectors and drift detection。
- [x] Capacity tests use imported default limits, stream 100 MiB without a 100 MiB allocation, simulate 5 GiB through authoritative counters and set no performance threshold。
- [x] Security gates cover threat/control mapping、locked production dependency graphs、tracked tree、review diff、reachable Git history、evidence and support bundle with exact digest allowlists only。
- [x] Evidence models are closed at every nesting level, atomic, commit/manifest bound and unable to serialize raw stdout/stderr、private content、business IDs、URLs or secret material。
- [x] Preflight is side-effect-free before validating live switch、Git/config/model/key/toolchain/ports/database authority; failure codes never echo values or raw exceptions。
- [x] Host startup uses the exact root `make setup-db` / `make start` path; cleanup uses invocation PID/PGID/start identity and never broad `make stop` or unverified database drop。
- [x] DeepSeek proof requires real Worker execution、multiple durable frames、at least one `llm.tool.result`、one terminal、refresh/Gateway restart replay and cross-scope denial。
- [x] Recovery proof performs archive、post-backup journal-first purge、distinct new restore DB、browser traffic switch、RPO absence、back-switch and exact owned cleanup。
- [x] CI runs only deterministic no-secret/no-paid/no-traffic-switch stages, while local `make release-acceptance` remains the sole complete closure entry。
- [x] M1–M7 22-file list remains an immutable prefix; M1–M8 final list has one Makefile authority and zero-skip enforcement。
- [x] Candidate and final evidence cannot be assembled from partial retries; diagnostic stage runs cannot emit a release status。
- [x] Closure documentation commit invalidation is handled by a second complete candidate/review/final cycle on exact closure HEAD, with no tracked edits afterward。
- [x] Final wording updates 8/8 only after a prior full final pass, preserves exact host certification limits and creates no tag/push/release artifact。
