# M6 Task 19 Report — Reliability Release Gates

Date: 2026-07-18

Baseline: `e4d53393485cbf0b4e65eab29566683f2c095bb7`

Scope: Task 19 only. Task 20 documentation and final milestone closeout were not started, and `.superpowers/sdd/progress.md` was not changed.

## Delivered release contract

- Root `make test` delegates to `make test-project-foundation-postgres`.
- `PROJECT_FOUNDATION_POSTGRES_TESTS` is one fixed, ordered source of truth containing the eight M1–M5 PostgreSQL files and twelve M6 migration/schema/process/Job/stream/quota/audit/recovery/release files.
- The root recipe hard-fails when `POSTGRES_TEST_URL` is empty and runs pytest with `DEER_FLOW_REQUIRE_ZERO_SKIPS=1`; the pytest session hook turns any skip into a release failure.
- `.github/workflows/project-foundation-postgres-tests.yml` hard-checks the administrator URL and invokes the root Make target instead of carrying a second file list.
- `.github/workflows/frontend-unit-tests.yml` runs the M6 static-gate file explicitly.
- The release contract test parses the complete Make variable and compares the exact ordered tuple. It also proves the recipe and workflow consume that single source rather than embedding another list.

## Real process and recovery evidence encoded by the tests

- Scheduler ownership: two independent child processes contend for the PostgreSQL session advisory lock; SIGKILL of the owner must allow the contender to acquire it on a different backend PID.
- Worker takeover: a production Worker leases a real private-run Job, is SIGKILLed, and a second production Worker takes over after lease expiry. The test requires exactly one successful attempt, one lease-lost attempt, and one terminal stream frame.
- Retry safety: `safe`, `unsafe`, and `unknown` Jobs are seeded through production `JobRepository.enqueue(EnqueueJob(...))`; ambiguous unsafe/unknown attempts become dead and are never replayed.
- Scope isolation: before Worker takeover, project B receives a future queued Job, Run, durable stream frames, quota counter/ledger, and audit record through production repositories/services. Exact project/run/job/frame/quota/audit tuples must be byte-for-value equal after project A settles.
- Gateway reconnect: a first real uvicorn process serves the first SSE frame; after formal process replacement, the second real uvicorn process replays only the suffix from `Last-Event-ID`, preserves owner scope, and returns 404 for a foreign scope. The listening socket is pre-bound and handed to uvicorn with `--fd`/`pass_fds`, closing the port-allocation race.
- Recovery: the release file creates an encrypted archive, rejects tampering and journal gaps, restores to random `deerflow_restore_*` databases, replays tombstones, and verifies schema/isolation proof state.
- Quota/audit: concurrent reservations are raced against real PostgreSQL state and audit redaction fails closed without persisting secret-bearing metadata.

## Production compatibility repairs required by the gate

- Historical M4 private-work migration now stops at the M5 final revision (`0013`) rather than silently crossing the explicit M6 reliability migration by upgrading to `head`.
- Partial integration applications install the real quota and operational audit ports required by M6 admission.
- M5 Automation's historical exact-head assertion now targets the M1–M6 release revision without broadening Task 19 into Task 20 documentation.
- Static-site project navigation suppresses project-private Chats/Memory/Connections/Automations entry points.

## TDD evidence

Initial RED evidence:

- Frontend static gate: `947 tests`, `1 failed`, `946 passed`, `0 skipped`; static mode exposed `/projects/alpha/chats`.
- Historical M4 migration: `3 failed, 2 passed`; the migrator attempted `head` and crossed the M6 explicit migration boundary.
- Gateway process gate initially failed after the first uvicorn was already healthy because the test HTTP client inherited ambient proxy transport; `trust_env=False` corrected only test transport.
- Independent-review exact-list contract: failed with `NameError: _make_variable is not defined`.
- Independent-review foreign-scope contract: failed with `NameError: _seed_foreign_scope_sentinel is not defined`.

Pre-review GREEN evidence, before the final review-strengthening patch:

- Three new backend release files: `9 passed in 55.08s`.
- Fixed root M1–M6 release list: `158 passed, 0 skipped in 93.60s`.
- Frontend static gate/full unit run: `947 passed, 0 skipped`.
- Frontend `pnpm check`: passed.

Final review-strengthening patch evidence available in this run:

