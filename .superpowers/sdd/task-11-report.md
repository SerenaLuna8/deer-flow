# M7 Task 11 closure candidate report

- Branch: `codex/m7-legacy-cleanup`
- Task 11 base: `e01f9b81`
- Full M7 base: `70a36a5c`
- Candidate status: implementation and mandatory gates complete; final independent review pending
- M8 status: pending; no release-ready claim

## Scope

- Synchronized root/backend/frontend AGENTS, English/Chinese README, CHANGELOG,
  master design, M7 design/plan, and M7 backup/recovery runbook with the final
  fresh-install/project/admin/Gateway/Worker/Scheduler/version-7 recovery model.
- The three obsolete staged-operation runbooks were already absent at Task 11 base
  `e01f9b81`; `docs/operations/` contains only the M7 backup/recovery runbook.
- Extended `backend/tests/test_m7_source_absence.py` with relative Markdown link
  verification and active operational documentation residue checks. External URLs,
  site-absolute paths, and document-local anchors are deliberately ignored.
- Recorded Tasks 1–10 implementation commits, reviewed ranges, and per-task verdicts
  in the M7 design. The Task 11/full-branch verdict, `Completed` state, and master
  7/8 ledger are intentionally reserved for the final independent reviewer and parent.

## TDD evidence

### RED

```text
UV_CACHE_DIR=/private/tmp/m7-task11-uv-cache uv run pytest tests/test_m7_source_absence.py -q
5 failed, 26 passed
```

The new mutation tests failed because `_markdown_link_findings` and
`_active_doc_residue_findings` did not exist. After the smallest helper implementation,
the two repository-level contracts remained red with two broken local image links and
48 active-document residue findings, demonstrating that the gates detected real drift.

### GREEN checkpoint

```text
UV_CACHE_DIR=/private/tmp/m7-task11-uv-cache uv run pytest tests/test_m7_source_absence.py -q
31 passed in 2.57s
```

The mutation tests, production source absence, relative Markdown links, and active-doc
final-surface checks all passed at this checkpoint.

The final post-format/post-documentation rerun also passed:

```text
31 passed in 2.34s
1 file already formatted
All checks passed!
```

## Final verification evidence

All results below come from fresh Task 11 commands against the candidate diff.

- Fixed 22-file PostgreSQL gate: `279 passed`, `0 skipped`, `0 failed` in
  109.51 seconds. It used a disposable cluster on `127.0.0.1:55447` and only
  generated `deerflow_test_*` / `deerflow_restore_*` databases.
- Backend full suite: `6854 passed`, `944 skipped`, `0 failed` in 92.20 seconds.
  PostgreSQL-only tests are intentionally skipped here and are covered with zero
  skips by the fixed gate above.
- Backend blocking-I/O: `27 passed` in 10.86 seconds.
- Ruff: `1047 files already formatted`; `All checks passed!`.
- Frontend unit: 122 files, `888 passed`, `0 skipped`, `0 failed`.
- Frontend `pnpm check` and Prettier check: PASS.
- Frontend M7 production Playwright: `14 passed`; static Playwright: `2 passed`.
- `BUILD_MODE=production pnpm build` and `BUILD_MODE=static pnpm build`: PASS,
  each generating all 80 pages.
- Fresh database path: `make setup-db` installed only
  `0001_project_saas_baseline`; `make check-db` reported healthy and exact head.
- `make doctor`: Ready with two expected optional warnings after supplying a
  temporary, ignored default configuration, a non-networked dummy model key, and
  the disposable M7 database. `make help`: PASS.
- Support bundle: nine expected files; `triage.json` status `ok`; doctor embedded
  successfully; raw env/thread/user files absent; secret fields redacted. A unique
  secret probe, PostgreSQL URLs, removed extension config, removed root/config path
  overrides, and removed migration commands had zero bundle matches.
- Markdown/source consistency: final source/link/active-doc gate `31 passed`;
  specified active-doc legacy residue search had zero matches; `git diff --check`
  passed; `.superpowers/sdd/progress.md` remained untouched.

Two environment/test classifications were kept visible rather than treated as
product failures:

- The first sandboxed PostgreSQL invocation could not connect to the local test
  port and produced 160 setup errors before isolated database creation. The
  unchanged authorized command passed all 279 tests with zero skips.
- The first production Playwright run had one existing Automation dialog timeout
  (`13 passed`, `1 failed`). The isolated case passed, then the unchanged full
  14-test gate passed. Task 11 changed no frontend production source.

Resource cleanup completed: the disposable cluster contained zero remaining test
or restore databases before shutdown, port `55447` was closed, its exact temporary
directory and support-bundle artifacts were removed, and the temporary ignored
`.env`, `frontend/.env`, and `config.yaml` files were deleted.

## Review handoff

The independent reviewer must compare `70a36a5c..candidate HEAD` against the M7
design and implementation plan, then explicitly inspect deleted-surface absence,
project/owner authority, system-admin redaction, Gateway/Worker/Scheduler boundaries,
baseline equality, old-database fail-before-DDL, recovery invariants, frontend static
no-network behavior, and this documentation/link gate. No final M7 status or 7/8 ledger
entry is valid before that verdict and all required repairs are verified.
