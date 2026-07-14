# M4 Task 5 Independent Review

## Scope and verdict

Reviewed commit `90c3c58a` (`feat: admit project runs with exact M3 assets`) against
approved base `b2c919cf` in
`/Users/jiangfeng/deer-flow/.worktrees/m4-private-work`.

**Verdict: CHANGES REQUIRED — 1 Critical, 3 Important, 0 Minor.**

This review is not `APPROVED` because Critical/Important findings remain. Production
code was not modified during the review.

## Critical findings

### C1. Run-scoped Local sandboxes are misclassified, bypassing host-path confinement

`LocalSandboxProvider.acquire_with_mounts()` creates IDs in the form
`local-run:<user>:<thread>:<run>` at
`backend/packages/harness/deerflow/sandbox/local/local_sandbox_provider.py:416`, but
the shared classifier at
`backend/packages/harness/deerflow/sandbox/tools.py:1236-1253` accepts only `local`
and IDs beginning with `local:`. `local-run:` therefore takes the remote-sandbox
branch in every common tool consumer.

The blast radius is centralized and broad. The nine classifier consumers skip Local
behavior for run-scoped exact agents:

- `_sanitize_error` does not mask host paths (`sandbox/tools.py:569-580`);
- thread-directory preparation is skipped (`sandbox/tools.py:1446`);
- `bash` skips the Local host-bash guard, path validation/translation, cwd setup,
  timeout, and Local output masking (`sandbox/tools.py:1697-1733`);
- `ls`, `glob`, and `grep` skip Local path validation/resolution/masking
  (`sandbox/tools.py:1766`, `1839`, `1915`);
- `read_file`, `write_file`, and `str_replace` skip Local path validation and
  virtual-to-host resolution (`sandbox/tools.py:1988`, `2143`, `2209`).

This is a host-filesystem boundary failure, not merely incorrect presentation.
`LocalSandbox._resolve_path_with_mapping()` returns an unmapped path unchanged at
`backend/packages/harness/deerflow/sandbox/local/local_sandbox.py:294-307`. Thus an
exact Agent with configured file tools can pass an absolute host path through
`read_file`/`write_file` without the Local tool-layer allowlist/confinement that
would have rejected it.

The central contract test at
`backend/tests/test_local_sandbox_virtual_path_contract.py:254-265` covers only
`local`, `local:<user>:<thread>`, a foreign ID, and an unset state. It omits
`local-run:`. Fix the shared classifier and add the `local-run:` assertion there so
all nine callers inherit the correction. Add focused read/write/bash assertions for
the run-scoped ID as defense-in-depth; tests that call `LocalSandbox` methods directly
do not exercise this common dispatch boundary.

## Important findings

### I1. Private exact agents inherit global optional mutation/delegation tools

The private `_make_lead_agent` path restricts persisted tool groups and disables the
global MCP cache, but otherwise calls the ordinary `get_available_tools()`
(`backend/packages/harness/deerflow/agents/lead_agent/agent.py:612-623`). That loader
unconditionally appends `skill_manage` whenever the process-global
`skill_evolution.enabled` is true
(`backend/packages/harness/deerflow/tools/tools.py:91-97`) and appends
`invoke_acp_agent` whenever global ACP configuration exists
(`backend/packages/harness/deerflow/tools/tools.py:135-148`).

If the exact Skill set has no explicit `allowed-tools` declaration, the Skill policy
preserves allow-all behavior. A project run can then:

- create/edit/delete user-global custom Skills and refresh the global/user prompt
  cache through `skill_manage`
  (`backend/packages/harness/deerflow/tools/skill_manage_tool.py:114-159,206-239`);
- delegate to a globally configured ACP agent/workspace even though private subagent
  mode was explicitly disabled.

This contradicts the exact persisted Agent/Skill/MCP composition and the no-global-
Skill-pollution invariant. The private path needs an explicit allowlist/denylist for
out-of-band optional tools, with tests that turn on both Skill evolution and ACP.
At minimum, exact private runs must not receive `skill_manage`; ACP needs an explicit
project-runtime policy rather than implicit inheritance from process configuration.

### I2. Exact Skill reads still consult and populate global per-user Skill storage

