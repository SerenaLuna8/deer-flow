# M7 Task 10 Report: fixed M1-M7 release and source-absence gates

## Status

PASS — Task 10 establishes the ordered M1-M7 PostgreSQL release gate, production source-absence checks, and a real Gateway/Scheduler/Worker process boundary. This report covers Task 10 only on base `4ab7649cd8591a8d77b0fdf81170cfb2a2cd3160`; it does not mark M7 complete or start Task 11. Independent review remains a separate acceptance step.

## Delivered

- Root `PROJECT_FOUNDATION_POSTGRES_TESTS` is the only ordered 22-file M1-M7 list. CI calls the root target instead of duplicating the order, and a print target gives contract tests a cross-platform way to read the list.
- The gate includes the exact M7 baseline and packaged bootstrap, final project capabilities, job/stream/quota/audit/retention/recovery boundaries, real process behavior, production source absence, and the release contract itself. Missing `POSTGRES_TEST_URL` exits before pytest collection; selected PostgreSQL tests enforce zero skips.
- `test_m7_source_absence.py` scans production roots only and rejects deleted modules, global routes, removed imports, runtime `setup_agent`/`update_agent` residue, and legacy config authority outside the exact tombstone validator allowlist. Historical documentation and tests are not treated as production authority.
- `test_m7_process_boundary.py` starts real Scheduler, Gateway, and Worker processes against one random database. Gateway performs HTTP admission, Scheduler owns only scheduling, and the registered Worker owns the lease, graph execution, durable stream append, terminal append, and settlement. The test restarts Gateway, reconnects by `Last-Event-ID`, and proves account/project/thread scope isolation.
- Process child evidence records PID and role for claim, lease, graph execution, stream append, terminal append, and settlement. Existing Scheduler/Gateway/Worker tests were updated to the final M7 contracts.
- Removed the dead file-backed `setup_agent` and `update_agent` runtime modules and their filesystem-only tests. Final Agent configuration coverage now parses immutable asset payloads; final Skill filesystem coverage uses explicit run-owned read-only mounts and rejects ambient global mounts.
- Removed the remaining global LangGraph/thread/artifact URL fallbacks from Nginx, frontend compatibility config, embedded upload results, and associated docs/tests. Embedded results expose virtual paths only; authenticated project APIs own download URLs.

## TDD and failure classification

Initial new-gate RED result:

```text
9 tests: 6 failed, 3 passed
```

The intended failures were the missing 22-file list/print target, missing process-role evidence, and production legacy residue. After implementing the gate, the first complete backend run exposed 52 obsolete expectations:

```text
52 failed, 6801 passed, 945 skipped
```

They were classified by isolated reruns, not hidden with ordering or skips:

- 31 filesystem Agent write tests were pure legacy. Their dead production tools and dedicated tests were deleted.
- 21 tests represented behavior that remains final: immutable Agent payload parsing, explicit run Skill mounts, project-private SSE dependencies, permanent catalog provider signatures, channel tombstones, shared path constants, and the single M1-M7 list. Those tests were ported and retained.
- 0 failures were cross-test state pollution; every failure reproduced independently.

The migrated focused slice finished:

```text
161 passed, 8 skipped, 0 failed
```

The eight skips were PostgreSQL cases in a non-PostgreSQL focused invocation, not release evidence. The final fixed PostgreSQL gate had zero skips.

## Final verification

Dedicated PostgreSQL gate, using only `127.0.0.1:55443` and random `deerflow_test_*` / `deerflow_restore_*` databases:

```text
M1-M7 release stats: collected=242 passed=242 skipped=0
242 passed in 115.58s
```

Backend gates:

```text
Full backend: 6815 passed, 944 skipped, 0 failed in 92.57s
Blocking-I/O: 27 passed in 11.00s
Ruff format: 1047 files already formatted
Ruff check: All checks passed
Final source/release contracts: 7 passed
```

Frontend gates:

```text
Unit: 122 files, 888 passed, 0 skipped, 0 failed
pnpm check: PASS
Production Playwright: 14 passed
Static Playwright: 2 passed
BUILD_MODE=production pnpm build: PASS
BUILD_MODE=static pnpm build: PASS
Prettier changed scope: PASS
```

The first sandboxed Playwright and final unit invocations could not bind a loopback port (`EPERM`). The same commands passed unchanged with the required local-server permission; these were environment failures, not code failures.

Additional checks:

```text
Fixed Makefile list count: 22
Workflow YAML parse: PASS
git diff --check: PASS
Production source-absence: 4 passed
```

## Resource cleanup

- The disposable PostgreSQL cluster listened only on `127.0.0.1:55443`; no process touched the normal `5432` service.
- Before shutdown, the cluster contained no `deerflow_test_*` or `deerflow_restore_*` databases.
- PostgreSQL was stopped cleanly, `/private/tmp/deerflow_m7_task10_pg_AVi1zn` was removed, and port `55443` was confirmed closed.
- Final process inspection found no Gateway, Scheduler, Worker, or process-test child from this worktree.

## Self-review

