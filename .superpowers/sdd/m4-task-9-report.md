# M4 Task 9 Implementation Report — Project-private Memory

## Status

- Date: 2026-07-15
- Baseline: `6a5686ea`
- Branch: `codex/m4-private-work`
- Implementation commit: `58f5d98f`
- Review: APPROVED (0 Critical / 0 Important)
- Task 10: not started

Task 9 delivers the runnable project-private Memory path. The review was intentionally
limited to one frozen functionality checklist; non-blocking hardening remains backlog.

## Delivered boundary

- `ProjectMemoryStorage` and `PrivateMemoryRepository` persist Memory by
  `(project_id, owner_user_id, namespace)` in PostgreSQL, split summary/facts, and use
  expected-version writes.
- `ProjectMemoryUpdateQueue` uses asyncio debounce and freezes scope, Thread, Run,
  namespace, and membership version when enqueued. It revalidates membership before
  writing and never reads project authority from the later execution ContextVar.
- `MemoryUpdater` loads and saves the same project scope/version and records source
  Thread/Run on extracted facts.
- Memory middleware and the pre-summarization hook route private runs to the project
  queue; the synchronous private hook does not fall back to legacy file Memory.
- Dynamic prompt injection reads only the runtime private scope/namespace and injects
  date-only context when membership is inactive or project Memory is unavailable.
- `PrivateMemoryService` provides status/list/reload/import/export/update/delete.
  Viewer-capable contexts may read/export; writes require the existing create capability.
- Non-project runtimes keep the existing file-backed storage, Timer queue, router, and
  prompt behavior. Project HTTP routing and legacy cutover guards remain Task 11/12 work.

## Verification

The final PostgreSQL run used the isolated local cluster at port `55484`; fixtures
created and dropped random `deerflow_test_*` databases. No business database was used.

| Gate | Result |
| --- | --- |
| Task 9 repository + queue + prompt + summarization | 38 passed, 0 skipped |
| Legacy Memory/router/prompt/updater/summarization | 277 passed |
| Task 9 cumulative subagent gate | 118 passed, 0 skipped |
| Ruff check | passed |
| Ruff format check | 12 files formatted |
| Compileall | passed |
| `git diff --check` | passed |

## Bounded backlog

- Retry/rebase policy for optimistic Memory conflicts.
- Multi-event-loop lifecycle for the process queue singleton.
- Deeper credential/secret message classification.
- Durable failed-update queue records, batching, and performance tuning.
- Optional pgvector retrieval.

These items do not block the runnable Task 9 boundary and were not used to open another
review loop.
