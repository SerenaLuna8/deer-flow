# Task 9 Report: M7-only backup and restore

## Outcome

Task 9 replaces recovery compatibility branches with one authenticated M7 archive contract:

- archive schema version is fixed at `7`;
- schema revision is fixed at `0001_project_saas_baseline`;
- the manifest and per-chunk AAD carry the canonical M7 catalog digest;
- backup rejects any noncanonical source before invoking `pg_dump` and verifies the catalog again in the exported snapshot;
- restore authenticates the complete archive, then rejects pre-M7/unknown contracts as `UNSUPPORTED_ARCHIVE_SCHEMA` before target parsing, lookup, creation, or DDL;
- restore verifies the source before mutation and the restored target before proof;
- the pre-M6 cutover producer, receipt path, tests, and CLI option are deleted.

Journal-first purge, source/journal anchors, continuous tombstone replay, proof binding, inode-safe cleanup/fsync, and drill ownership handoff remain in the existing recovery workflow.

## TDD evidence

Initial RED run for the first archive contract tests produced `3 failed`. After the initial implementation it became `3 passed`. A second RED test proved a noncanonical source still reached the mocked `pg_dump` spawn (`5 passed, 1 failed`); adding the exact M7 source verifier made the focused file green.

The final Task 9 test file covers:

- fixed version/revision/digest manifest fields;
- AAD binding of version, revision, digest, source identity, and chunk index;
- re-signed manifest tampering of version, revision, or digest with unchanged ciphertext;
- unknown root objects and a wrong Alembic head before `pg_dump` spawn;
- authenticated pre-M7 rejection before any target operation;
- restore-stable canonical schema hashing plus near-miss drift rejection.

## Restore-stable catalog contract

Real PostgreSQL 14 `pg_dump`/`pg_restore` changes only the presentation of varchar status arrays in check constraints and two partial indexes:

```text
ARRAY['queued'::character varying, 'running'::character varying]::text[]
->
ARRAY['queued'::character varying::text, 'running'::character varying::text]
```

The shared Task 8/Task 9 verifier normalizes only a complete array of string literals with those exact varchar/text casts. It does not perform a global cast replacement. Negative digest tests prove that changing an element, the element type, the `ANY` operator, or the left-side predicate still produces a mismatch. A real fresh baseline and a real restored target now produce the same canonical signature while Task 8 drift cases remain fail-closed.

## Security and failure boundaries

- A fully authenticated unsupported archive reports the stable `UNSUPPORTED_ARCHIVE_SCHEMA` code; an attacker who edits and re-signs only schema fields still fails AEAD chunk authentication.
- Source exactness is checked before libpq passfile acquisition/version probing/`pg_dump`, then app catalog definitions are checked again inside the exported repeatable-read snapshot.
- Target name validation and existence checks occur only after full archive authentication and M7 support validation.
- The post-restore exact M7 verifier runs before recovery proof creation. Runtime schema drift deletes the invocation-owned target and writes no proof.
- Body failure, cancellation during authority release, unknown-workspace-file handling, journal gap/tamper, frozen-head proof binding, and random-drill cleanup remain covered by the recovery gates.

## Verification evidence

- Task 8 exact baseline plus live restore: `27 passed` (`26` Task 8 tests plus one live restore).
- Fixed 16-file project-foundation PostgreSQL release gate: `157 passed`, `0 skipped`.
- Recovery gate (`test_m7_backup_restore_postgres.py`, backup archive, restore PostgreSQL, restore safety, retention purge, tombstone journal): `133 passed`, zero skips.
- Fresh standalone blocking-I/O gate: `4 passed`.
- Focused runtime-schema mismatch and successful random drill: `2 passed`.
- Ruff format check: `19 files already formatted`.
- Ruff lint: `All checks passed!`.
- Full backend collection: `7774 tests collected`, zero collection errors.
- Production residue scan for `PRE_M6`, `pre[_-]cutover`, revision `0013`, and revision `0015`: no matches.
- `git diff --check`: clean.
- Isolated PostgreSQL cleanup: no `deerflow_test_*`/`deerflow_restore_*`/`deerflow_autogen_*` databases remained; the port-55439 cluster was stopped, its exact temporary data directory and diagnostic files were removed, and `pg_isready` returned `no response`.

## Documentation

Updated the recovery runbook, root/backend agent guides, and README for archive schema version 7, canonical digest/AAD binding, exact M7 source/target verification, unsupported pre-M7 behavior, and public CLI output.

## Scope boundary

This report covers Task 9 only. Task 10 has not been started, and `.superpowers/sdd/progress.md` was not modified.
