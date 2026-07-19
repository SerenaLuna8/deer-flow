# M7 Task 11 independent-review repair report

- Branch: `codex/m7-legacy-cleanup`
- Implementation base: `70a36a5c`
- Reviewed candidate: `fd2550f49240029ae6897252b83ca603e529cc6a`
- Exact independent-review range: `00f7ae3ca0a15e069a03fd19bc009ca0c53d1b2a..fd2550f49240029ae6897252b83ca603e529cc6a`
- First review verdict: `0 Critical / 4 Important / 1 Minor`
- First repair commit: `744e7b8725dfc38ff5f256b6af1075dbd0a5e246`
- Second frozen rereview verdict at `744e7b87`: `0 Critical / 3 Important / 0 Minor`
- Current status: all three second-rereview Important findings repaired and full gates rerun; second repair commit pending
- M7/M8 status: this repair does not mark M7 complete, does not change the milestone ledger, and does not start M8

## Review repairs

1. **I1 — global/filesystem Memory removal**
   - Deleted `MemoryStorage`, `FileMemoryStorage`, global Memory queue/singletons, global updater CRUD, filesystem Memory path helpers, sample loader, config fields and Helm values.
   - Sync/unscoped middleware and prompt paths fail closed; project PostgreSQL Memory storage, queue and updater remain.
   - Embedded client global Memory methods are now physically absent; project PostgreSQL Memory APIs remain the only management surface.
2. **I2 — TUI/Thread/Gateway compatibility removal**
   - Removed in-memory ThreadMeta implementation, trusted unscoped adapter, Gateway authz compatibility module and dependency getters.
   - SQL ThreadMeta and TUI metadata writes require exact `PrivateResourceScope`; embedded client Thread operations require an explicit checkpointer and never fall back to the global provider.
3. **I3 — retired frontend error codes**
   - Removed Automation/reliability cutover codes and added strict response rejection tests.
4. **I4 — active documentation inventory and link gate**
   - Active inventory covers every current root/module, `.github`, `skills/public`, backend docs, deploy/docker/docs and frontend content document; history exclusions are exact paths/prefixes.
   - Every local link and frontend docs route resolves to a real file; final result is `110 active / 0 broken links / 0 legacy residues`.
   - Root now contains one Chinese `README.md`; root translation README files were deleted and active references removed.
5. **M1 — review range reporting**
   - This report records the exact full range above rather than a shortened or candidate-only range.

## TDD evidence

### RED

- Combined review-repair source tests: `5 failed / 38 passed / 1 skipped` before implementation.
- Frontend retired-code tests: `3 failed / 23 passed` before implementation.
- Documentation inventory: `49 broken links / 212 legacy residues`, later reduced through incremental checkpoints before GREEN.

### Affected GREEN

- I1 focused backend: `122 passed / 3 skipped`.
- I2 focused backend: `55 passed / 50 PostgreSQL-dependent skipped` before the real PostgreSQL gate.
- I3 focused frontend: `26 passed / 0 skipped`.
- I4 source/docs gate: `34 passed`; inventory `110 active / 0 links / 0 residues`.
- Old full-suite assertions updated to the final fail-closed contract: `259 passed / 11 skipped` focused.

## Full verification after repair

- Fixed 22-file M1–M7 PostgreSQL gate: `282 passed / 0 skipped` in 115.05 seconds.
- Backend full suite: `6677 passed / 936 skipped / 0 failed` in 89.21 seconds.
- Backend blocking-I/O: `27 passed`.
- Ruff: `1037 files already formatted`; `All checks passed!`.
- Frontend unit: 122 files, `891 passed / 0 skipped / 0 failed`.
- Frontend `pnpm check`: PASS.
- Frontend production Playwright: `14 passed`; static Playwright: `2 passed`.
- Production and static builds: PASS, each generating 80 pages.
- Fresh `0001_project_saas_baseline` setup plus `make check-db`: healthy, exact head.
- `make doctor`: Ready with two expected optional warnings; `make help`: PASS.
- Support bundle: `triage.json` status `ok`, doctor included, redacted secret fields true, and zero matches for the dummy key, PostgreSQL URL or removed Memory/config/API surfaces.
- `git diff --check`: PASS.

The first PostgreSQL command used an incorrect explicit `postgresql+psycopg` driver and therefore produced isolated-database setup errors. The release probe and full gate were rerun with the contract URL `postgresql://...`, which the test helper converts to asyncpg; both passed. This was an execution-environment error, not a product failure.