The exact prompt and slash-activation paths avoid catalog discovery, but the normal
progressive-loading path does not. `read_file_tool()` calls
`_is_disabled_skill_path()` before sandbox resolution
(`backend/packages/harness/deerflow/sandbox/tools.py:2017-2023`). For an exact path
such as `/mnt/skills/custom/<asset-uuid>/SKILL.md`, that helper treats the UUID
directory as a global custom Skill name and calls `get_or_new_user_skill_storage()`
(`backend/packages/harness/deerflow/sandbox/tools.py:192-237`). The storage factory
inserts the instance into the process-global per-user LRU
(`backend/packages/harness/deerflow/skills/storage/__init__.py:234-273`).

Consequences:

- reading a project run's exact Skill pollutes global process Skill state, violating
  the explicit Task 5 invariant;
- an unrelated global `_skill_states.json` entry for the asset-UUID-shaped name can
  override the exact runtime's authoritative `enabled=True` and block the project
  Skill.

Add a trusted exact-runtime Skill-state path to `read_file` that never consults
global storage, and test the real `read_file_tool` path while observing the global
storage cache/state. Existing tests cover prompt/slash rendering and direct
`LocalSandbox.read_file`, so they miss this interaction.

### I3. Temporary private Skill cleanup silently becomes non-retryable

`PrivateAgentRuntime.aclose()` sets `_closed=True` before calling
`shutil.rmtree(self.skill_root, True)`
(`backend/app/private_work/asset_runtime.py:569-573`). The positional `True` is
`ignore_errors=True`. A removal failure is therefore swallowed, the worker cannot
log it, and every subsequent `aclose()` returns without retrying. Project-private
Skill content may remain under `/tmp` indefinitely. Materialization-failure cleanup
also ignores all removal errors at `asset_runtime.py:735-736`.

This does not meet the mandatory deterministic-cleanup invariant. Do not mark the
runtime closed until deletion succeeds; surface/log cleanup failure without replacing
the original public runtime error, and retain a bounded retry path. Add a failure-then-
retry test in addition to the current successful-removal checks.

## Minor findings

None.

## Independent verification

All PostgreSQL verification used only the disposable cluster under
`/tmp/deerflow-m4-task5-review-pg.zmgpAi`, listening on localhost port `55491`.
`POSTGRES_TEST_URL` was explicit, fixtures created random `deerflow_test_*` databases,
and the server was stopped after the tests. No business database was used.

### Task 5 and staged-failure gates

- Four Task 5 suites
  (`test_private_run_admission`, `test_private_asset_runtime`,
  `test_private_runtime_context`, `test_legacy_system_asset_runtime`):
  **92 passed, 0 failed, 0 skipped in 5.49s**.
- Mandatory six-file gate: **96 passed, 6 failed, 0 skipped, 1 warning in 9.01s**.
  All six failures stop at legacy `POST /api/threads` with
  `409 PRIVATE_WORK_CUTOVER`; they are the predeclared Task 11 router-cutover cases
  and do not reach Task 5 admission, materialization, model, MCP, graph, or worker
  execution.

### Regression and risk-based gates

- Local sandbox classifier/provider/middleware suites: **86 passed, 0 skipped in
  0.91s**. This passing result does not cover `local-run:` in the central classifier.
- Task 4 Run/snapshot/event/feedback plus M3 resolver selection: **167 passed,
  0 skipped, 1 warning in 13.26s**.
- Task 1-3 schema/context/import-firewall/Thread/checkpointer boundaries:
  **260 passed, 0 skipped, 1 warning in 25.92s**.
- Affected RunManager/worker/lead-agent/runtime-channel/sandbox selection with the
  disposable PostgreSQL URL: **277 passed, 0 skipped in 3.14s**. An initial run
  without `POSTGRES_TEST_URL` produced `276 passed, 1 skipped`; it was not treated as
  success and was rerun with the explicit disposable URL.

The two warnings are the existing Starlette `httpx` deprecation.

### Quality gates

- `ruff check` over the 25 changed Python files: **All checks passed**.
- `ruff format --check` over the same files: **25 files already formatted**.
- `python -m compileall -q app/private_work app/gateway packages/harness/deerflow`:
  exited 0 with no output.
- `git diff --check b2c919cf..90c3c58a`: exited 0 with no output.
- Worktree status was clean before this review artifact was added.

Passing tests do not change the verdict because none exercises the four defect
boundaries above.
