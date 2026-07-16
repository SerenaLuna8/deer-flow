# M5 Task 18 implementation report

## Consistency RED

Before documentation edits, the required scan exited 1 as expected and identified the
stale current-status statement at
`docs/superpowers/specs/2026-07-12-project-first-saas-design.md:16`:
`M5 至 M8 尚未完成`.

## Documentation mapping

- `README.md` / `README_zh.md`: project Automation entry, Viewer read-only behavior,
  manual trigger, single-Gateway `scheduler.enabled`, occurrence-before-admission,
  no replay after admission, and M6 boundary.
- root/backend/frontend `AGENTS.md`: project+owner authority, Scheduler ownership,
  migration command, cache cancel-before-clear, Viewer constraints, and release-candidate
  status.
- overall/M4/M5 specs: release-candidate status and corrected M6 ownership of generic
  jobs, independent Workers, durable SSE, quotas, audit, and general backup/restore.
- `docs/operations/m5-automation-migration.md`: maintenance window, writer stop,
  authenticated external backup proof, exact dry-run/execute/check commands, probes,
  recovery, rollback boundary, and redacted logging rules.
- `CHANGELOG.md`: unreleased M5 release-candidate summary without a completion claim.

## Fresh verification

All PostgreSQL verification used only
`postgresql+asyncpg://postgres@127.0.0.1:55435/postgres`. The retained local
cluster was created at `/tmp/deerflow_m5_pg`; every integration fixture created
and dropped only generated `deerflow_test_*` databases. No business database was
read or written.

### Backend

```text
cd backend && uv run pytest tests/ -q
7662 passed, 880 skipped, 10 warnings in 106.19s

cd backend && make test-blocking-io
41 passed in 11.30s

cd backend && make lint
All checks passed!
1058 files already formatted

cd backend && uvx ruff format --check .
1058 files already formatted
```

The full-suite skips are declared environment/optional-integration skips; the fixed
PostgreSQL release gate below had zero skips. Warnings were existing dependency
deprecations, short test JWT-key warnings, and one intentional unknown-model-kwarg
warning.

### Real PostgreSQL and migration smokes

The first isolated-cluster attempt could not create test databases because `initdb`
had created its default superuser under the host account name. After adding the
expected `postgres` superuser to only that isolated cluster, the required fresh rerun
passed:

```text
cd backend && POSTGRES_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:55435/postgres \
  uv run pytest <fixed eight M1-M5 integration files> -q
32 passed in 9.54s; 0 skipped

# Task 16 fresh-install plus legacy dry-run/execute/idempotency smokes
2 passed in 0.93s; 0 skipped
```

The fixed file list was exactly: M1 cutover, project isolation, M2 governance,
M3 shared assets, M4 private work, M4 private-work migration, M5 project Automation,
and M5 Automation migration.

### Operations

A dedicated `deerflow_test_task18_repair2_ops` database was created in the same isolated
cluster, initialized to `0013_project_automation_finalize`, inspected, and dropped.
A temporary non-secret minimal `config.yaml` was used only for doctor and removed
immediately afterward.

```text
make doctor
Status: Ready (6 warning(s))

make check-db
PostgreSQL 状态: 健康
current/head: 0013_project_automation_finalize
Automation: ready
```

The six doctor warnings were the intentionally absent local `.env` files and four
optional web-tool configurations; there were no required-check failures.

### Frontend

```text
cd frontend && pnpm check
exit 0

cd frontend && pnpm format
All matched files use Prettier code style!

cd frontend && pnpm test
126 files; 915 passed; 0 failed; 0 skipped

cd frontend && pnpm test:e2e:all
167 normal Playwright tests passed in 55.7s
1 independent static-build Playwright test passed in 21.3s
```

The unit runner required unsandboxed local-port permission after a sandbox `EPERM`;
the fresh authorized rerun passed. Playwright emitted `NO_COLOR` notices and expected
connection-refused proxy noise for deliberately unmocked fallback requests, but both
test commands exited 0 with the counts above.

### Workspace consistency

```text
git diff --check
exit 0, no output

required M5 stale-status / old-gate / false-flag consistency scan
exit 0, no matches
```

## Self-review

- The initial Task 18 release-candidate documentation pass changed no production
  behavior, database state machine, API, migration implementation, or frontend runtime
  code. The final-review repair below changes only public reconciliation error mapping.
- Current documentation says release candidate awaiting independent Task 18 review;
  it does not claim M5 complete, 5/8, or 62.5% as current state. Those values remain
  only in the conditional completion contract.
- The runbook includes the exact required commands, maintenance writer stop,
  authenticated external backup proof, M1-M5 probes, recovery, and the forward-only
  rollback boundary.
