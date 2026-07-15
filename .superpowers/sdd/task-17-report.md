# Task 17 Implementation Report

Status: **DONE — M4 private-work release gates are runnable and the feature entry is enabled**

## Delivered

- Added a concise real-PostgreSQL M4 release gate and reusable support fixture. The eight behavior-level tests cover final Alembic/cutover readiness, owner and Viewer boundaries, owner/project isolation, project and system Agent snapshots, real LangGraph checkpoint scope metadata, stale catalog generation, authorization revocation, run/event/feedback, file/artifact, Memory, Connections and encrypted credential persistence.
- Extended the existing PostgreSQL CI workflow with the fixed M4 private-work and M4 migration files while preserving the explicit hard failure when `POSTGRES_TEST_URL` is absent.
- Extended existing project E2E fixtures only for the missing stop, human-input and upload behaviors, and added one focused isolation spec for project switching plus the static demo landing-path contract. Existing Task 15/16 tests continue to own Viewer/direct URL, artifact, Memory and Connections coverage.
- Enabled `PROJECT_PRIVATE_WORKSPACE` only after the backend gate was green and the frontend RED confirmed the remaining blocker was the disabled entry. Readiness and capability gates remain intact, and disabled/loading/error CTA copy now describes current service state rather than a future milestone.

## TDD Evidence

- Backend RED: **1 failed** — `test_m4_release_database_is_at_final_head_and_cutover_ready` failed while the support readiness stub returned false.
- Frontend unit RED: **115 files / 832 tests, 1 failed / 831 passed** — the release prerequisite still asserted that private project work was hard-disabled.
- Frontend isolation RED: the new ready-state entry assertion failed while `PROJECT_PRIVATE_WORKSPACE` remained false.
- Backend GREEN: the focused M4 PostgreSQL file passed **8/8** after the reusable fixture and representative contract matrix were implemented.
- Frontend GREEN: the fixed four-spec Playwright gate passed **23/23** after enabling the feature and correcting strict mock contracts (UUID thread IDs and `single_choice` human-input mode).

## Concentrated Release-Gate Repair Wave

- Recent private work now requires the compile-time feature, `private_work.read_own`, a non-static build and backend readiness `ready` before rendering links or enabling Thread search. `migration_required` and `unavailable` return no recent section and keep the search query disabled.
- Repair RED: full Rstest ran **115 files / 835 tests** with **3 expected failures / 832 passed**. The failures proved that `migration_required`, `unavailable` and static mode still rendered Recent private work before the fix. Repair GREEN: **116 files / 836 tests passed** after adding readiness/static wrapper tests and a component harness that renders CTA, project navigation and Recent together while asserting readiness and Thread queries remain disabled.
- The Viewer PostgreSQL case now calls the real `PrivateRunAdmissionService.admit(..., PrivateRunCreate())` against a repository-seeded owned Thread, asserts `PrivateWorkForbidden`, then verifies zero rows in `runs`, `run_asset_versions` and `run_mcp_grant_snapshots` before the Viewer exercises own-delete.
- The isolation E2E now holds the Alpha project Thread response, switches through actual Next.js `Link` navigation via the workspace and Beta project card, renders Beta data, then releases the Alpha response and proves it cannot backfill. It does not use `page.reload` and is accurately named as a project-switch E2E.
- Account identity transition is not claimed as E2E coverage. The existing focused identity/scope integration tests prove that external identity changes reject old refresh commits, abort scope work before cleanup and drop deferred old-scope results without recreating their query; the focused command passed **2 files / 11 tests**.
- Static build coverage is split accurately: the pure landing-path contract only proves legacy chat routing, while the new component harness mocks `isStaticWebsiteOnly() === true`, renders CTA/navigation/Recent and proves no private entry or enabled readiness/Thread query exists.

## PostgreSQL Safety and Verification

- The verification cluster was initialized solely for Task 17 at `/tmp/deerflow_m4_task17_pg_019f5eb9` and listened only on `127.0.0.1:55417` with an empty disposable `postgres` administrator database.
- `POSTGRES_TEST_URL` was `postgresql+asyncpg://postgres@127.0.0.1:55417/postgres`. The integration fixture derived a new random `deerflow_test_<pid>_<uuid>` database for every case and dropped it afterward. The M4 readiness helper independently rejects any current database name outside that pattern.
- No business database URL or existing application database was used. The disposable cluster was stopped after the final database gate.
- Focused M4 PostgreSQL command — **8 passed in 2.25s**.
- Required six-file PostgreSQL command — **16 passed in 4.79s**:
  - M1 cutover
  - project isolation
  - M2 governance
  - M3 shared assets
  - M4 private work
  - M4 private-work migration
- Changed Python `ruff check` and `ruff format --check` — passed. The full six-file database gate was rerun after Ruff formatted the new Python files.
- Workflow YAML parsed successfully; the readable fixed list contains exactly the six required integration files and retains the explicit `POSTGRES_TEST_URL` guard.

## Frontend and Repository Verification

- Required four-file Playwright command — **23 passed**.
- `pnpm test -- --run` — **116 files / 836 tests passed, 0 skipped**.
- Focused auth identity/private-work scope command — **2 files / 11 tests passed**.
- `pnpm check` — ESLint and TypeScript passed.
- `git diff --check` — passed.

## Staged Backlog

- Exhaustive adversarial races, fuzzing, high-volume/performance benchmarks and broader authorization-boundary permutations remain intentionally staged; Task 17 validates the representative runnable release contract.
- Advanced project connection provider discovery, credential replace/rebind UI and secret-management expansion remain staged behind future backend/frontend contracts.
- Broad documentation synchronization and the final release review belong to Task 18 and were not started here.

## Commit

- Baseline: `test: enforce M4 private work release gates` (`323a48fc`).
- Concentrated repair: `fix: close M4 release gate gaps` (final SHA reported in the handoff because a commit cannot embed its own SHA).
