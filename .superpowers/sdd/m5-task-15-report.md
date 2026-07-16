# M5 Task 15 Implementation Report

## Status and scope

- Status: complete
- Branch: `codex/m5-project-automation`
- Task base: `2cc34e5fc3b24b8b6091400a9011fd99d42f6970`
- Delivery commit subject: `test: gate M5 automation isolation`
- Scope: real PostgreSQL M5 scope, authorization, concurrency, constraint, revocation, run-admission, restart, ownership, and M4-compatibility gates; fixed CI release-gate expansion through M5
- Explicit exclusions: production runtime changes, frontend work, Task 16/17 behavior, M6 Worker/SSE work, and `.superpowers/sdd/progress.md`

## Delivered gate

- `backend/tests/support/m5_automation.py` creates a final-schema disposable database only from the explicit pytest `postgres_admin_url`, which is sourced from `POSTGRES_TEST_URL`, and rejects any database name outside `deerflow_test_*`.
- The seed matrix contains project A owner A, project A owner B, project A Viewer, project B owner, and a `system_admin` with no private-work membership override.
- Project API probes use the real FastAPI router, identity/context resolution, scoped repositories, sessions, cutover guard, and capability checks. They cover get, list/pagination, update, delete, run history, Thread reverse lookup, Viewer read/trigger behavior, owner isolation, project isolation, and system-admin isolation.
- Concurrency tests synchronize independent service calls with `asyncio.Barrier`. Each service opens its own real `AsyncSession`; no session, transaction, repository, advisory-lock, or PostgreSQL constraint is mocked.
- Competing scheduled reservations across two projects and competing claims admit exactly one occurrence under a global cap of one. Concurrent manual requests with the same idempotency key return one occurrence, and separate owners cannot oversell the global cap.
- Real composite foreign keys reject cross-owner and cross-project task coordinates, a cross-owner Thread pointer, and a run pointer belonging to a different Thread.
- Real membership/lifecycle services prove downgrade, removal, leave, and pending project deletion freeze definitions and cancel queued occurrences with `AUTOMATION_AUTHORIZATION_REVOKED`.
- Real dispatch/admission proves fresh and reused Threads capture the exact published Agent version. Completion is idempotent, and restart reconciliation interrupts an already-admitted run without a second launcher call.
- Current revision `0013_project_automation_finalize` keeps the M4 private-work guard ready. Two independent PostgreSQL engines prove Scheduler ownership is exclusive and can be cleanly transferred after release.
- `.github/workflows/project-foundation-postgres-tests.yml` now names the M1–M5 gate, keeps the `POSTGRES_TEST_URL` hard failure before pytest, and appends the M5 integration file to the fixed release list.

## Strict TDD evidence

The existing Automation PostgreSQL baseline passed before the new gate was added:

```text
67 passed in 14.11s
```

The first run after writing the new fixture and integration suite was RED:

```text
6 passed, 3 failed, 0 skipped
```

One failure was the intended missing M5 workflow contract. Two failures were test-author mistakes: the snapshot assertion used obsolete field names, and a self-referential source scan matched its own forbidden token. After correcting only those test defects, the focused suite remained RED for exactly the missing CI wiring:

```text
7 passed, 1 failed, 0 skipped
```

The workflow was then changed minimally. The final strengthened gate passed twice consecutively:

```text
8 passed in 2.47s
8 passed in 2.46s
```

No production implementation defect was exposed, so no production runtime file was changed.

## Verification evidence

Every PostgreSQL command used the explicit server at `127.0.0.1:55435`; required fixtures created random `deerflow_test_*` databases. The brief's support command was also rerun with the explicit URL so its PostgreSQL-marked cases executed instead of skipping.

| Gate | Result |
| --- | ---: |
| Task 15 real PostgreSQL integration, first final run | 8 passed, 0 skipped |
| Task 15 real PostgreSQL integration, second final run | 8 passed, 0 skipped |
| PostgreSQL fixture + check-script support validation | 31 passed, 0 skipped |
| Fixed M1–M4 PostgreSQL release gate | 17 passed, 0 skipped |
| Exact fixed M1–M5 CI PostgreSQL file set | 25 passed, 0 skipped |
| Full backend Ruff check | all checks passed |
| Full backend Ruff format check | 1057 files already formatted |
| `git diff --check` | passed |

## Self-review

- The test helper accepts no fallback URL and creates no SQLite substitute. The disposable database prefix is asserted by both helper and integration test.
- The concurrency evidence uses four explicit barriers: scheduled reservation, queued claim, manual replay, and global-cap admission. It does not serialize contenders in the test or inspect/issue advisory-lock SQL directly.
- No `AsyncMock`, `MagicMock`, `unittest.mock`, `monkeypatch`, or mocked session/lock primitive appears in the fixture or integration file.
- The API fixture overrides only request transport boundaries: it supplies an authenticated test identity and a real SQLAlchemy session. Project context, capability revalidation, repositories, constraints, cutover, and Automation services remain production implementations.
- The global scheduled cap spans project A and project B; composite FK probes separately exercise both cross-owner and cross-project coordinates.
- Dispatch assertions validate real parent Thread/Run rows and `run_asset_versions`, including the exact current published Agent version for both fresh and reused Thread modes.
- The restart proof counts launcher calls and checks both occurrence and admitted Run terminal state, so an accidental replay cannot pass through a status-only assertion.
- CI still fails before pytest when `POSTGRES_TEST_URL` is empty and executes the exact prior M1–M4 list plus the new M5 file.
- No production file, Task 16/17 file, or milestone/progress document was modified.

No blocking Task 15 concern remains. The final commit SHA is reported in the handoff because a commit cannot embed its own SHA.