- The Gateway and Scheduler process sources do not import graph execution code; graph/stream/terminal role evidence is produced only by the Worker PID.
- Reconnect assertions cover cursor ordering and deduplication across a real Gateway restart, plus foreign-account denial.
- Source scanning deliberately excludes historical docs/tests while checking runtime Python, frontend application code, scripts, and Nginx configuration.
- The root Makefile remains the sole CI order source. The test contract contains an expected order for drift detection, but workflows do not duplicate it.
- No progress ledger was changed and Task 11 was not started.

## Remaining acceptance boundary

Task 10 implementation and mandatory gates are complete. Independent review must still inspect the committed diff before Task 10 is formally accepted; M7 final documentation/closure belongs to Task 11.

## Independent-review repair (2026-07-19)

The first independent review found `0 Critical / 2 Important / 1 Minor`. The
repair stayed inside Task 10 and did not update the progress ledger or begin
Task 11.

- Source absence now inventories 83 unique deleted production paths, including
  every removed frontend surface, global router, config backend, memory store,
  pre-M7 migration, migration CLI, legacy skill writer, and recovery helper.
  The route inventory contains 18 removed global prefixes, including
  `/api/assistants`, `/api/console`, `/api/input-polish`, and
  `/api/suggestions`.
- Python route, import, runtime-symbol, and config checks are AST-based, so
  comments cannot create false positives. Nginx `location` and `rewrite`
  directives have a dedicated unquoted scan. Mutation tests prove that an
  unquoted Nginx route and a config tombstone literal are rejected while a
  historical Python comment is ignored.
- The process child no longer constructs a custom executor or handler. It calls
  `run_worker(handlers=None, agent_runner=...)`; production constructs the real
  `RunAgentPrivateExecutor` and `PrivateRunJobHandler`, and only the graph runner
  is controlled. Claim, graph, stream, and terminal PID evidence is recorded
  from inside that real execution boundary, while settlement evidence observes
  the production handler commit from PostgreSQL.
- Gateway and Scheduler are checked through a structured, transitive local
  import graph and an isolated runtime `sys.modules` probe. The general runtime
  packages now lazy-load `run_agent`/`RunContext` only for explicit Worker-side
  consumers, so importing Gateway or Scheduler does not import the Worker,
  private executor, graph runner, or lead-agent graph.
- Embedded upload docs now state the local-path plus `virtual_path` contract and
  explicitly exclude host HTTP artifact URLs. The unused global
  `UploadedFileInfo`, `UploadResponse`, and `UploadListResponse` models were
  removed. The stale global suggestions-config request was also removed; the
  composer continues to use its project-scoped suggestions endpoint.

Repair RED evidence:

```text
12 focused tests: 9 failed, 3 passed
```

The failures covered the incomplete path/route inventory, Nginx and config
mutations, Python comment false positive, custom process executor/handler,
missing production runner seam, stale upload docs, and old response models.

Repair verification:

```text
Focused source/process/upload contracts: 13 passed
Related backend regression: 273 passed, 2 PostgreSQL skips
Real M7 Gateway/Scheduler/Worker process gate: 4 passed
Worker crash/takeover process gate: 3 passed
M1-M7 PostgreSQL gate: 247 passed, 0 skipped in 124.95s
Full backend: 6825 passed, 941 skipped in 109.78s
Blocking-I/O: 27 passed in 11.24s
Ruff: All checks passed; 1047 files already formatted
Frontend unit: 122 files, 888 passed, 0 skipped
Frontend check: PASS
Production Playwright: 14 passed
Static Playwright: 2 passed
BUILD_MODE=production pnpm build: PASS
BUILD_MODE=static pnpm build: PASS
Prettier changed scope: PASS
git diff --check: PASS
```

The first sandboxed frontend unit invocation failed to bind `::1:3000` with
`EPERM`; the unchanged command passed with local-server permission. The first
verbose backend run completed but its final summary was truncated, so the same
suite was rerun with quiet output to capture the counts above. Neither event was
a product failure.

The repair PostgreSQL cluster listened only on `127.0.0.1:55444`. Before
shutdown it contained no `deerflow_test_*` or `deerflow_restore_*` databases.
It was stopped cleanly, its exact temporary directory
`/private/tmp/deerflow_m7_task10_repair_pg_0Vfwla` was removed, port `55444`
was confirmed closed, and no process-test child from this worktree remained.

All fixed review findings are repaired. Formal acceptance still requires the
independent re-review of the repair commit; Task 11 remains out of scope.

## Second independent-review repair (2026-07-19)

The fixed second review found `0 Critical / 2 Important / 0 Minor`. The repair
again stayed inside Task 10: it did not update the progress ledger or begin
Task 11.

- The production frontend scan now covers every JavaScript/TypeScript source
  suffix under `frontend/src`, with explicit fixture, mock, story, test, and
  declaration exclusions. JavaScript string extraction catches exact route
  literals in hooks and `.ts`/`.tsx`/`.js` core files without treating module
  aliases such as `@/core/api/feedback` as HTTP routes.
- Nginx comments are stripped before checking real `location` and `rewrite`
  directives. Mutations prove that comments do not fail the gate while both
  real directive forms do.
