# M5 Task 10 Implementation Report — Explicit Legacy Automation Migration

## Status and scope

- Date: 2026-07-16
- Baseline: `b1ec884f4292e0027f3120fa8e6698816e1e8080`
- Branch: `codex/m5-project-automation`
- Commit subject: `feat: migrate legacy automations`
- Scope: Task 10 backend migration and operations wiring only
- Explicit exclusions: frontend, Task 11 documentation/release closure, and `.superpowers/sdd/progress.md`

Task 10 adds the explicit `migrate-automations` dry-run/execute workflow, safe setup/check/doctor
integration, staged receipts, failure recovery, and real PostgreSQL coverage. It does not claim that
the overall M5 milestone is complete.

## Delivered contract

- Added `make migrate-automations ARGS="--dry-run|--execute --owner-map <json> --backup-dir <path>"`
  at the root and backend Makefiles. Root help states that dry-run must precede execute.
- The strict owner map is exactly one legacy owner UUID to one active non-Viewer project plus one
  explicit executable Agent fallback for fresh-thread tasks. Reuse-thread tasks derive their Agent
  only from the exact scoped M4 Thread.
- Dry-run accepts 0011/0012/0013, validates M4 cutover and all source authority/relations, writes
  nothing, and returns only counts, bounded status aggregates, a truncated source hash, and safe
  mode/cutover flags.
- An unfinished execute accepts 0011 or 0012, requires an existing non-empty operator
  backup/restore proof file, and never presents the CLI as the database backup system.
- The staged path upgrades to 0012, records a stable migration run identity, writes and verifies
  separate `scheduled_tasks` and `scheduled_task_runs` ledgers, runs exact scope/Agent/Thread/Run
  probes, records `migration_ready`, lets 0013 fail before destructive DDL if any receipt or relation
  is invalid, and completes the cutover singleton last.
- Legacy fresh task Thread pointers are cleared. A skipped pre-admission occurrence with a synthetic
  nonexistent Thread is retained with null Thread/Run pointers; every other non-null historical
  Thread/Run pointer must resolve through the exact M4 project+owner relation.
- Completed cutover reruns are safe no-ops with accurate counts. Partial domain-ledger reruns resume
  only when source fingerprint, owner-map digest, completed ledgers, and target values still agree.
- `check-db` requires the three M5 control tables and reports only `ready`,
  `migration_required`, or `unavailable`; `doctor` and `setup-db` preserve only a redacted migration
  instruction and never echo prompt, title, map values, IDs, credentials, or database URLs.

## Strict TDD evidence

The initial RED command was:

```bash
cd backend
POSTGRES_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:55435/postgres \
  uv run pytest \
  tests/test_automation_migration.py \
  tests/test_automation_migration_cli.py \
  tests/test_setup_postgres.py \
  tests/test_check_postgres.py \
  tests/test_doctor.py -q
```

Collection failed because `scripts.migrate_automations` did not exist. After the first implementation,
the real PostgreSQL test exposed a schema contradiction: 0012 still kept
`scheduled_task_runs.thread_id` NOT NULL, so the specified skipped pre-admission normalization could
not be written. The 0012 expand migration now relaxes that column during staging; 0013 retains the
intended final nullable contract.

Subsequent RED/GREEN self-review cycles found and fixed:

1. A newly created, empty ledger table was incorrectly treated as evidence that every target domain
   had already been written.
2. A deterministic pointer/status normalization changed the raw source row during staging and could
   make a legitimate interrupted rerun look like source drift; the receipt fingerprint now hashes
   the normalized source semantics while the dry-run inventory still hashes every source field.
3. A task-domain receipt followed by interruption incorrectly compared the not-yet-written run
   domain as target tamper; compatibility checks now cover only domains with completed ledgers.
4. Changing the owner map after the migration run receipt but before the first domain ledger could
   start a second run; any existing run identity now pins source fingerprint and map digest first.
