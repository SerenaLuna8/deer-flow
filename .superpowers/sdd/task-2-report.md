# M7 Task 2 Report: PostgreSQL-only system assets

## Status

PASS — implemented the deterministic packaged system-asset bootstrap and removed the pre-cutover/file-backed runtime authority described by Task 2. This report covers only Task 2 on base commit `528e43c7fa5ad537885db4e41522e0c729ba8494`; it does not claim M7 completion or release readiness.

## Delivered

- Added a strict, versioned `catalog.json` plus authenticated Agent/Skill payloads loaded through `importlib.resources`.
- Added a single-transaction, idempotent PostgreSQL bootstrap with deterministic IDs, published versions, an `asset_catalog_state` lock, and the fixed non-login builtin principal `00000000-0000-0000-0000-000000000007`.
- Added fail-closed conflict handling, digest/path/symlink validation, rollback coverage, and checks proving that bootstrap creates no credential or project binding.
- Made `PostgresAssetCatalogProvider` permanent: removed `cutover_at`, `is_cutover_enabled()`, cutover constants, and filesystem fallback behavior.
- Removed the legacy Agent/Skill/MCP/Features routers, Skill archive installer, per-user/global storage factories, legacy Skill category, and `skill_manage` mutation surface.
- Refactored lead-agent prompt/activation, client, subagent, channel, sandbox, MCP, and ACP paths so ambient repo/user/custom/extensions state is not run authority. Project Worker runs consume exact admitted snapshots; embedded/global paths fail closed.
- Updated root and backend `AGENTS.md` guidance without changing the milestone completion count.

## TDD evidence

Initial focused RED:

```text
13 failed, 35 passed in 7.52s
```

Failures were the intended missing bootstrap package, pre-cutover provider contract, legacy storage/import paths, and extensions-backed runtime behavior. A later invariant test for a preexisting builtin-principal project membership also failed before the membership conflict check:

```text
1 failed
E   Failed: DID NOT RAISE <class 'app.shared_assets.bootstrap.service.BootstrapConflict'>
```

Both sets were made green by the production changes, not by weakening the assertions.

## Final verification

The worktree harness package was forced with `PYTHONPATH=packages/harness` because `backend/.venv` is shared with the main checkout and its editable install points there. All commands still used the required `backend/.venv/bin/pytest` binary.

Required randomized-PostgreSQL affected gate:

```text
POSTGRES_TEST_URL=postgresql+asyncpg://jiangfeng@127.0.0.1:55437/postgres \
PYTHONPATH=packages/harness .venv/bin/pytest \
  tests/test_m7_asset_bootstrap_postgres.py \
  tests/test_asset_catalog_provider.py \
  tests/test_private_asset_runtime.py \
  tests/integration/test_m3_asset_resolution_postgres.py \
  tests/test_harness_boundary.py -q --tb=short -rs

63 passed in 13.61s
```

PostgreSQL skips: **0**. The final run required unsandboxed localhost access because sandboxed TCP returned `Operation not permitted`; `pg_isready` confirmed the designated disposable listener before rerun.

Adjacent unit gate:

```text
289 passed in 1.81s
```

This covered lead Skill prompt behavior, embedded client fail-closed behavior, ACP, sandbox security, and tool schema regressions.

Updated embedded client E2E removal/configuration slice:

```text
9 passed in 0.33s
```

Additional checks:

```text
Ruff: All checks passed for every modified Python file and the new bootstrap package.
OpenAPI: /api/agents, /api/skills, /api/mcp/config, and /api/features absent.
compileall: app and packages/harness/deerflow passed.
git diff --check: passed.
```

## Self-review

- Manifest parsing forbids unknown keys, duplicate source keys/kind-slugs, unsafe relative paths, symlinks, non-regular files, and digest mismatch before payload parsing.
- Bootstrap locks the singleton catalog state and uses one caller-owned session transaction; no shared-asset service commit is called. A conflict rolls back the principal and every asset written earlier in the transaction.
- Existing canonical rows must match deterministic identity, published version, and payload checksum; source-key conflicts fail closed.
- The builtin principal has no password, OAuth identity, project membership, credential, or binding and is rejected by `resolve_asset_actor()`.
- Runtime source scans found no production references to the removed storage factories, legacy category, installer, `skill_manage` tool, cutover provider method/constants, or removed router mounts.
- The two remaining `ExtensionsConfig.from_file()` calls are confined to the legacy configuration module's own cached loader/reloader definitions; Task 2 runtime consumers no longer call them. Final deletion of that configuration module belongs to the later M7 configuration cleanup task.
- No claim is made that the full backend suite or later M7 task gates pass; tests whose only subject was the deleted archive/file-backed surface were removed or changed to assert stable fail-closed errors.

