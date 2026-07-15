# M4 Task 18 implementation and verification report

Date: 2026-07-15

Repair baseline: `d0c7ee98cf80a3222af084b253d4cace50810864`

Status: **READY FOR FIXED-COMMIT INDEPENDENT REVIEW**. The bounded release-blocker repair
wave and all required executable gates are green. M4 remains a candidate until the parent starts
the single formal review from the fixed repair commit. This report does not mark Task 18 complete,
does not change overall progress to 4/8, and does not check the final acceptance checklist.

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

## Frozen legacy SQLite contract

The repair initially made SQLite source signatures match final M4 PostgreSQL rows. Technical
verification rejected that approach because the real legacy source contract is
`threads_meta/runs/run_events/feedback.user_id` without M4 project columns.

The final repair therefore:

- restores the frozen legacy source signatures and user-reference allowlist;
- validates source columns against that frozen contract rather than current ORM columns;
- uses current equivalent column types only for decoding renamed legacy `user_id` values while
  preserving legacy source keys and ledger semantics;
- keeps cross-source reference/unique discovery tolerant of columns that did not exist before M4;
- fails before a transaction or target write when private legacy rows are pointed directly at the
  final M4 schema, with the explicit message that a pre-M4 PostgreSQL target and subsequent
  `migrate-private-work` cutover are required;
- keeps the real final-schema reconciliation test on rows the final schema can directly carry
  (`users` and `scheduled_tasks`) instead of inventing an M4-shaped SQLite source.

Focused evidence: `tests/test_sqlite_to_postgres_migration.py` is `67 passed`, including the direct
final-target fail-before-write regression.

## Root diagnostics repairs

Two deterministic `make doctor` defects were fixed:

1. the root recipe launched `../scripts/doctor.py` from `backend` without `PYTHONPATH=.`, so the
   backend `scripts.check_postgres` module was unavailable;
2. `asdict(PostgresCheckResult)` omitted the computed `healthy` property, so doctor treated a
   healthy result as false even when `make check-db` passed.

Both have regression tests. A normal, gitignored `make config` setup was generated only in this
isolated worktree, supplied one test-only model with no real secret, and pointed explicitly at the
disposable database. `make doctor` exited 0 with `Status: Ready`; the only warnings were the
unconfigured optional `web_capture` tool and disabled host bash for the local sandbox. The
temporary `config.yaml`, `.env`, and `frontend/.env` were deleted afterward.

## Disposable PostgreSQL safety record

- Data directory: `/tmp/deerflow_m4_task18_repair_pg2.1EGZ09/data`
- Bind: `127.0.0.1:55420`
- Admin/maintenance database: `postgres`
- Explicit check database: `deerflow_task18_check2`
- `make setup-db` initialized the check database to `0011_private_artifact_tombstone`.
- Integration fixtures created and dropped only random `deerflow_test_*` databases.
- No business database URL or business database was used.
- The PostgreSQL server was stopped and both Task 18 temporary cluster directories were deleted.

## Final backend evidence

| Gate | Fresh result |
| --- | --- |
| M4 focused (`test_private_work_*`, `test_private_*`, scoped checkpointer) | `429 passed in 46.19s` |
| blocking-I/O | `35 passed in 11.03s` |
| frozen legacy SQLite migration | `67 passed in 1.18s` |
| full backend after the final SQLite repair | `8160 passed, 18 skipped, 12 warnings in 256.62s` |
| fixed M1-M4 six-file PostgreSQL gate | `16 passed in 4.32s`, 0 skipped |
| Ruff check | all checks passed |
| Ruff format check | `1009 files already formatted` |

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

## Next step

Create the single repair commit, then let the parent start the one formal independent review from
that immutable commit. Only a fixed-commit review with zero Critical/Important findings may move
Task 18 to completion verification and update M4 to 4/8.
