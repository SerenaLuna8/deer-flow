# M7 Task 11 independent-review repair report

- Branch: `codex/m7-legacy-cleanup`
- Implementation base: `70a36a5c`
- Reviewed candidate: `fd2550f49240029ae6897252b83ca603e529cc6a`
- Exact independent-review range: `00f7ae3ca0a15e069a03fd19bc009ca0c53d1b2a..fd2550f49240029ae6897252b83ca603e529cc6a`
- Review verdict: `0 Critical / 4 Important / 1 Minor`
- Current status: all four Important findings repaired and full gates rerun; repair commit pending
- M7/M8 status: this repair does not mark M7 complete, does not change the milestone ledger, and does not start M8

## Review repairs

1. **I1 — global/filesystem Memory removal**
   - Deleted `MemoryStorage`, `FileMemoryStorage`, global Memory queue/singletons, global updater CRUD, filesystem Memory path helpers, sample loader, config fields and Helm values.
   - Sync/unscoped middleware and prompt paths fail closed; project PostgreSQL Memory storage, queue and updater remain.
   - Embedded client global Memory methods now reject with `AssetCatalogUnavailable` and point to authenticated project APIs.
2. **I2 — TUI/Thread/Gateway compatibility removal**
   - Removed in-memory ThreadMeta implementation, trusted unscoped adapter, Gateway authz compatibility module and dependency getters.
   - SQL ThreadMeta and TUI metadata writes require exact `PrivateResourceScope`; embedded client Thread operations require an explicit checkpointer and never fall back to the global provider.
3. **I3 — retired frontend error codes**
   - Removed Automation/reliability cutover codes and added strict response rejection tests.
4. **I4 — active documentation inventory and link gate**
   - Active inventory covers root docs, backend docs, deploy/docker/docs and frontend content; history exclusions are exact paths/prefixes.
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

## Handoff

The repair commit must include no change to `.superpowers/sdd/progress.md`, no `Completed` M7 status, no 7/8 ledger entry and no M8 work. Parent should use the repair commit as the next review candidate and retain the exact reviewed range recorded above as the source independent-review range.