## Concerns

- Task 2 intentionally leaves later M7 cleanup work (including final configuration/module deletion and baseline migration collapse) untouched.
- The complete backend suite was not run; verification used the task's mandatory PostgreSQL gate plus directly affected adjacent unit/E2E slices.

## Independent review repair (2026-07-18)

### Status

PASS — the five Important and one Minor independent-review findings were repaired on the Task 2 branch. This remains a Task 2-only result: it does not start M7 Tasks 3-11, claim the M7 milestone complete, or claim release readiness.

### Findings closed

- GitHub's ownerless webhook path now returns an empty authoritative registry and performs no global, per-user, or legacy filesystem Agent discovery. A real `fanout_event()` regression test forbids `Path.exists()`, `Path.iterdir()`, `Path.stat()`, and the legacy `load_agent_config()` entry point.
- Existing bootstrap rows are validated as the complete canonical Agent/Skill/MCP graph: parent identity and metadata, version metadata, Skill file bytes and identity, ordered Agent Skill/MCP references, and MCP credential slots. The packaged catalog now contains one canonical unbound MCP definition so every graph type is exercised.
- The builtin principal is rejected when referenced from every credential actor field and from creator/updater fields on Agent, Skill, or MCP project bindings.
- Obsolete tests were deleted or ported to exact admitted runtime Skills. The full backend collection and exact static scan no longer encounter removed storage factories, `SkillCategory.LEGACY`, or `user_should_see_legacy_skills`.
- A real `_make_lead_agent(config, app_config=..., private_runtime=...)` contract test proves exact model, soul, tool group, builtin/MCP tools, Skill mount/read-only state, and subagent disabling while legacy/global fallbacks are fail-fast.
- Exact admitted Skills render as `[run exact, read-only]`, and `runtime_read_only` participates in the prompt cache signature.

### Repair TDD evidence

Focused RED evidence before the production repairs:

```text
GitHub ownerless fan-out: 1 failed
E   AssertionError: GitHub webhook fan-out must not inspect legacy agent files

Canonical bootstrap graph: 7 failed
The previous implementation accepted changed/missing child graph rows and had no canonical MCP graph.

Builtin-principal authority references: 13 failed, 21 deselected
E   Failed: DID NOT RAISE <class 'app.shared_assets.bootstrap.service.BootstrapConflict'>

Full backend collection: 8600 tests collected, 3 errors
The errors were obsolete imports in test_remote_sandbox_backend.py,
test_runtime_paths.py, and test_skills_loader.py.

Exact Skill prompt/cache behavior: 2 failed, 2 passed
The exact Skill lacked the read-only label and the old cache renderer unpacked a four-field signature.
```

The exact private lead-agent wiring characterization test passed immediately against the Task 2 implementation, so it exposed no production wiring defect; it was retained as the requested security-sensitive regression contract.

Focused GREEN evidence after repair:

```text
GitHub registry/dispatcher/real fan-out: 4 passed in 0.24s
Canonical bootstrap graph slice: 7 passed, 14 deselected
Builtin-principal authority slice: 13 passed, 21 deselected
Migrated exact-runtime/residue suite: 259 passed in 1.74s
Exact lead-agent/prompt gate: 43 passed in 0.67s
Full backend collection: 8623 tests collected in 3.15s, 0 errors
```

### Final repair verification

Required randomized-PostgreSQL affected gate:

```text
POSTGRES_TEST_URL=postgresql+asyncpg://jiangfeng@127.0.0.1:55437/postgres \
PYTHONPATH=packages/harness .venv/bin/pytest \
  tests/test_m7_asset_bootstrap_postgres.py \
  tests/test_asset_catalog_provider.py \
  tests/test_private_asset_runtime.py \
  tests/integration/test_m3_asset_resolution_postgres.py \
  tests/test_harness_boundary.py -q --tb=short -rs

82 passed in 19.37s
```

