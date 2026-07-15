# M4 Task 18 implementation and verification report

Date: 2026-07-16

Formal-review repair baseline: `c5167f5239769983b9335943459df5a53d85b3a8`

Status: **COMPLETE — PARENT COMPLETION VERIFICATION PASSED**. The single formal review reported
0 Critical, 2 Important, and 1 Minor finding. One concentrated repair wave closed all three and
all required executable gates are green. Per the runnable-first rule, no second formal review was
started. Parent verification on fixed commit `7bda63660ce5d177ebef8519f12dd5effcfdd09c`
confirmed the review repairs, clean worktree, fresh gates, and documentation consistency. Task 18
and M4 are complete; overall progress is 4/8 (50%).

## Repair scope

The initial full-backend run exposed shared final-schema fixture/adaptation seams rather than 81
independent product defects. The bounded repair aligned those seams without weakening project and
owner scope:

- channel connection/router/runtime tests now use `PrivateResourceScope` and scoped repositories;
- console, gateway lifecycle, persistence bootstrap/autogen/timezone and PostgreSQL fixture tests
  create rows valid under the M4 final schema;
- legacy runtime lifecycle tests use the project-private Thread/run route and separately prove the
  legacy mutation surface stays closed after cutover;
- private skill roots use resolved temporary paths on macOS;
- local private sandbox projection no longer attempts a host `mkdir /mnt`;
- scoped checkpointer pending writes retain authorization/thread locking, validate an existing
  marker, and allow LangGraph's legitimate write-before-checkpoint ordering;
- replay fixtures use the final project-private route and the replay golden contains the current
  scoped events;
- the setup-agent real-server test uses the authenticated private asset path;
- subagent log capture patches the module logger directly instead of depending on inherited
  pytest logging handlers.

The frontend artifact failure was traced to a test-fixture state mismatch: a custom
`runStreamHandler` emitted values but did not persist them into the mock Thread state/history, so a
post-stream refresh restored old `THREAD_MESSAGES`. The helper now persists the final custom
values through the same mock upsert path as the default stream. Production artifact/state merge
code was not changed. Unmocked `127.0.0.1:8001` proxy messages remain known E2E log noise.

## Formal-review repair closure

The fixed-commit formal review of `c5167f52` found two runnable blockers and one portability bug:

1. legacy SQLite private rows had no executable route through a pre-M4 PostgreSQL schema;
2. marker-before legacy `/api/channels/*` requests reached the final project-scoped repository
   signature and returned 500;
3. the root `make doctor` recipe used a POSIX-only `PYTHONPATH=.` assignment.

The bounded repair closes them without weakening final project scope:

- `setup-m4-migration-db` creates or verifies a dedicated database whose application schema is
  exactly `0007_project_shared_assets`, while still initializing the LangGraph tables;
- `migrate-sqlite --m4-staging-target` requires that exact revision, reflects the actual 0007
  target tables, validates their frozen column/primary-key contract, and imports from verified
  backups before `migrate-private-work` advances through the final M4 marker;
- a real disposable-PostgreSQL integration test executes legacy SQLite
  `threads_meta/runs/run_events/feedback` through 0007, the private-work cutover, final revision
  `0011_private_artifact_tombstone`, and `cutover_complete`, then verifies project/owner/Agent
  authority on the resulting rows;
- the legacy channel router alone uses `LegacyChannelConnectionRepository`, an explicit raw-SQL
  adapter frozen to the 0007/0008 owner-only columns. The final `ChannelConnectionRepository`,
  project route, and inbound routing remain project-scoped and unchanged;
- the 25 removed pre-marker behavior cases were restored and now run against both 0007 and 0008;
  the separate post-marker suite still proves stable `409 PRIVATE_WORK_CUTOVER` behavior;
- `scripts/doctor.py` installs the backend import path in Python, and the root Make recipe uses the
  existing cross-platform backend runner without a shell environment assignment.

Focused RED/green evidence was preserved: the restored channel suite initially had 25 failures,
the SQLite pipeline first failed because the 0007 setup target did not exist, and the Windows
recipe assertions initially had 2 failures. The combined repaired slice is `232 passed`, 0 skipped.

## Frozen legacy SQLite contract

The repair initially made SQLite source signatures match final M4 PostgreSQL rows. Technical
verification rejected that approach because the real legacy source contract is
`threads_meta/runs/run_events/feedback.user_id` without M4 project columns.

The final source-contract repair therefore:

- restores the frozen legacy source signatures and user-reference allowlist;
- validates source columns against that frozen contract rather than current ORM columns;
- uses current equivalent column types only for decoding renamed legacy `user_id` values while
  preserving legacy source keys and ledger semantics;
- keeps cross-source reference/unique discovery tolerant of columns that did not exist before M4;
- fails before a transaction or target write when private legacy rows are pointed directly at the
  final M4 schema, and provides the explicit 0007 staging mode for the supported executable path;