5. Legacy task-level `last_thread_id`/`last_run_id` pointers were not checked; they now require the
   same exact M4 composite authority as occurrence history before expand DDL.

Each repair has an isolated real PostgreSQL regression test.

## Verification

All PostgreSQL commands used the explicitly supplied local server on port 55435. Fixtures created
and dropped random `deerflow_test_*` databases; no business database was used.

| Gate | Result |
| --- | ---: |
| Final migration + CLI suite | 17 passed |
| Final Task 10 migration + setup/check/doctor operations suite | 127 passed |
| M5 Tasks 1–10 plus persistence/default-project bootstrap regression gate | 255 passed |
| Final no-op count end-to-end PostgreSQL regression | 1 passed |
| Full backend Ruff check | all checks passed |
| Full backend Ruff format check | 1051 files already formatted |
| Compileall for backend app/packages/scripts and Task 10 tests | passed |
| `git diff --check` | passed |

The full `pytest tests/` release gate and frontend gates are intentionally left to Task 11, matching
the explicit instruction not to perform Task 11 in this change.

## Self-review conclusion

- Reports and exceptions exposed by the CLI contain no private source value or identifier.
- Dry-run performs no DDL, target, ledger, marker, directory, or proof-file write.
- Invalid Viewer/project/Agent/reuse scope, unsupported status, orphan task/run history, and missing
  backup proof all fail before expand writes.
- Ledger receipts cover both domains, target digests, source/target row counts, and stable run/map
  identity. The marker is never completed before 0013's final probes.
- Completed, pre-ledger, one-domain, two-domain, target-tampered, source-changed, and empty-install
  rerun states are covered.
- `.superpowers/sdd/progress.md` is unchanged.

No blocking Task 10 concern remains. Operational prose and milestone-wide release claims remain
deliberately deferred to Task 11.

## Independent review repair — finalize receipts, source locks, and 0013 resume

The post-implementation review found four Important fail-closed gaps. This separate repair keeps
the work inside Task 10 and supersedes the earlier report's per-domain transaction and pre-ledger
run-identity details:

1. A crash after revision 0013 committed but before the last cutover-marker write could not resume,
   because the ordinary path tried to rebuild a migration plan from the deliberately lossy final
   schema. Revision 0013 with an incomplete marker now has a dedicated receipt-only recovery path.
   It locks both Automation tables, rechecks M4, the exact marker and migration run, both ledgers and
   counts, actual final target digests and relations, the owner-map digest, and the final schema;
   only then may it write the final marker. Target or receipt tamper fails closed, while a completed
   second rerun is an accurate no-op.
2. Revision 0013 previously trusted the ledger target digests without recomputing actual targets.
   The migration runner and revision now share one dependency-light canonical projection/digest
   contract. The revision recomputes both target digests before destructive DDL and also verifies the
   full normalized 0012 source fingerprint, so a change to a legal legacy-only field is not silently
   discarded.
3. Staging previously committed separate domain transactions without source-table locks. Execute now
   takes `SHARE ROW EXCLUSIVE` locks on both source tables and, in the same protected transaction,
   re-inventories, replans, verifies the stable fingerprint, writes or validates both domain receipts,
   probes relations, completes the run, and records `migration_ready`. Revision 0013 takes the same
   lock before its final receipt probes and DDL. A real second-connection test proves a legacy update
   blocks during staging and, if it commits between staging and finalize, is detected without stale
   overwrite or column removal.
4. Source schemas are now exact revision-specific allowlists for 0011, 0012, and final 0013 rather
   than required-column subsets. The 0012 raw digest projects every allowed legacy and expanded
   column. Any unknown column fails dry-run and execute before control-table or target writes.

Strict review-repair RED was recorded with eight failures: target tamper reached DDL, the post-0013
crash rerun tried the legacy fingerprint path, post-0013 target tamper was misclassified, a concurrent
legacy update was not blocked, and all four 0011/0012 task/run unknown-column cases were accepted.
The same eight fault-injection cases now pass.

