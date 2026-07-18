# M6 Task 20 Report — Documentation and Milestone Closure

Date: 2026-07-18

Baseline: `57d98be91b0a0ea293c8742f8d5aceed6821096f`

Scope: Task 20 documentation synchronization, fresh whole-branch gates, gate-discovered compatibility repairs, and the final independent M6 closure review. M7 legacy cleanup and M8 final release acceptance remain outside this task.

## Delivered documentation contract

- `docs/operations/m6-reliability-migration.md` now gives the exact pre-M6 archive/receipt, isolated restore rehearsal, authenticated proof, zero-write dry-run, maintenance stop, acknowledged execute, `check-db`, fixed M1–M6 gate, role readiness, final backup, and separate restore-drill order.
- `docs/operations/m6-backup-recovery.md` documents distinct secret authority, encrypted backup publication, the external authenticated tombstone journal, new-database-only restore, disposable drill ownership, failure decisions, and the no-downgrade/no-in-place-restore rule.
- Root, backend, frontend, English/Chinese README, CHANGELOG, overall SaaS design, and M6 design now agree that M6 is 6/8 (75%) while M7 and M8 remain open. The repository is therefore not described as a complete releasable multi-user SaaS.
- M6 commands in the design use the actual `--maintenance-acknowledged`, backup-proof, restore, drill, and reconciliation interfaces.

## Gate-discovered compatibility repairs

- Gateway lifecycle tests now provide a callable session-factory double, matching the production `AuditService` contract without weakening production validation.
- Historical M4/M5 migration tests now prove their revisions remain ancestors of the M6 `0015_project_reliability_finalize` head instead of freezing the repository at the former M5 head.
- `pnpm build` and `pnpm preview` use the repository's already-established deterministic Webpack production path. Default Next 16 Turbopack deadlocked twice: once under the system Node runtime and once under Node 24 with a clean `.next`; a process sample showed the Node event loop and all Turbopack Tokio workers waiting on condition variables. The Webpack path completed the same production build and is already the E2E web-server contract.

## TDD and debugging evidence

Initial documentation RED:

- `docs/operations/m6-backup-recovery.md` did not exist.
- The required `6/8`, `75%`, `M7`, and `M8` completion contract was absent from the M6 status documents.

Fresh full-gate RED and repairs:

- First backend full run: `3 failed, 2530 passed, 396 skipped` before early termination after the failures were stable. All three failures rejected old `object()` session-factory doubles with `TypeError: audit session factory is invalid`; the focused repair passed `3/3`.
- Second backend full run: `2 failed, 7886 passed, 1099 skipped` in `26:07`. Both failures were stale M4/M5 exact-head assertions. The combined five-test repair gate passed `5/5`.
- Frontend unit tests initially received sandbox `listen EPERM` on `::1:3000`; the unchanged command passed when allowed to bind its local ephemeral test listener.
- Default `pnpm build` stopped making progress in Turbopack. A clean-cache Node 24 reproduction excluded stale cache and the system Node version; `next build --webpack` passed, after which the exact patched `pnpm build` command passed.
- `make doctor` first proved the temporary M6 database healthy but correctly rejected the isolated worktree's missing config/model. A gitignored example config plus a doctor-only placeholder model made the full command return Ready without any external LLM request; all temporary config and database state was removed afterward.

## Fresh final gate evidence

