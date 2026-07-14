# M4 Task 5 Report: project run admission and exact M3 runtime

## Outcome

Task 5 adds an internal project-private admission/materialization path without adding
project HTTP routers or a second runtime. A private Run is admitted with an exact,
secret-free M3 Agent/Skill/MCP/grant snapshot and is then registered with the existing
`RunManager` and executed once by the existing `run_agent()` worker. Legacy system
assets keep their pre-cutover runtime; the legacy path rejects project-scoped assets.

## TDD evidence

The initial focused RED was recorded before the production modules existed:

```text
cd backend && uv run pytest \
  tests/test_private_run_admission.py \
  tests/test_private_asset_runtime.py \
  tests/test_private_runtime_context.py -q

3 errors during collection
ModuleNotFoundError: app.private_work.run_admission / asset_runtime / runtime_context
```

Subsequent RED cases covered deterministic same-Thread admission, catalog/grant drift,
exact Skill sandbox isolation, forged runtime authority, mixed-type MCP secret echoes,
unsupported run mounts, host-bash read-only bypass, model allowlist ordering, and
post-transaction malformed Skill cleanup. Each was made green in the focused suites;
concurrency tests use barriers/events and bounded timeouts rather than sleeps.

Final Task 5 implementation gates:

```text
# Pure runtime/context + legacy compatibility
70 passed in 0.88s

# PostgreSQL admission + asset materialization
22 passed in 5.23s

# Four affected Task 5 suites after formatting
92 passed in 5.86s
```

## Admission and lock order

`PrivateRunAdmissionService.admit()` accepts only an issued `PrivateWorkContext` and a
server-normalized `PrivateRunCreate` (`pending`, `reject`, exact assistant/model chosen
server-side). In one caller-owned PostgreSQL transaction it performs:

1. project `FOR UPDATE` revalidation;
2. membership `FOR UPDATE` revalidation and independent checks for
   `private_work.create` and `shared_assets.execute`;
3. scoped active Thread `FOR UPDATE` and active-Run conflict check;
4. session-bound exact M3 Agent resolution;
5. exact Agent/Skill/MCP version and credential-closure locks;
6. catalog-generation `FOR UPDATE` comparison;
7. atomic pending Run, ordered asset snapshot, and grant snapshot insert.

Any failure rolls back the complete transaction. Real PostgreSQL tests prove exactly
one same-Thread concurrent admission wins and leaves one Run plus one snapshot, while
cross-project, other-owner, guessed UUID, stale membership, suspended project, and
deleted/frozen Thread access fail with the stable anti-enumerating contract.

## Exact runtime and secret-zero audit

- Materialization starts from the persisted Run snapshot, re-locks the scoped Run and
  exact asset rows, reloads each exact version/checksum, compares dependency order and
  current grant closure, and compares catalog generation last. Current logical binding
  drift cannot change the Run's selected versions.
- The exact Agent model is checked against the live configured model allowlist before
  Skill/MCP materialization. Missing models produce a stable stale-asset error with
  zero materializer, MCP, model-factory, or graph calls.
- Skill archives are validated and parsed in a `0700` run-owned temporary tree. A
  run-scoped sandbox mount replaces the entire configured Skill root, so public,
  legacy, and other-user Skills are absent. Worker cleanup removes both the host tree
  and the provider's `(owner, thread, run)` sandbox entry without disturbing the
  reusable legacy thread sandbox.
- Providers without run-scoped read-only mounts fail before model construction. Local
  exact runs additionally fail closed when `sandbox.allow_host_bash=true`, because path
  translation cannot make arbitrary host shell commands read-only.
- MCP discovery and invocation use one-shot clients. The only plaintext boundary is
  `materialize_mcp_secrets_in_session`; material exists only in local call variables.
  Run DTOs, snapshots, manifests, proxy tools, configs, checkpoints, logs, exceptions,
  cache state, and reprs contain no plaintext. Discovery schemas and returned values
  are recursively scanned across strings, bytes, numeric/boolean scalars, mappings,
  sequences, Pydantic models, and dataclasses; unknown/cyclic shapes fail closed.
- MCP invocation locks/decrypts the exact closure before locking the generation row,
  commits the verification transaction before the external call, and rechecks on every
  proxy invocation. A deterministic writer test proves grant repinning serializes at
  the credential closure and the next call observes stale generation/grant state.