- Exact Make/workflow and zero-skip child contracts: `2 passed, 3 deselected`.
- All 12 changed Python files: Ruff check passed; Ruff format check reported all 12 formatted.
- Frontend `pnpm check`: passed.
- Both workflow YAML files parsed successfully.
- `make -n test` emitted the exact 20-file ordered command with `DEER_FLOW_REQUIRE_ZERO_SKIPS=1`.
- `git diff --check`: passed.

Final fresh runtime evidence after the review-strengthening patch:

- Targeted PostgreSQL/process release suite: `10 passed`, `0 skipped` in `56.23s` after the fixed-SHA review repair.
- Fixed root M1–M6 PostgreSQL release gate: `159 passed`, `0 skipped` in `94.31s` after the fixed-SHA review repair.
- Frontend static/full unit gate: `947 passed`, `0 skipped`; no snapshot changes.
- Frontend `pnpm check`: passed.
- Root Make release entry with no `POSTGRES_TEST_URL`: failed before pytest with the required public message.
- Ruff check passed; Ruff format reported all `1170` backend Python files formatted; `git diff --check` passed.
- PostgreSQL residual-state query returned no `deerflow_test_*` or `deerflow_restore_*` database.
- Process residual-state query returned no Task 19 Worker, Gateway, or scheduler child.

## Plan reconciliation

- The repository's frontend tests live under `frontend/tests/unit/`, so the new file is `frontend/tests/unit/m6-static-gates.test.tsx` rather than the brief's non-conforming `frontend/tests/` path.
- `.github/workflows/ci.yml` does not exist in this repository. The frontend portion was integrated into the existing `.github/workflows/frontend-unit-tests.yml`; the PostgreSQL portion was integrated into the existing project-foundation workflow.
- The initial independent review found three Important issues (foreign-scope sentinel depth, non-exact Make list assertion, and Task 20 AGENTS scope) and one Minor issue (Gateway port TOCTOU). All four were implemented; AGENTS is back to the baseline text.
- The first fixed-SHA review found three Important issues and one Minor: the root recipe used POSIX-only environment syntax despite the repository's Windows Make branch; the Gateway replay gate did not assert exact SSE ID order; the Task 19 static gate did not create or assert removal of a real mutation-cache entry; and the historical M4 migration error still called the intentional M5 boundary `head`.
- The repair moves URL validation and `DEER_FLOW_REQUIRE_ZERO_SKIPS=1` setup into the Python release runner, makes Make invoke that runner through the existing cross-platform backend command, asserts exact replay order, creates/removes a real scoped mutation-cache row, and names the project Automation final revision accurately.
- The repair RED was `3 failed, 3 deselected` for the Make/runner/message contracts. During the full GREEN gate, an existing quota rotation test exposed `ORDER BY` on a random UUID; the assertion now maps authoritative `source_kind` directly to old/new key IDs instead of inferring time from UUID order.

Pre-repair independent review history:

- Code quality: **PASS**.
- Spec compliance was **pending fresh runtime evidence**.
- Critical: 0; Important: 1; Minor: 0.
- The sole remaining Important item was the unavailable fresh PostgreSQL/process evidence. The fresh gates above now close that evidence gap; a fixed-SHA post-commit review is still required before Task 19 acceptance.
- The first fixed-SHA review at `0b3a4073` reported 0 Critical, 3 Important, and 1 Minor. All four findings have repair code and fresh evidence above; a post-repair fixed-SHA review is still required before Task 19 acceptance.
- The post-repair review at `6dc749df` reported 0 Critical, 0 Important, and one Minor: mapping quota ledger rows by `source_kind` could hide duplicate rows. The final assertion now requires exactly two rows and the exact unordered reserve/release key-ID set; its focused PostgreSQL gate passed `1/1`, `0 skipped`.

Final independent review verdict at `2323cb6b`:

- **APPROVED / Ready to merge: Yes**.
- Critical: 0; Important: 0; Minor: 0.
- The final incremental review confirmed the exact quota ledger cardinality/set repair and found no new issue.

## Residual-state policy

Every process test owns child cleanup in `finally`, while PostgreSQL fixtures own random database teardown. The final fresh run left no `deerflow_test_*` or `deerflow_restore_*` database and no Worker/Gateway/scheduler child.