- Backend full suite after the review repairs: `8990` collected, `7888 passed`, `1102 skipped`, `0 failed` in `1567.99s (0:26:08)`. The JUnit result recorded `errors="0"` and `failures="0"`.
- M6 real-PostgreSQL Task 20 gate after the review repairs: `24 passed, 0 skipped in 11.02s` across reliability schema, migration, and release files.
- Fixed 20-file M1–M6 real-PostgreSQL release gate after the review repairs: `160 passed, 0 skipped in 97.54s`.
- Cursor/proof review RED: `3 failed, 7 passed, 0 skipped`; focused GREEN: `10 passed, 0 skipped in 4.27s`; the three affected PostgreSQL files then passed `31 passed, 0 skipped in 13.26s`.
- Earlier gate-discovered compatibility regressions after formatting: `5 passed in 0.49s`.
- Backend Ruff: all checks passed; all `1170` Python files formatted.
- Frontend unit suite: `132` files, `947 passed`, `0 failed`, `0 skipped`, no snapshot changes.
- Frontend `pnpm check`: passed after the final package/document changes.
- Exact post-review `pnpm build`: Webpack compiled in `24.3s`, TypeScript completed, and `81/81` static pages generated; exit 0.
- Root `make doctor`: `Ready` with only the expected no-web-capture and disabled-host-bash warnings.
- Root `make check-db`: healthy PostgreSQL 14.19, current/target revision `0015_project_reliability_finalize`, Automation ready, Reliability ready.
- Documentation contract searches and `git diff --check`: passed. Both newly linked M6 runbooks exist. The only missing local Markdown target found by the broader scan is the pre-existing `docs/tui/tui-preview.svg` link present unchanged in both READMEs.
- The random `deerflow_test_task20_check_*` database, doctor-only configuration, build caches, and process sample were removed after verification.

The `1102` skips in the backend whole-suite run are its normal no-`POSTGRES_TEST_URL` local behavior; the three additional collected skips are the new PostgreSQL-backed cursor/proof cases. PostgreSQL release evidence is the separate mandatory M6 and fixed 20-file runs above, both of which enforced and reported zero skips.

## Plan reconciliation

- `docs/operations/m6-reliability-migration.md` already existed from Task 18, so Task 20 updated it and created only `m6-backup-recovery.md`.
- `README_zh.md` and `CHANGELOG.md` were synchronized in addition to the brief's file list because the M6 design release checklist requires both user-facing languages and release notes to agree.
- Backup proof precedes dry-run because the production dry-run command authenticates `--backup-proof`; maintenance begins only after proof and dry-run review. This is the executable Task 18/CLI contract even though the Task 20 interface summary listed the terms in a different shorthand order.
- The four backend test files and frontend build script were not planned documentation files, but fresh full gates exposed concrete compatibility/build failures. Their changes are minimal gate repairs, not scope expansion into M7/M8.

## Independent closure review

The first independent review reported **CHANGES REQUIRED** with `0 Critical / 3 Important / 0 Minor`:

1. Root and backend `AGENTS.md` still described the former eight-file M1–M5 release gate rather than Task 19's fixed 20-file M1–M6 gate.
2. Route and bridge SSE cursor parsing accepted unbounded decimal strings, allowing oversized inputs to escape the stable 400 contract or exceed PostgreSQL `BIGINT`.
3. Reliability migration dry-run returned before authenticating the required backup proof.

The bounded repair changed the documentation to the shared 20-file release command, introduced one canonical `0..9223372036854775807` cursor parser used by both route and bridge, and moved backup-proof authentication before the dry-run return. Tests cover a 5000-digit cursor, `BIGINT + 1`, missing/tampered/wrong-source backup proofs, stable route errors, bridge rejection, and a zero-write dry-run catalog snapshot. The affected gates, fixed M1–M6 gate, backend whole suite, and frontend final gates were rerun with the green evidence above.

The first post-repair rereview confirmed those three findings closed and independently reran the focused gate (`10 passed`) plus the fixed M1–M6 gate (`160 passed, 0 skipped`). It found one additional Important documentation mismatch: the migration runbook named only `deerflow_test_*` temporary databases even though the restore release cases also create `deerflow_restore_*`. A documentation contract reproduced the omission with exit 1; the runbook now names both random prefixes and continues to prohibit the business database.

The final independent rereview reported **APPROVED**, `0 Critical / 0 Important / 0 Minor`, and **Ready to merge: Yes**. All four findings from the two review passes are closed.