### Repair verification

| Gate | Result |
| --- | ---: |
| Eight review-driven fault-injection cases | 8 passed |
| Complete migration and M5 schema suites | 31 passed |
| Task 10 migration/CLI/setup/check-db/doctor suite | 135 passed |
| Expanded Tasks 1–10 Automation + bootstrap + check-db + real M4 migration gate | 406 passed |
| Targeted Ruff check/format and compileall during repair | passed |
| `git diff --check` during repair | passed |

The atomic transaction intentionally changes one old injected-failure expectation: a failure before
the second domain now rolls back the new run and first ledger together. Partial-domain compatibility
is still covered by constructing a valid previously committed 0012 task receipt and proving the new
runner resumes it safely. A failure before the first ledger leaves no durable run identity, so a later
reviewed owner map may proceed; once any receipt exists, the map and stable source digest remain
pinned. Task 11 work remains excluded.

## Second independent review repair — eliminate finalize lock upgrade deadlock

The second review found one remaining Important concurrency gap. Revision 0013 first acquired
`SHARE ROW EXCLUSIVE`, which is compatible with the `ROW SHARE` table lock taken by the scheduler's
`SELECT ... FOR UPDATE`. The scheduler could therefore lock a row and then wait for its `UPDATE`'s
`ROW EXCLUSIVE`, while the migration waited to upgrade to `ACCESS EXCLUSIVE` for destructive DDL.
That wait cycle can be rejected by PostgreSQL as `40P01` instead of producing the required
deterministic serialization.

Strict TDD added three real PostgreSQL barrier tests before production changes. RED showed all three
missing invariants: a scheduler `SELECT FOR UPDATE` crossed the migration's initial lock, a
writer-first sequence observed the migration holding granted `ShareRowExclusiveLock` while waiting
for `AccessExclusiveLock`, and no shared fixed-order final lock helper existed. The minimal repair is
one shared statement:

```sql
LOCK TABLE scheduled_tasks, scheduled_task_runs IN ACCESS EXCLUSIVE MODE
```

Revision 0013 executes that statement before every probe, digest, or DDL operation and holds it to
transaction end. The receipt-only 0013 recovery path uses the same statement before actual-target
verification and the final marker write, preventing a pre-cutover writer from changing verified rows
after the marker commits. Ordinary 0012 staging intentionally retains `SHARE ROW EXCLUSIVE`, because
it performs no lock upgrade and its existing lock/transaction boundary already blocks legacy writes.

GREEN proves both scheduler orderings. When migration starts first, the writer blocks at the initial
`SELECT FOR UPDATE`, then completes after 0013 commits. When the writer starts first, migration waits
without holding the weaker lock; after the writer commits, the migration detects target digest drift
and stays at 0012 without destructive DDL. Two concurrent final lockers using the shared two-table
order also serialize without a cross-table deadlock.

### Second repair verification

| Gate | Result |
| --- | ---: |
| Real PostgreSQL finalize/writer order and two-table lock tests | 3 passed |
| Complete migration and M5 schema suites | 34 passed |
| Task 10 migration/CLI/setup/check-db/doctor suite | 138 passed |
| Expanded Tasks 1–10 Automation + bootstrap + check-db + real M4 migration gate | 409 passed |
| Full backend Ruff check and format check | passed; 1052 files formatted |
| Compileall and `git diff --check` | passed |

This repair changes only finalization/recovery lock acquisition, its real concurrency coverage, and
this report. Task 11 remains excluded.

## Final review repair — unify normal execute with the final marker barrier

