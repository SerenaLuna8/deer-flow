# M4 Task 6 Third and Final Independent Review

## Verdict

**APPROVED — 0 Critical, 0 Important, 0 Minor.**

Reviewed range:

- Overall Task 6 base: `872a304c`
- Second-repair base: `9f1cafc20271c7a5653d18e03e57b88412bee194`
- Stable reviewed head: `51a0a8e760cfb4dfc4ac9a96a220f6946121de8f`

The second repair closes both Important findings from the post-repair review. The original three Important findings remain closed under fresh cumulative race and compatibility verification. No new Critical, Important, or Minor finding was identified.

## Second-repair findings

### A. Authoritative authorization reason and completion persistence — closed

`RunManager.update_run_completion()` and `set_status()` now use the same per-record `status_write_lock`. Neither holds the manager registry lock across store I/O. Once an authoritative status write publishes `interrupted / authorization_revoked`, completion normalizes the store-facing terminal status/reason and refuses to replace the in-memory public reason, while continuing to persist token, message, and last-message fields.

The exact fresh PostgreSQL regression performed this sequence:

1. Commit the authorization marker.
2. Publish an attempted success through authoritative `set_status()`.
3. Call `update_run_completion()` with `status=error`, `error="completion detail"`, token counters, message count, and last AI message.

Both memory and PostgreSQL remained `interrupted / authorization_revoked`; PostgreSQL and memory retained `total_tokens=17`, `message_count=3`, and the completion message, while PostgreSQL also retained the 5/12 input/output split.

The status-first and completion-first concurrency test proves that the second store write does not begin while the first per-record write is blocked. Final memory/store state is authorization-revoked with all counters retained in both orders. Legacy boolean, `None`, no-store, `error=None`, non-null replacement-error, and missing-row recovery behavior was separately exercised.

Fresh evidence:

- Exact PostgreSQL A+B selection: **2 passed**.
- Status/completion ordering plus no-store, `error=None`, and missing-row compatibility: **6 passed**.
- Independent temporary legacy `None` store-contract test: **1 passed**.

### B. Frozen connection retention during normal connect — closed

Normal ownership transfer now selects only another owner's row whose status is exactly `connected`. It does not mutate `frozen` rows or delete their retained credentials.

The exact fresh PostgreSQL regression established a valid project-scoped frozen owner A with credential version 7 and a same-identity revoked row for owner B. Connecting B produced:

- A remained `frozen`, retained `frozen_at`, credential version 7, and encrypted envelope.
- B became `connected` using the existing row ID.
- Repeating B's upsert returned the same ID and preserved the state.
- In a separate true-transfer case, the formerly connected owner became `revoked`, its credential was deleted, and the new owner became connected.

The existing normal-connect lock-before-lookup test and fresh multi-identity PostgreSQL restore race continue to prove use of the shared deterministic identity advisory lock and exactly one connected winner per identity.

## Cumulative original three Important findings

| Original finding | Final status | Fresh evidence |
| --- | --- | --- |
| MCP initial discovery could occur after revocation | Closed | The boundary is installed before discovery, the marker race produces zero remote calls, revoked materialization leaves no manager registration, and no database transaction is retained across remote discovery. |
| Requested terminal success could be published before the marker-aware authoritative result | Closed | PostgreSQL performs one marker-aware status update; `RunManager` publishes only after the store outcome. Completion now shares the same per-record sequence and cannot replace the revoked public reason. |
| Multi-identity restore could deadlock or choose inconsistent winners | Closed | Identity keys remain deterministic signed int64 values, sorted and deduplicated before locks; bulk restore and normal upsert use the same helper. Concurrent two-project/two-identity restore commits with one connected winner per identity. |

The focused cumulative selection for these three boundaries was **7 passed**.

## Release-gate evidence

All PostgreSQL commands used an independent disposable cluster under `/tmp` on `127.0.0.1:55457`, with explicit `POSTGRES_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:55457/postgres`. Repository fixtures created and dropped only random `deerflow_test_*` databases. No business database or PostgreSQL skip was accepted.

| Gate | Fresh result |
| --- | --- |
| Exact second-repair PostgreSQL A+B | **2 passed** |
| Original three-Important cumulative selection | **7 passed** |
| Task 6 focused | **40 passed** |
| Fresh schema/governance | **127 passed** |
| Gate 2 exact | **99 passed, 6 failed, 1 warning** |
| Mandatory runtime plus Task 6 | **144 passed, 6 failed, 1 warning** |
| Task 4 plus M3 resolver | **171 passed, 1 warning** |
| Task 1-3 exact union | **260 passed, 1 warning** |
| Affected runtime | **281 passed** |
| Legacy channel targeted baseline | **18 failed** |

The six Gate 2/mandatory failures are exactly the previously declared Task 11 lifecycle cases. Each stops at legacy `POST /api/threads` with `409 PRIVATE_WORK_CUTOVER`, before Task 6 admission or runtime logic:

1. stream completion and persistence;
2. real lead-agent business path;
3. cancel interrupt;
4. interrupted title from checkpoint;
5. wait-false title generation;
6. rollback checkpoint restore.

The 18 legacy channel repository failures are isolated Task 10 staged failures. Their old fixtures call connection/OAuth persistence without the final-schema required `project_id`; the failures are `NOT NULL` violations on project-scoped channel rows. They are not counted as green and do not intersect the project-scoped second-repair B regression.

The affected-runtime command initially had one setup error because the filesystem/network sandbox blocked its one localhost PostgreSQL checkpointer test. The exact command was rerun with localhost access and completed at **281 passed**; the setup error was environmental rather than a product result.

## Static and scope audit

- `ruff check` on all four changed Python files: **passed**.
- `ruff format --check`: **4 files already formatted**.
- `python -m compileall -q` on both changed production modules: **passed**.
- `git diff --check` for second-repair base to head: **passed**.
- `git diff --check` for overall Task 6 base to head: **passed**.
- Second-repair production scope is limited to `RunManager` completion sequencing and channel connection transfer filtering; the remaining changes are tests and durable documentation.
- No Task 7 file authority, file repository, file finalizer, artifact finalizer, Memory scope, router cutover, or later-task implementation was introduced. The cumulative Task 6 range contains only the already-reserved `before_file_finalization` authorization protocol hook, explicitly without Task 7 implementation.

## Final disposition

Both post-repair Important findings are closed with fresh real-PostgreSQL evidence, the original three Important findings remain closed, and the cumulative regression/static gates reveal no new Task 6 defect. Task 6 is approved at **0 Critical / 0 Important / 0 Minor**.