- The file-wide `app_config.py` tombstone allowlist is gone. The source gate
  permits exactly one `LEGACY_CONFIG_TOMBSTONES` definition and only the two
  expected AST consumption shapes in the two `mode="before"` validators; an
  extra `REINTRODUCED = "run_events"` literal is rejected.
- The structured local import graph now walks every scope, so imports nested
  inside Gateway or Scheduler functions are visible. A mutation proves a
  function-local `RunAgentPrivateExecutor` import is rejected.
- Production `run_worker` no longer exposes an `agent_runner` test seam. The
  process child patches the existing `RunAgentPrivateExecutor` runner seam in
  the child process, then calls the exact production entrypoint
  `run_worker(handlers=None)`. The real executor, handler, Worker PID evidence,
  PostgreSQL settlement, and reconnect boundary remain exercised.

Second-repair RED evidence:

```text
18 focused tests: 9 failed, 9 passed
```

The nine intended failures covered Nginx comment handling, the unscanned hooks
and exact frontend literals, file-wide `app_config.py` bypass, the production
runner seam, the non-exact child entrypoint, and function-local import graph
coverage.

Second-repair verification:

```text
Focused source/process contracts: 20 passed
Related backend regression: 169 passed, 1 PostgreSQL skip
Real M7 Gateway/Scheduler/Worker process gate: 5 passed
Worker crash/takeover process gate: 3 passed
M1-M7 PostgreSQL gate: 256 passed, 0 skipped in 117.66s
Full backend: 6831 passed, 944 skipped in 98.92s
Blocking-I/O: 27 passed in 10.99s
Ruff: All checks passed; 1047 files already formatted
git diff --check: PASS
```

The single related-regression skip was the PostgreSQL-only rollback case in a
non-PostgreSQL invocation; both the dedicated process gates and the fixed
22-file PostgreSQL gate ran separately with zero skips. No frontend production
file changed in this repair; the prior full frontend verification remains the
Task 10 frontend evidence.

The second-repair PostgreSQL cluster listened only on
`127.0.0.1:55445`. Before shutdown it contained no `deerflow_test_*` or
`deerflow_restore_*` databases. It was stopped cleanly, its exact temporary
directory `/private/tmp/deerflow_m7_task10_repair2_pg_bmHfGF` was removed, port
`55445` was confirmed closed, and no process-test child from this worktree
remained. Formal acceptance still requires independent re-review; Task 11
remains out of scope.

## Third independent-review repair (2026-07-19)

The fixed third review found `0 Critical / 1 Important / 1 Minor`. This
gate-only repair stayed inside Task 10: it changed no production source, did
not update the progress ledger, and did not begin Task 11.

- The structured import graph now recognizes literal module names loaded by
  all six common dynamic forms: `importlib.import_module`, an aliased
  `importlib`, direct and aliased `from importlib import import_module`,
  `__import__`, and `builtins.__import__`. The AST walk covers module,
  function, class, and lambda scopes.
- Unknown or variable dynamic targets fail closed with an unresolved-import
  marker. The two configuration-driven external module loaders have exact
  module/scope/argument-name exceptions.
- Legitimate lazy graph imports are allowlisted only by exact
  module/scope/literal triples. A mutation in `deerflow.agents.__getattr__`
  proves that the expected factory target is accepted while an added private
  executor target is still followed and rejected; there is no path-wide
  exemption.
- The frontend lexical scanner now skips real `//` and multi-line `/* */`
  comments before collecting JavaScript/TypeScript strings. It preserves
  comment markers and escaped quotes inside strings, continues to inspect URL
  double slashes, and rejects real single-quoted, double-quoted, and template
  route literals.

Third-repair RED evidence:

```text
15 focused cases: 10 failed, 5 passed
```

The ten intended failures were the six unrecognized constant dynamic import
forms, an imprecise lazy-import boundary, a variable target that did not fail
closed, and the two JavaScript comment false positives. The five pre-green
cases proved that comment-looking text inside strings could not hide a real
route even before the lexer repair.

Third-repair verification:

```text
New focused mutations: 15 passed
Complete source/process non-PostgreSQL slice: 38 passed, 1 PostgreSQL deselected
Real M7 Gateway/Scheduler/Worker process file: 13 passed
Worker crash/takeover process gate: 3 passed
M1-M7 PostgreSQL gate: 274 passed, 0 skipped in 117.65s
Blocking-I/O: 27 passed in 11.01s
Ruff: All checks passed; 1047 files already formatted
git diff --check: PASS
```

The process file now contains the original five boundary tests plus eight new
dynamic-import mutation cases. The third-repair PostgreSQL cluster listened
only on `127.0.0.1:55446`; before shutdown it contained no `deerflow_test_*` or
`deerflow_restore_*` databases. It was stopped cleanly, its exact temporary
directory `/private/tmp/deerflow_m7_task10_repair3_pg_Ab3hPC` was removed, port
`55446` was confirmed closed, and no process-test child from this worktree
remained. Formal acceptance still requires independent re-review; Task 11
remains out of scope.