- Examples expose no private titles/prompts, owner-map content, or full identifiers.
- M6 remains responsible for independent Workers, durable SSE, generic jobs/retries,
  quotas, audit, and general backup/restore; M7-M8 remain open.

## Whole-branch final-review repair wave

The independent whole-branch review reported zero Critical findings and two Important
findings:

1. `PrivateRunRecord.error` was copied into public Automation occurrence history by
   `_outcome_for_run`, which could expose provider output or private prompt content.
2. The root/backend contributor guides still described an older PostgreSQL release gate,
   and the backend command list omitted `migrate-automations`.

### Reconciliation privacy TDD

The regression test covers completion and restart reconciliation for all three terminal
failure states. Each case writes `provider secret sk-private
prompt=customer-confidential` only to the private run record and requires a stable public
error code plus a fixed safe message on the occurrence.

```text
# RED
6 failed; every occurrence exposed the injected private provider text

# GREEN
6 passed in 1.69s

# Complete reconciliation file
18 passed in 3.97s
```

Production reconciliation now maps `run_failed`, `run_timeout`, and `run_interrupted` to
fixed public-safe messages. It does not read `run.error` when constructing the public
occurrence outcome, while the original private run record retains its diagnostic text.

### Contributor-guide repair

- Root and backend `AGENTS.md` now list the exact eight M1-M5 PostgreSQL release-gate
  files/domains.
- The guidance distinguishes routine local skips from required release evidence with an
  isolated `POSTGRES_TEST_URL` and zero skips.
- The backend command list now includes `make migrate-automations ARGS="--dry-run ..."`.

All fresh verification counts in this report are from the post-repair tree. M5 remains a
release candidate awaiting re-review; this report does not claim M5 complete, 5/8, or
62.5%.

## Whole-branch second final-review repair wave

The second concentrated whole-branch review reported zero Critical findings and one
Important finding. Direct service calls did not enforce the designed lifecycle edges
`enabled -> paused` and `paused -> enabled`: `pause()` and `resume()` reached
`_prepare_mutation()` from duplicate or terminal source states, cancelling a queued
occurrence before rewriting the definition.

### Lifecycle transition TDD

The real PostgreSQL regression test covers eight invalid transitions: pause from
`paused`, `completed`, `failed`, or `cancelled`, and resume from `enabled`, `completed`,
`failed`, or `cancelled`. Every case snapshots the complete definition and queued
occurrence records before the call and also verifies that the service clock is never
read.

```text
# RED
8 failed in 2.27s
All eight calls committed definition writes and cancelled the queued occurrence.

# GREEN
8 passed in 2.11s

# Complete service file
46 passed in 10.45s

# Stable public error and router mapping
15 passed in 0.70s
```

The minimal production repair imports `AutomationConflict` and checks the locked task's
source status before taking a clock snapshot, validating the target, cancelling queued
work, or writing the definition. Invalid transitions now raise the stable
`AUTOMATION_CONFLICT` error and roll back with byte-for-byte-equivalent repository
records. Update and delete state handling were not changed.

### Second-wave fresh full-gate evidence

```text
backend full: 7662 passed, 880 skipped, 10 warnings in 106.19s
blocking I/O: 41 passed in 11.30s
lint/format: clean; 1058 files formatted
exact eight-file PostgreSQL gate: 32 passed in 9.54s; 0 skipped
fresh/legacy migration smokes: 2 passed in 0.93s; 0 skipped
doctor: Ready with 6 optional warnings
check-db: healthy; current/head 0013_project_automation_finalize; Automation ready
frontend check/format: clean
frontend unit: 126 files; 915 passed; 0 failed or skipped
frontend E2E: 167 dynamic passed; 1 static-build passed
```

The eight new service cases account for the full-suite skip increase from 872 to 880
when `POSTGRES_TEST_URL` is intentionally absent. The exact PostgreSQL release gate and
the focused lifecycle run both supplied the isolated URL and had zero skips.

One failed operations setup attempt was also retained as negative evidence. A first
`make migrate-db` invocation omitted an explicit `DATABASE_URL`; it inherited the local
`127.0.0.1:5432/deerflow` value and failed closed. The immediately following read-only
check showed that database still at revision `0007_project_shared_assets` with M5 tables
absent. No revision, Automation table, or cutover-marker write occurred. The valid rerun
then set `DATABASE_URL` explicitly to
`127.0.0.1:55435/deerflow_test_task18_repair2_ops`; doctor and check-db passed there, and
the temporary config and database were removed.

M5 remains a release candidate awaiting another independent whole-branch review.
