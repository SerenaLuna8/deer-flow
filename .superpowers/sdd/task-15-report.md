# Task 15 Implementation Report

Status: **DONE**

## Delivered

- Added project-scoped chat list and detail routes that only consume `useCurrentProject()`.
- Extracted the workspace chat implementation into `ScopedChatPage`; the workspace adapter preserves existing behavior while the project adapter injects the Task 14 project client and project paths.
- Preserved project history, stream, stop, human-input and upload flows while hiding unsupported goal, compact, branch, regenerate, scheduled-task, sidecar and artifact actions.
- Added the executable-Agent selector backed by the M3 catalog. Explicit UUID creation sends only logical `agentAssetId` / `agentScope` data and navigates to the project detail route.
- Added owner-scoped recent/list views and one public not-found result for missing, other-owner and cross-project Thread metadata.
- Added one readiness query/helper. CTA, chats-list create and project navigation require the compile-time feature flag, the relevant capability, and backend `readiness=ready`; `PROJECT_PRIVATE_WORKSPACE` remains false.
- Kept static-demo and viewer entry points non-runnable, and documented the shared chat ownership in `frontend/AGENTS.md`.

## TDD Evidence

- Initial RED: focused project tests failed because `agent-selector-dialog`, `project-chat-page`, and `recent-private-work` did not exist.
- Shared-page RED: workspace/project adapters failed the extraction regression because `scoped-chat-page.tsx` did not exist.
- Readiness RED: `tests/unit/core/private-work/readiness.test.ts` failed on the missing readiness module.
- Navigation RED: presentation regression failed until project navigation consumed readiness.
- GREEN: Task 15 unit gate passed **19 files / 152 tests**.
- Full frontend GREEN: **108 files / 804 tests**.

## Verification

- `pnpm exec playwright test tests/e2e/project-private-chat.spec.ts` — **2 passed**.
- `pnpm check` — ESLint and TypeScript passed.
- `git diff --check` — passed.
- Playwright trace review proved an early parallel failure was a mock defect: the correct SSE AI message rendered, then the mock state refetch replaced it with pre-run history. The fixture now persists streamed state and the full parallel file passes with the default assertion timeout.

## Self-review

- Project source contains no legacy `/api/threads/*` or `/api/langgraph/threads` calls.
- Agent creation does not send version, owner, or capability claims.
- Project route callbacks are omitted, not merely disabled, for hidden branch/regenerate actions.
- Workspace defaults remain enabled and its existing route still uses the shared adapter.

## Follow-ups / Intentional Gates

- Task 16 owns project Memory and connections.
- Task 17 may enable `PROJECT_PRIVATE_WORKSPACE` after cutover; readiness and capability checks must remain in place.

## Repair Wave: Project Chat Scope Boundaries

Baseline commit: `f938f66f` (`feat: add project private chat experience`).
Repair commit: the single commit containing this report; its final SHA is recorded in the Task 15 handoff because a commit cannot embed its own SHA.

### RED

- Focused project-chat unit: **1 file / 6 tests, 4 failed / 2 passed**. Missing contracts were the suggestion gate, metadata error state, disabled artifact provider, and scoped viewer delete.
- Task 15 Playwright: **6 tests, 5 failed / 1 passed**. It observed a legacy suggestion POST, an artifact surface, no viewer delete, and both metadata-5xx state failures.
- Project thread adapter regression: **1 file / 7 tests, 1 failed / 6 passed** because project HTTP errors discarded status.
- Cached-null metadata regression: **1 file / 6 tests, 1 failed / 5 passed** because a refetch error could lose precedence to stale `data=null`.

### GREEN

- Project routes independently disable follow-up suggestion POSTs while workspace defaults remain enabled.
- Project artifacts are provider-disabled: state setters no-op, tool calls cannot auto-open/select, present-files stays hidden, ChatBox renders no artifact panel, and E2E observes zero legacy artifact requests.
- `private_work.read_own` viewers can delete their owner-scoped threads using the project client without gaining create, run, upload, or branch actions; the row action is not nested inside its link.
- Project HTTP errors retain status. Only normalized missing metadata plus settled empty history/messages shows not-found; metadata errors keep usable history or render a retryable error, with errors taking precedence over cached null data.

### Final Verification

- Task 15 focused unit gate: **19 files / 154 tests passed**.
- `pnpm exec playwright test tests/e2e/project-private-chat.spec.ts`: **6 passed**.
- `pnpm test -- --run`: **108 files / 806 tests passed**.
- `pnpm check`: ESLint and TypeScript passed.
- `git diff --check`: passed.