PostgreSQL skips: **0**. The gate used only randomized `deerflow_test_*` databases through the designated disposable PostgreSQL listener.

Additional final gates:

```text
All modified non-PostgreSQL test files: 306 passed in 1.90s
Client/ACP/sandbox adjacency: 325 passed, 11 skipped in 1.79s
Updated client E2E selected slice: 13 passed, 1 skipped, 23 deselected in 0.30s
Full backend collection: 8623 tests collected in 3.15s, 0 errors
Ruff: All checks passed for all 19 modified Python files
Ruff format: 19 files already formatted
compileall: app and packages/harness/deerflow passed
OpenAPI: removed Agent/Skill/MCP config/Features routes absent
Removed-symbol static scan: zero hits
GitHub filesystem-authority static scan: zero hits
git diff --check: passed
```

The 11 adjacent skips are all external-LLM E2E cases whose API key is not configured; they are outside the required PostgreSQL gate, which had zero skips.

### Repair self-review and remaining concerns

- The existing-row bootstrap path compares every non-generated canonical parent/version/child field; database timestamps are intentionally not canonical payload data.
- Ownerless GitHub delivery intentionally produces no Agent matches until a later scoped design can resolve an authenticated PostgreSQL project binding. It never falls back to disk.
- Deleted tests only exercised APIs removed by Task 2; mixed suites were ported to exact runtime snapshots and remain collected.
- The branch still preserves later M7 Tasks 3-11 and the 6/8 milestone boundary unchanged.
- The complete backend test suite was not executed; full collection was executed to prove zero collection errors, alongside the complete Task 2 PostgreSQL gate and affected adjacency gates above.

## Final MCP slot-schema repair (2026-07-18)

### Status

PASS — the packaged MCP credential slot now uses DeerFlow's authoritative domain schema, and bootstrap canonical graph comparison preserves JSON scalar types. This repair changes only the final Task 2 MCP/bootstrap finding.

### TDD evidence

Focused real-PostgreSQL RED before the production repair:

```text
3 failed, 2 passed, 34 deselected in 0.82s

Provider/McpService reconstruction:
TypeError: 'bool' object is not iterable
  at McpService._definition_from_record()
  while reconstructing tuple(values) from the packaged slot schema

Strict JSON equality:
False versus 0: incorrectly matched
1 versus 1.0: incorrectly matched
List order and mapping-key-order control cases behaved as expected.
```

Focused GREEN after replacing the general JSON Schema with the domain slot schema and adding recursive JSON comparison:

```text
5 passed, 34 deselected in 0.75s
```

The packaged slot schema is now exactly:

```json
{"headers":["X-DEERFLOW-DOCS-KEY"]}
```

It contains only a header name, no secret value. The real PostgreSQL regression test proves bootstrap initially creates zero credentials, grants, or project bindings; the test then creates a normal project binding solely to exercise the actual `BindingService` and `ProjectAssetResolver` path, whose uncredentialed optional slot materializes to an empty mapping.

An intermediate complete gate exposed that applying Python concrete-type identity to non-JSON database values also rejected the driver's UUID representation (`1 failed, 86 passed`). The comparator was narrowed to JSON Mapping, list, and numeric/bool scalar semantics. Its final contract proves `false != 0`, `1 != 1.0`, list order is exact, and mapping key order is irrelevant without changing non-JSON row identity comparison.

### Final verification

```text
Focused bootstrap idempotency, Provider/Resolver, and JSON comparison:
6 passed, 33 deselected in 1.07s

Focused Provider/Resolver regression:
1 passed in 0.71s

Full Task 2 PostgreSQL gate after final formatting:
87 passed in 19.23s
PostgreSQL skips: 0

Ruff: All checks passed
Ruff format: 2 files already formatted
compileall: app and packages/harness/deerflow passed
Removed-symbol static scan: zero hits
GitHub filesystem-authority static scan: zero hits
git diff --check: passed
```

The MCP payload digest recorded in `catalog.json` is `8b5d24e0e23150cf43ad66d40d46751b3492acc95d6e112c1fa7564e3fa37deb`.
