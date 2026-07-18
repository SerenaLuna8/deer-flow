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