The final review found one remaining Important consistency gap. The receipt-only 0013 recovery path
already acquired the shared fixed-order two-table `ACCESS EXCLUSIVE` barrier and revalidated every
final receipt before writing the marker, but the ordinary non-empty execute path did not reuse it.
After `command.upgrade(..., "head")` committed and released 0013's DDL locks, that path opened a new
transaction, checked only the revision, and called `_mark_cutover_complete()` directly. A writer that
had been waiting behind 0013 could therefore change a verified target row before the marker write;
ordinary execute would report a completed cutover while a receipt-only recovery of the same state
would reject the digest drift.

The minimal repair removes the direct normal-path marker transaction. After recreating the async
engine following Alembic's synchronous upgrade, ordinary execute now calls
`_resume_final_cutover(..., complete_marker=True)`, exactly like final-schema recovery. One final
transaction therefore:

1. acquires `scheduled_tasks, scheduled_task_runs` in the shared fixed `ACCESS EXCLUSIVE` order;
2. rechecks revision 0013 and the M4 cutover marker;
3. rechecks the exact migration-ready marker, completed run, owner-map digest, both ledgers, source
   and target counts, actual target digests, scope relations, and the final schema; and
4. writes `cutover_complete` last while the same barrier is still held.

Any writer drift now fails closed and leaves the marker incomplete. The repair reuses the existing
receipt-only implementation and keeps the existing dispose/recreate engine lifecycle; it does not
add a second validation contract or another engine.

### Strict TDD evidence

The new real PostgreSQL regression starts from revision 0011 and invokes only the public
`run_automation_migration()` entry point. It pauses after committed staging, holds
`automation_cutover_state` so revision 0013 retains its granted source-table locks, observes the
scheduler writer's `RowShareLock` waiting behind 0013, then releases 0013. The writer commits its
target change before the public runner enters the final marker transaction.

RED against the previous ordinary path was the expected behavioral failure:

```text
FAILED test_public_execute_revalidates_writer_drift_after_finalize_commits
Failed: DID NOT RAISE AutomationMigrationError
```

After the one-path repair, the same test passes. The runner raises the redacted target-digest
conflict, the writer's legal `status='paused'` change remains committed, revision 0013 remains
installed, and the singleton remains `migration_ready` with
`final_schema_probe_complete=true` and `cutover_at=NULL`.

### Final repair verification

All PostgreSQL suites used the explicit isolated server on port 55435 and random
`deerflow_test_*` databases.

| Gate | Result |
| --- | ---: |
| Public finalize-first writer-drift regression | 1 passed |
| Normal execute, crash recovery, target tamper, both writer orders, fixed lock order | 7 passed |
| Complete Task 10 migration and M5 schema suites | 35 passed |
| Task 10 migration/CLI/setup/check-db/doctor suite | 139 passed |
| Comprehensive Tasks 1–10 Automation, bootstrap/check-db, and real M4 migration gate | 411 passed |
| Full backend Ruff check | all checks passed |
| Full backend Ruff format check | 1052 files already formatted |
| Compileall for backend app/packages/scripts and Task 10 tests | passed |
| Code diff whitespace check | passed |

Self-review confirmed that the success, drift, recovery, and empty-install branches retain their
existing report semantics; no private values or database URLs were added to errors or reports; the
fixed lock order is unchanged; and no `40P01` occurred in either scheduler ordering. This repair is
limited to Task 10 migration finalization, its regression test, and this appended report. Task 11 and
`.superpowers/sdd/progress.md` remain untouched.

## Frozen review repair — exact 0012 schema and complete final constraints

The frozen Task 10 review found two remaining Important fail-before-DDL gaps.

First, revision 0013 held the correct fixed-order `ACCESS EXCLUSIVE` barrier but immediately entered
fixed target/source projections. Unlike the public runner's preflight, the revision did not compare
the actual table schemas to the exact 0012 expanded allowlists. An unknown column added after staging
could therefore survive finalize even though the equivalent preflight state was rejected.

Revision 0013 now performs this immutable order inside its Alembic transaction:

