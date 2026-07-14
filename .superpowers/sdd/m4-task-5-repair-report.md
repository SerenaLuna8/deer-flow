# M4 Task 5 Repair Report

## Scope and result

Repaired the findings in `m4-task-5-independent-review.md` against Task 5 commit
`90c3c58a` without implementing Task 6 or later router cutover work.

**Result: C1 and I1-I3 are closed.** The adjacent temporary-root creation failure
boundary is also mapped to the stable private-work error contract. All final Task 5,
earlier-milestone, risk-based, and quality gates passed. The mandatory six-suite gate
has only the six predeclared Task 11 staged `409 PRIVATE_WORK_CUTOVER` failures.

## Finding-to-fix mapping

### C1: `local-run:` bypassed common Local sandbox boundaries

- The central `is_local_sandbox()` classifier now recognizes `local`, `local:...`,
  and `local-run:...`, so all existing consumers apply the same Local confinement,
  virtual-path resolution, error masking, directory setup, and host-bash guard.
- The central contract test covers `local-run:`.
- Focused common-tool tests verify run-scoped Local read/write resolution and output
  masking, and verify that disabled host bash never reaches sandbox execution.

### I1: exact private agents inherited global optional tools

- `get_available_tools()` now has default-preserving `include_skill_manage` and
  `include_acp` switches. Legacy/system runtimes retain both defaults.
- The private exact-agent path explicitly disables global MCP, Skill mutation, and
  ACP delegation tools. When ACP is disabled, the loader does not read or construct
  ACP configuration at all.
- The exact-agent test enables global Skill evolution and ACP and proves both tools
  remain absent while the explicit loader flags are present.

### I2: exact Skill read/list touched global per-user Skill state

- Exact Skill read/list bypasses global Skill enablement/storage only when the runtime
  carries a typed `RunScopedReadOnlyMount` whose `run_id` matches the trusted runtime
  `run_id` and whose container root contains the requested path.
- Exact Skill reads stay virtual until the run-scoped `LocalSandbox` mapping resolves
  them to the per-run temporary tree. Ordinary/global Skill paths preserve existing
  discovery, enablement, and storage behavior.
- Tests exercise the real `read_file_tool` and `ls_tool`, make global user Skill
  storage fail if touched, and reject forged dictionary markers and wrong-run mounts.

### I3: cleanup silently became non-retryable

- Private Skill tree removal performs three bounded attempts, treats an already
  absent tree as success, and raises a generic path-free cleanup error after persistent
  failure.
- `PrivateAgentRuntime._closed` changes only after removal succeeds, so persistent
  failure remains retryable.
- Materialization preserves the original stable public error if cleanup also fails,
  while logging only a generic cleanup message. Worker finalization also omits exception
  traceback text that could expose the host path.
- Temporary-root creation, including `mkdtemp()` failure, maps to
  `PrivateWorkUnavailable(request_id)` without leaking host paths.

## Test-driven evidence

### RED

Tests were added before production changes and failed at the reviewed boundaries:

- Core pure repair selection: **8 failed in 0.64s**. Failures showed `local-run:`
  classified as non-Local, file tools receiving virtual paths instead of resolved
  host paths, host bash executing, optional exact-agent tools leaking, exact Skill
  reads touching global storage, the typed-mount helper missing, and cleanup neither
  retrying nor hiding the path.
- Worker cleanup log regression: **1 failed in 0.31s** because the captured log exposed
  `private-host-path-sentinel` through exception information.
- PostgreSQL materialization cleanup regression: **1 failed in 0.65s** because the
  cleanup exception exposed the temporary host path instead of preserving the stable
  asset error.
- Temporary-root creation regression: **1 failed in 0.36s** before the stable
  creation helper existed.

### GREEN

- Core pure repair selection: **9 passed in 0.51s**.
- Persistent-cleanup PostgreSQL regression: **1 passed in 0.59s**.
- Temporary-root creation regression: **1 passed in 0.30s**.
- Final four Task 5 suites: **98 passed in 5.80s**.
- Local sandbox risk suite (classifier, provider mounts, middleware/lifecycle, common
  tool security, and user-scoped Skill storage): **243 passed in 2.10s**.
- Final exact-agent/exact-Skill plus legacy Skill/ACP/tool-dedup selection:
  **33 passed in 0.66s**.

## Required PostgreSQL gates

All PostgreSQL tests used the disposable cluster under
`/tmp/deer-flow-m4-task5-pg.bbrrG4` on localhost port `55437` with explicit
`POSTGRES_TEST_URL`. Fixtures created only random `deerflow_test_*` databases. No
business database was used and no PostgreSQL skip was accepted as success.
The disposable server was stopped after verification.

- Mandatory six-suite gate: **102 passed, 6 failed, 1 warning in 9.21s**.
  The only failures are:
  - `test_stream_run_completes_and_persists_runtime_state`
  - `test_stream_run_executes_real_lead_agent_setup_agent_business_path`
  - `test_cancel_interrupt_stops_running_background_run`
  - `test_cancel_interrupt_generates_missing_title_from_checkpoint`
  - `test_cancel_wait_false_generates_title_from_graph_input_before_checkpoint`
  - `test_cancel_rollback_restores_pre_run_checkpoint`

  All six are the predeclared Task 11 lifecycle cases stopped by the staged legacy
  `409 PRIVATE_WORK_CUTOVER`; there is no Task 5 admission/runtime failure.
- Task 4 Run/snapshot/event/feedback plus M3 resolver selection:
  **166 passed, 1 warning in 15.05s**.
- Task 1-3 schema/context/import-firewall/Thread/checkpointer boundaries:
  **260 passed, 1 warning in 28.25s**.
- Affected RunManager/worker/lead-agent/runtime-channel/sandbox selection:
  **277 passed in 3.16s**.

The warnings are the existing Starlette `httpx` deprecation.

## Quality gates

- `ruff check` for all 10 changed Python files: **All checks passed**.
- `ruff format --check` for all 10 changed Python files:
  **10 files already formatted**.
- `python -m compileall -q` for all changed production Python modules: exited 0.
- `git diff --check`: exited 0.

## Durable documentation

`backend/AGENTS.md` now records the typed matching-run exact Skill boundary, the
central Local classifier requirement, and bounded/path-free/retryable private runtime
cleanup semantics.