- reflects and validates the real 0007 target instead of trying to insert through final
  `Base.metadata`;
- keeps the real final-schema reconciliation test on rows the final schema can directly carry
  (`users` and `scheduled_tasks`) instead of inventing an M4-shaped SQLite source.

Focused evidence: `tests/test_sqlite_to_postgres_migration.py` is `68 passed`, including the direct
final-target fail-before-write regression and the final-schema rejection of staging mode. The
end-to-end 0007-to-final pipeline is also part of the fixed six-file PostgreSQL gate.

## Root diagnostics repairs

The repair history fixed three deterministic `make doctor` defects:

1. the root recipe launched `../scripts/doctor.py` from `backend` without `PYTHONPATH=.`, so the
   backend `scripts.check_postgres` module was unavailable;
2. `asdict(PostgresCheckResult)` omitted the computed `healthy` property, so doctor treated a
   healthy result as false even when `make check-db` passed;
3. the formal review found that the repaired root recipe still depended on a POSIX-only shell
   assignment, so backend path installation now happens inside Python.

Both have regression tests. A normal, gitignored `make config` setup was generated only in this
isolated worktree, supplied one test-only model with no real secret, and pointed explicitly at the
disposable database. `make doctor` exited 0 with `Status: Ready`; the only warnings were the
unconfigured optional `web_capture` tool and disabled host bash for the local sandbox. The
temporary `config.yaml`, `.env`, and `frontend/.env` were deleted afterward.

## Disposable PostgreSQL safety record

- Data directory: `/tmp/deerflow_m4_task18_review_fix_pg.RYA0Ax/data`
- Bind: `127.0.0.1:55423`
- Admin/maintenance database: `postgres`
- Explicit check database: `deerflow_task18_review_fix_check`
- `make setup-db` initialized the check database to `0011_private_artifact_tombstone`.
- Integration fixtures created and dropped only random `deerflow_test_*` databases.
- No business database URL or business database was used.
- The PostgreSQL server was stopped and the formal-review repair cluster directory was deleted.

## Final backend evidence

| Gate | Fresh result |
| --- | --- |
| formal-review repaired slice | `232 passed`, 0 skipped |
| M4 focused (`test_private_work_*`, `test_private_*`, scoped checkpointer) | `429 passed in 45.14s` |
| blocking-I/O | `35 passed in 11.00s` |
| frozen legacy SQLite migration | `68 passed in 1.28s` |
| full backend after the formal-review repair | `8221 passed, 18 skipped, 12 warnings in 272.25s` |
| fixed M1-M4 six-file PostgreSQL gate | `17 passed in 5.25s`, 0 skipped |
| Ruff check | all checks passed |
| Ruff format check | `1013 files already formatted` |

The 18 full-suite skips are all declared environment/live conditions:

- 1 missing local live-test config;
- 1 delegation-ledger live opt-in;
- 11 client E2E tests requiring an LLM API key;
- 3 create-agent live tests requiring an LLM API key;
- 1 real-LLM deferred-tool opt-in;
- 1 Linux `/proc`-specific sandbox test.

There were no PostgreSQL-gate skips, failures, or errors.

## Final frontend evidence

| Gate | Fresh result |
| --- | --- |
| `pnpm check` | pass |
| `pnpm test` | 116 files, 836 passed, 0 skipped, 0 snapshot changes |
| artifact stream regression repeated serially | `5 passed` (`--repeat-each=5 --workers=1`) |
| full Playwright | `156 passed in 51.9s` |

The full Playwright run included the artifact regression and all M4 project-private-work E2Es.

## Root and repository evidence

- `make check-db`: healthy; current and target revision both
  `0011_private_artifact_tombstone`.
- `make doctor`: exit 0, `Status: Ready (2 warning(s))` under the isolated temporary setup.
- `git diff --check`: pass before report update and will be rerun before commit.
- Documentation stale-state search only returns historical M3 text and Task 18's own RED/search
  instructions, not current M4 completion claims.

## Deliberate runnable-first boundary

Project run join/cancel/rollback remains the Task 11 staged gap already recorded in
`.superpowers/sdd/progress.md`; Task 18 does not add a new lifecycle API. The current project Stop
path aborts the scoped in-flight stream, and server-side SSE consumer disconnect cancellation
remains covered. Adding persistent cross-request join/cancel/rollback is later lifecycle work, not
part of this bounded release-blocker repair.

## Completion verification

The parent verified the immutable repair commit, the executable SQLite staging command and real
PostgreSQL pipeline, marker-before and marker-after channel contracts, cross-platform doctor path,
fresh gate counts, clean temporary-resource teardown, and documentation status. No second review
loop was opened. Branch integration remains a user choice; no merge, push, or worktree cleanup was
performed automatically.