1. acquire the shared `scheduled_tasks, scheduled_task_runs` `ACCESS EXCLUSIVE` barrier;
2. inspect both actual column sets and require exact equality with
   `AUTOMATION_EXPANDED_COLUMNS`;
3. only then execute the first fixed target projection and final-constraint validation;
4. verify marker/run/ledger/count/digest/source/relation receipts; and
5. perform destructive DDL and record the final schema probe.

A structural unit test pins that ordering before any Alembic mutation. Real PostgreSQL tests add a
non-empty `private_shadow` column after committed staging to each source table independently. Both
finalizations now fail with the redacted unsupported-schema error while revision remains 0012, the
unknown and legacy columns remain intact, and the singleton remains `migration_ready` with no final
probe or cutover timestamp.

Second, script preflight previously validated only selected legacy enums. In particular, a negative
`run_count` passed dry-run; execute could commit staging and freeze the receipt before final CHECK
installation rejected the row. Revision 0013 likewise had no independent actual-target constraint
probe, so a migration-ready target tamper was classified first as digest drift instead of the more
fundamental invalid-final-row state.

The dependency-light migration digest module now owns one shared
`final_target_rows_satisfy_constraints()` validator used by both the public runner's planned rows and
revision 0013's actual rows. It mirrors every final CHECK plus each data uniqueness rule:

- task context mode, schedule type, status, overlap policy, context/thread pairing, Agent scope,
  positive version, non-negative run count, and bounded last outcome;
- occurrence trigger, status, Run-to-Thread requirement, non-negative launch attempts, positive
  optional membership version, and positive task version; and
- project/owner task scope uniqueness, occurrence-key uniqueness, and the partial non-null manual
  idempotency uniqueness rule.

The existing pre-DDL scope/relation probes continue to cover the final NOT NULL, project, owner,
membership, Agent, Thread, Run, and Agent-project trigger authorities. Thus the shared validator and
the existing relation probes together cover every final data constraint before installation.

### Frozen-review TDD evidence

Before production changes, the five new real PostgreSQL cases all failed:

```text
FFFFF
2 unknown-column cases: Failed: DID NOT RAISE RuntimeError
negative run_count dry-run: Failed: DID NOT RAISE AutomationMigrationError
negative run_count execute: automation migration failed safely after staging/finalize
migration-ready tamper: target digest probe failed instead of target constraints
```

The shared validator test was also written before implementation and failed collection with the
expected missing-import error. GREEN now proves all final conditions and uniqueness rules through 19
pure validator cases, while the five real PostgreSQL cases prove both lifecycle boundaries.

### Frozen-review verification

All PostgreSQL suites used the explicit isolated server on port 55435 and random
`deerflow_test_*` databases.

| Gate | Result |
| --- | ---: |
| Shared final CHECK/uniqueness validator matrix | 19 passed |
| Unknown-column, negative preflight, and post-stage constraint-tamper PostgreSQL cases | 5 passed |
| Complete Task 10 migration and M5 schema suites | 59 passed |
| Task 10 migration/CLI/setup/check-db/doctor suite | 163 passed |
| Comprehensive Tasks 1–10 Automation, bootstrap/check-db, and real M4 migration gate | 435 passed |
| Full backend Ruff check | all checks passed |
| Full backend Ruff format check | 1052 files already formatted |
| Compileall for backend app/packages/scripts and affected Task 10 tests | passed |
| Code diff whitespace check | passed |

Self-review confirmed that exact column validation is the first operation after the source lock and
occurs before `_assert_final_target_constraints()` can issue a fixed projection. Dry-run and execute
share the same zero-write plan validator before expand; 0013 independently revalidates actual rows
under the final barrier; recovery after 0013 remains protected by installed database constraints.
Errors and reports remain redacted. This repair changes only Task 10 migration validation, its tests,
and this appended report; Task 11 and `.superpowers/sdd/progress.md` remain untouched.
