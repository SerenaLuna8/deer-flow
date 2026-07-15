# Task 16 Implementation Report

Status: **DONE — frozen review findings repaired**

## Delivered

- Added `/projects/[project_slug]/memory` and `/connections`, each consuming the entered project context and account/project-scoped private-work provider.
- Extracted an injectable shared Memory view. Project Memory uses strict response/import parsing, scoped query keys and backend optimistic versions; Viewer renders list/export only.
- Added strict project connection adapters and a Connections page that uses canonical channel provider metadata plus executable project Agents. Connect/disconnect are imperative calls; transient selection/pending state is cleared in `finally` and no secret-bearing mutation enters TanStack caches.
- Added project file/artifact URL builders and wired project upload list/delete/download, artifact preview/download and sidecar restore/create through the project client. Project routes never fall back to legacy upload/artifact endpoints or host paths.
- Re-enabled the shared artifact and sidecar surfaces for project chat with capability gates. Runner can upload/run; Viewer can list/download/delete own files without gaining upload or run.
- Added Chats, Memory and Connections navigation entries behind the compile-time feature flag, readiness and `private_work.read_own`. `PROJECT_PRIVATE_WORKSPACE` remains false for Task 17.

## TDD Evidence

- Initial adapter RED: **3 files failed / 0 tests collected** because the project Memory, connection and file modules did not exist. Core GREEN: **3 files / 7 tests passed**.
- Memory/page RED: **3 files / 8 tests, 5 failed / 3 passed** for missing operations and routes. Page/shared-view GREEN: **5 files / 12 tests passed**; workspace Memory regression: **3 files / 10 tests passed**.
- Sidecar/artifact RED: **2 files / 9 tests, 5 failed / 4 passed**; scoped resolver RED: **1 file / 3 tests, 1 failed / 2 passed**. Combined GREEN: **3 files / 12 tests passed**.
- Navigation/file behavior RED: **2 files / 6 tests, 3 failed / 3 passed**. GREEN: **2 files / 6 tests passed**, followed by a clean TypeScript check.
- Focused Playwright GREEN: **8/8 passed** across project private data and project chat.

### Frozen Review Repair Wave

The parent review was frozen at **1 Critical + 4 Important** findings, all repaired in this single concentrated wave:

1. **Critical — Memory fact provenance:** strict project Memory parsing and the shared `MemoryFact` type now accept only the backend-valid optional `sourceThreadId` and `sourceRunId` fields. List, export and import are covered with a real-shaped fixture; unknown fields remain rejected.
2. **Important — Memory source routing:** the shared fact list accepts an injected source-link resolver. Workspace keeps `pathOfThread`; project Memory emits `/projects/{encoded_slug}/chats/{encoded_source}` and prefers `sourceThreadId` over legacy `source`.
3. **Important — artifact query ownership:** project artifact content queries now live below the account/project private-work root and are removed on scope transition, while workspace retains its legacy query key and retry behavior.
4. **Important — artifact transport and states:** artifact loading uses the authenticated fetch wrapper, checks HTTP status through the normalized gateway error path and exposes loading/public-error/unavailable UI states without rendering error bodies as content. Project artifact queries disable retries so the public failure state is prompt; workspace defaults are preserved.
5. **Important — Memory mutation lifecycle:** project mutation keys are scope-owned; in-flight work receives an abort signal and scope disposal aborts it. A mount/scope generation guard prevents deferred callbacks from recreating old cache state, and mutation variables/cache entries are removed on transition.

Repair-wave TDD evidence:

- Frozen-finding RED: **4 files / 14 tests, 6 failed / 8 passed**.
- Artifact retry compatibility RED: **1 file / 4 tests, 1 failed / 3 passed**; fixed with project-only retry suppression.
- Repair GREEN: **4 files / 16 tests passed**.
- Initial repaired Playwright run exposed one delayed 503 state while React Query retried; after the scoped retry fix, the final project private-data/chat run was **9/9 passed**.

## Verification

- Repair-focused unit command — **4 files / 16 tests passed, 0 skipped**.
- `pnpm exec playwright test tests/e2e/project-private-chat.spec.ts tests/e2e/project-private-data.spec.ts` — **9 passed**.
- Required focused unit command — **115 files / 832 tests passed, 0 skipped**. Rstest treats the forwarded directory arguments as a full-suite invocation, so the observed count is the full suite.
- `pnpm test -- --run` — **115 files / 832 tests passed, 0 skipped**.
- `pnpm check` — ESLint and TypeScript passed.
- `git diff --check` — passed.

All source gates above were rerun after the final source/lint adjustment. This report was updated only after those gates completed; the Markdown-only report edit did not require rerunning source tests.

## Contract Decisions and Staged Backlog

- The existing project backend exposes connection list/connect/disconnect, but no project-scoped provider discovery, credential/secret replace or rebind contract. This task therefore uses canonical frontend provider metadata and does not call global channel configuration endpoints. Those advanced flows remain staged backend/frontend work.
- Files remain inside chat sidecar/artifact presentation; no standalone file manager was added.
- Advanced race/fuzz/performance hardening remains staged. Existing provider scope transition plus account/project query keys are retained as the runnable isolation boundary.
- Task 17 owns the real PostgreSQL/frontend release gate and feature flip; Task 16 does not enable the project private-work feature.

## Commit

- `feat: add project memory connections and files` (final SHA reported in the handoff because a commit cannot embed its own SHA).
- `fix: close project private data gaps` (repair SHA reported in the handoff because a commit cannot embed its own SHA).