- Temp archive/parse validation maps to `PRIVATE_WORK_ASSET_STALE`; storage/temp IO and
  unknown failures map to `PRIVATE_WORK_UNAVAILABLE`. Cleanup and compensation errors
  cannot replace the original public error or expose internal paths/secret sentinels.

## Runtime context and single-worker lifecycle

`prepare_private_run_config()` recursively removes client-supplied project/owner/user,
capability, Agent/model/Skill/MCP, asset-context, guardrail attribution, channel,
sandbox, Run/Thread, checkpoint marker, private-hook, secret, and internal execution
fields from top-level config and nested `context`/`configurable` data. It clamps the
recursion limit and injects only trusted Thread ID plus an opaque harness scope. The
worker overwrites the real Run/Thread/user values from the admitted persisted Run;
ambient contextvars and request DTOs cannot supply authority.

`start_private_run()` performs admission, pre-materialization model validation,
materialization, `RunManager.register_persisted()`, and one shared launch. The worker
uses the existing StreamBridge, journal, checkpointer, status/completion, cancellation,
workspace-change, and cleanup paths. Private factory dispatch is explicit and cannot
fall back to the legacy callable. Exact private agents do not load global Agent config,
global MCP cache, setup/update Agent tools, plan/bootstrap modes, or the task/subagent
tool even when those flags are forged by the client.

## Mandatory matrix and staged failures

The mandatory focused command from the Task 5 brief completed with exactly:

```text
96 passed, 6 failed, 0 skipped, 1 warning in 9.03s
```

All six failures remain the predeclared Task 11 router-cutover cases. Each fails while
creating a Thread through legacy `POST /api/threads` with
`409 PRIVATE_WORK_CUTOVER`; none reaches Task 5 admission, materialization, model,
graph, MCP, or worker execution:

1. `test_stream_run_completes_and_persists_runtime_state`
2. `test_stream_run_executes_real_lead_agent_setup_agent_business_path`
3. `test_cancel_interrupt_stops_running_background_run`
4. `test_cancel_interrupt_generates_missing_title_from_checkpoint`
5. `test_cancel_wait_false_generates_title_from_graph_input_before_checkpoint`
6. `test_cancel_rollback_restores_pre_run_checkpoint`

The other required cases are green: atomic pending snapshot before execution,
independent capabilities, busy/non-executable rejection, generation and binding drift,
grant revoke/repin/inactive envelope/mismatch, run-temp cleanup, client forgery,
secret-zero boundaries, scope isolation/deleted/frozen Thread, legacy separation, and
exactly-once shared lifecycle launch.

## Regression gates

All PostgreSQL commands used the disposable `/tmp` server at port `55437`; test
fixtures created random `deerflow_test_*` databases. `POSTGRES_TEST_URL` was explicit
and the reported PostgreSQL gates had zero skipped tests.

```text
# Task 4 run/snapshot/event/feedback + M3 resolver
166 passed, 0 skipped, 1 warning in 13.22s

# Task 1-3 schema/context/import-firewall/Thread/checkpointer boundaries
260 passed, 0 skipped, 1 warning in 26.41s

# Affected RunManager/worker/lead-agent/runtime-channel/sandbox tests
277 passed, 0 skipped in 3.23s
```

The warning is the existing Starlette `httpx` deprecation. The exact commands are the
brief's focused command plus the Task 1-4 file lists recorded in their briefs; the
affected-runtime command covered `test_run_manager`, rollback/Langfuse/subagent worker,
lead-agent model/prompt/skills, runtime channel merge, sandbox middleware/lifecycle,
and Local sandbox mount/virtual-path suites.

Final quality gates run before commit:

```text
uv run ruff check <Task 5 changed Python paths>
uv run ruff format --check <Task 5 changed Python paths>
python -m compileall app/private_work app/gateway packages/harness/deerflow
git diff --check
```

Result: Ruff reported `All checks passed`; format check reported `42 files already
formatted`; compileall and `git diff --check` exited successfully with no output.

## Scope

This task does not add Task 6 authorization-revocation side-effect boundaries, Task 11
project routers, Task 12 legacy cutover, default authority, SQLite, PostgreSQL RLS, or
an upstream LangGraph checkpoint-schema change.
