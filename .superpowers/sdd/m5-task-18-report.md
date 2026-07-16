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

## Whole-branch third final-review repair wave

The third concentrated review reported zero Critical findings, four Important
findings, and two Minor findings. This wave is still in progress and does not mark M5
complete.

### Sequential M4 to M5 migration TDD

A real PostgreSQL regression now starts from one revision-0007 database containing
both legacy M4 private-work rows and legacy Automation rows. M4 must preserve every
Automation row and stop at revision 0011; the subsequent M5 migration owns the
remaining upgrade to revision 0013 and final Automation cutover marker.

```text
# RED
1 failed in 0.98s: M4 unconditionally upgraded to head and failed safely

# GREEN
1 passed in 0.93s
```

The M4 migrator now checks both legacy Automation tables after committing its own
marker. It returns at the M4 final revision when either contains data, while retaining
the previous head-upgrade behavior for an empty Automation domain.

### Overlap settlement TDD

Concurrent scheduled reservation, scheduled reuse-thread dispatch, and manual
reuse-thread dispatch now all prove occurrence and parent-definition settlement. Cron
parents remain enabled and advance; once parents become terminal; every winning
terminal CAS increments the parent exactly once.

```text
# RED
5 failed in 1.70s: parents were not settled and reuse overlap used the wrong outcome

# focused GREEN
5 passed in 1.56s

# complete occurrence, dispatcher, and reconciliation files
60 passed in 12.94s
```

The shared settlement helper records the parent only after a successful occurrence
terminal transition. Scheduled overlap uses `skipped` with
`AUTOMATION_OVERLAP_SKIPPED`; manual overlap remains `rejected` with
`AUTOMATION_CONFLICT`.

### Once-delay and task-ID TDD

The service-level regressions cover the configured one-time schedule boundary, zero
writes on an invalid schedule-changing update, a title-only update within the minimum
window, strict constructor input, Gateway config injection, and the public task-ID
shape.

```text
# RED
8 failed in 1.93s

# focused GREEN
8 passed in 1.84s

# complete service and app-wiring files
56 passed in 12.19s
```

`ProjectAutomationService` now receives the effective scheduler
`min_once_delay_seconds`, rejects non-integer or negative constructor values, enforces
the lower bound for create and schedule-changing update, and validates an update before
cancelling queued work. Non-schedule edits preserve the existing occurrence time.
New IDs use the strict `task-<32 lowercase UUID hex>` form.

### Legacy cutover UI TDD

The legacy workspace page regression returns the real structured
`409 AUTOMATION_CUTOVER` envelope and runs in both `en-US` and `zh-CN`. It requires
localized repository-owned copy, no raw server message, no legacy create/filter/workbench
controls, and no non-GET scheduled-task request.

```text
# API parser RED
1 failed; 915 passed: the structured error class was absent

# API parser GREEN
5 passed in 0.10s

# browser RED
1 failed; 1 did not run: no migration-complete state existed

# browser GREEN
2 passed in 40.2s
```

The shared Gateway error parser now preserves HTTP status and structured code while
remaining compatible with string-detail responses. The legacy page recognizes only
`409 + AUTOMATION_CUTOVER`, hides all legacy mutation surfaces, and renders stable English
or Chinese migration-complete guidance without displaying server text.

### Third-wave fresh full-gate evidence

Every PostgreSQL command in this wave used the retained isolated cluster at
`127.0.0.1:55435`. Integration fixtures created only generated `deerflow_test_*`
databases. The operations gate used and then dropped
`deerflow_test_task18_repair3_ops`; its temporary non-secret `config.yaml` was also
removed.

The first full backend run exposed two stale `SimpleNamespace` scheduler fixtures in
`test_gateway_run_recovery.py`; both omitted the newly wired configuration field. No
production fallback was added because real `AppConfig.scheduler` is strict. Adding the
field to those two fixtures produced `5 passed, 1 skipped` for the affected file, then
the required complete rerun passed.

```text
# first complete backend run
2 failed, 7663 passed, 889 skipped, 10 warnings in 136.75s

# post-fixture complete backend rerun
7665 passed, 889 skipped, 10 warnings in 105.23s

# blocking-I/O
41 passed in 11.26s

# backend lint / format
All checks passed; 1059 files formatted

# exact eight-file M1-M5 PostgreSQL gate
33 passed in 10.07s; 0 skipped

# fresh-install and legacy migration smokes
2 passed in 1.32s; 0 skipped

# operations
doctor: Ready with 6 optional warnings
check-db: healthy; current/head 0013_project_automation_finalize; Automation ready

# frontend
check: passed after one import-order-only correction
format: passed
unit: 126 files; 916 passed; 0 failed or skipped
normal E2E: 169 passed
independent static-build E2E: 1 passed

# workspace
git diff --check: passed
required consistency scan: passed with no matches
```

The third review's four Important findings are covered by the sequential M4→M5
migration, terminal occurrence/parent settlement, service-level once-delay enforcement,
and legacy cutover UI sections above. The two Minor findings are covered by the strict
new task-ID shape and stable repository-owned English/Chinese cutover presentation.
Root/backend guides and the M4 runbook now also document the `0011` Automation handoff.