Cleanup completed: the disposable database was dropped, PostgreSQL stopped, port 55448 closed, and the exact temporary cluster, ignored config/env files and support artifacts were removed.

## Second frozen rereview repair

The second rereview of `744e7b87` found three remaining Important issues. This repair stays inside the frozen Task 11 boundary and does not update the milestone ledger.

1. **I1 — embedded client and TUI global compatibility surface**
   - Physically deleted all 16 global Skill/MCP/Memory methods from `DeerFlowClient`; an exists-but-raises implementation now fails the source mutation gate.
   - Removed TUI `/skills`, `/mcp` and `/memory`, startup global Skill discovery, dynamic Skill palette entries/activation, handlers and callers.
   - Deleted the reverse compatibility tests and retained embedded chat/stream/models, explicit checkpointer, local upload and artifact behavior.
2. **I2 — Gateway in-process RunManager cancellation chain**
   - Deleted `get_run_manager`, `app.state.run_manager`, router notifiers and `notify_local_cancellation`.
   - Removed optional membership/lifecycle notifier branches while preserving the authoritative in-transaction database authorization marker, retention, quota and audit operations.
   - Added structural and mutation gates for every removed chain shape.
3. **I4 — Helm and repository-wide residue**
   - Deleted the extensions ConfigMap/value/env/mount/checksum and the complete chart-managed Redis stream stack.
   - Reduced Helm Nginx to one final generic `/api/` location; removed the `/api/langgraph` rewrite and all explicit legacy global API locations.
   - Deleted `skills/public/claude-to-deerflow`, updated `.github` guidance, and changed README Memory semantics to authenticated project/owner PostgreSQL storage.
   - Removed the four deleted translation README filename literals from every tracked text file. The active inventory now reports `179 documents / 0 broken links / 0 legacy residues`.

### Second repair TDD and verification

- RED: `10 failed / 37 passed`, classified as I1 client/TUI (2), I2 RunManager chain (1), and I4 Helm/docs/residue (7).
- I1 client/E2E/TUI/source focused: `206 passed / 11 skipped`.
- I2 membership/lifecycle/router/authorization focused: `89 passed / 13 deselected`.
- Project Memory repository/service/prompt/queue/router regression: `104 passed / 10 skipped`.
- M7 source/docs/Helm gate: `49 passed`; `helm lint` reported 1 chart and 0 failures; rendered removed-surface matches: 0.
- Sole ordered PostgreSQL release inventory: exactly 22 files from `make print-project-foundation-postgres-tests`.
- Fixed 22-file M1–M7 PostgreSQL gate: `297 passed / 0 skipped` in 110.17 seconds.
- Backend full suite: `6659 passed / 936 skipped / 0 failed` in 88.73 seconds.
- Backend blocking-I/O: `27 passed`; Ruff format: `1037 files already formatted`; Ruff check: PASS.
- Frontend unit: 122 files, `891 passed / 0 skipped / 0 failed`; `pnpm check`: PASS.
- Frontend production Playwright: `14 passed`; static Playwright: `2 passed`; production/static builds: PASS, 80 pages each.
- Helm source/render extensions, Redis and legacy-route matches: 0; Nginx API locations: exactly one generic `/api/`.
- Tracked deleted translation README filename literals: 0; root translation README files remain absent; root Chinese `README.md` remains the sole README.
- Fresh `0001_project_saas_baseline` setup plus doctor: `Ready` with two expected optional warnings; `make help`: PASS.
- Support bundle: triage status `ok`, doctor included, secret-field redaction true, and zero PostgreSQL DSN or removed-surface matches.
- `git diff --check`: PASS.

The frontend unit command first failed only because the sandbox denied a temporary `::1:3000` listener (`EPERM`). The same unchanged command passed 891/891 when loopback listening was authorized; this was an execution-environment restriction, not a product failure.

Second-gate cleanup completed: the disposable cluster used explicit superuser `postgres`, bound only `127.0.0.1:55463`, passed a create/query/drop probe, and contained zero `deerflow_test_*`/`deerflow_restore_*` databases after the gate. It was then stopped; the exact cluster/cache directories were removed and port 55463 was confirmed closed.

Doctor/support used a separate disposable baseline database bound only to `127.0.0.1:55464`. It also had zero random test/restore databases before shutdown. Port 55464, its exact cluster/cache, generated ignored config/env files, support zip and both support sidecars were removed after verification.

## Handoff

The second repair commit must include no change to `.superpowers/sdd/progress.md`, no `Completed` M7 status, no 7/8 ledger entry and no M8 work. Parent should use the new repair commit as the next review candidate and retain both review checkpoints recorded above.
