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