M5 remains a release candidate pending an independent re-review of this repair wave. This
report does not mark M5 complete or claim 5/8 or 62.5%; M6-M8 remain open.

## Whole-branch fourth final-review repair wave

The fourth concentrated review reported zero Critical findings, two Important findings,
and two Minor findings. This wave repairs both Important contracts and the configuration-
comment Minor. The remaining broad project-Automation locale cleanup is intentionally
recorded as a non-blocking follow-up rather than mixed into the execution/admission repair.
M5 remains a release candidate and is not marked complete.

### Post-admission durable Run TDD

Real PostgreSQL dispatcher regressions now cover an exact terminal Run after real
`PrivateRunAdmissionService.admit`, an admitted `pending` Run whose launcher raises,
completion-before-backfill, deterministic adoption and mismatch behavior, no-Run
unavailable requeue, and concurrent governance cancellation. Terminal private errors are
retained only on the Run; the occurrence receives a fixed public-safe message and the
shared `AUTOMATION_RUN_*` outcome. Active admitted Runs are linked as `running` with their
durable `created_at`, do not increment the parent, and continue occupying the global cap.

```text
# RED
8 failed, 1 passed in 2.77s
All eight failures were the old rejected/pre-admission settlement behavior.

# focused GREEN
9 passed in 2.62s

# complete dispatcher + reconciliation + occurrence files
63 passed in 13.77s
```

The private Run terminal-to-public outcome mapper now lives in shared settlement code and
is consumed by both dispatcher failure recovery and reconciliation. Terminal settlement
updates the parent only after the first occurrence CAS; the completion-before-backfill
race leaves `run_count == 1`. An exact `pending`/`running` Run is backfilled instead of
requeued or rejected, so a `max_concurrent_runs=1` probe cannot reserve a second due task.
Only unavailable dispatch with no matching Run keeps the existing requeue path.

### Semantic schedule update and sparse PATCH TDD

The backend regression moves an enabled once Automation to 30 seconds before its persisted
execution, then sends a title change plus the exact normalized persisted
`schedule_spec`/`timezone`. The update must succeed, preserve `next_run_at`, and cancel an
existing queued occurrence through the normal title-update transaction. A true `+59`
second schedule change still fails before any definition or occurrence write.

The frontend builder now receives the initial `Automation` and emits only changed fields.
Its once comparison treats equivalent ISO-8601 instants such as `Z` and `+00:00` as equal;
the real form E2E fixes browser time at `:30`, edits a once Automation due at the next
minute, and proves the PATCH is exactly `{expected_version,title}`.

```text
# backend RED
1 failed in 0.74s with AutomationInvalid from the minimum-delay check

# backend GREEN and true-change guard
3 passed in 1.11s

# frontend RED
2 unit failures; near-once E2E reached a request with an unwanted schedule_spec

# frontend GREEN
917 unit tests passed; near-once E2E 1 passed; frontend check passed

# complete focused service/router and project Automation E2E
70 backend tests passed in 13.02s
12 browser tests passed in 25.2s
```

`config.example.yaml` now describes `min_once_delay_seconds` as applying to creation and
schedule-changing updates. Full project Automation i18n remains the only broad Minor from
this review and is deferred to a dedicated locale cleanup to avoid expanding this
execution-critical repair wave.

### Fourth-wave fresh full-gate evidence

All PostgreSQL commands used the isolated retained cluster at
`127.0.0.1:55435`; no command used the default `5432` port. Integration fixtures created
only generated `deerflow_test_*` databases. The operations chain created, initialized,
checked, and dropped `deerflow_test_task18_repair4_ops`, and its temporary non-secret
`config.yaml` was removed.

```text
# complete backend
7665 passed, 893 skipped, 10 warnings in 109.39s

# blocking-I/O
41 passed in 11.35s

# backend lint / format
All checks passed; 1059 files formatted

# exact eight-file M1-M5 PostgreSQL gate
33 passed in 10.53s; 0 skipped

# sequential final-schema, legacy, and fresh migration smokes
3 passed in 1.92s; 0 skipped

# operations
setup-db: created at 0013_project_automation_finalize
doctor: Ready with 6 optional environment/capability warnings
check-db: healthy; current/head 0013_project_automation_finalize; Automation ready

# frontend
check: passed
format: passed
unit: 126 files; 917 passed; 0 failed or skipped
normal E2E: 170 passed
independent static-build E2E: 1 passed
```

The first fresh frontend launch attempt was stopped before test discovery because the
execution sandbox denied both local listeners on port 3000 with `EPERM`. The same commands
were then rerun serially with local-listener permission and produced the complete passing
counts above; there was no product assertion failure. Final consistency and diff checks
passed after this report update.

The fourth review's Important findings are therefore covered by both focused RED/GREEN
evidence and the fresh complete gates. The broad locale cleanup remains one acknowledged
non-blocking Minor. M5 is still pending independent re-review; this report does not mark
the milestone complete.
