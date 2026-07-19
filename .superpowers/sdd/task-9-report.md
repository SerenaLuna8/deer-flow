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

## Independent review repair

The first bounded repair review addressed exactly four frozen findings:

- **I1 — exported-snapshot TOCTOU:** three real-PostgreSQL RED cases injected an
  owned sequence, a LangGraph index, or a function after the outer precheck and
  all three still reached the mocked `pg_dump` spawn. The exported snapshot now
  checks the exact root inventory, revision, and canonical catalog through the
  same asyncpg connection and already-open repeatable-read transaction; the
  three cases are GREEN and never spawn.
- **I2 — genuine old archive:** an exact authenticated old manifest and its
  old-AAD ciphertext now reaches `UNSUPPORTED_ARCHIVE_SCHEMA` before target
  parsing. HMAC validation still happens first, so a bad old-shape signature is
  `BACKUP_AUTHENTICATION_FAILED`, while a malformed version-7 manifest cannot
  use unsupported-schema classification as an authentication oracle.
- **I3 — strict manifest contract:** writer, reader, restore parser, public
  construction, and generated JSON schema now share one frozen strict Pydantic
  model. Unknown fields, scalar coercion, wrong version/revision/digest, invalid
  chunk bounds, and mutation are rejected. The combined I2/I3 RED set was eight
  failures and is now eight passes.
- **I4 — proof propagation:** `restore_proofs` now persists non-null archive
  schema version and canonical digest. Append-only triggers protect both fields.
  The two real-PostgreSQL RED cases are GREEN, including a live restore ORM read
  and invalid-insert/update rejection.

The repair changed the canonical baseline signature intentionally. An
independent metadata database read confirmed 615 columns with digest
`8bb79d7b08cd7404a9e459a42ebb56471d57888310c965f143d40cd553ecd5b4`
and 383 constraints with the first-review digest recorded at that checkpoint;
all other signature dimensions remain unchanged. A direct catalog audit also
confirmed `archive_schema_version smallint NOT NULL`, `schema_digest char(64)
NOT NULL`, and the initial version/revision/lower-hex proof constraint. The
second review below supersedes that constraint.

## Second bounded repair review

The sole follow-up Important found that the proof constraint accepted any
lowercase 64-hex digest. A real PostgreSQL RED inserted version `7`, revision
`0001_project_saas_baseline`, and `f` repeated 64 times without error. The
static baseline and ORM constraint now require the one canonical digest
exactly, so that insert is rejected by PostgreSQL.

The digest's constraint literal would otherwise make the catalog signature
self-referential. The release signature therefore normalizes exactly one full
constraint rendering containing the current canonical digest to the fixed
`__M7_CANONICAL_SCHEMA_DIGEST__` placeholder. It never replaces arbitrary
64-hex text: changing only the constraint literal to another valid digest makes
all final-schema entry points fail closed without mutating that database.

Two-stage generation converged independently:

- placeholder-normalized constraints: count `383`, digest
  `41ad7d27b84de8b3818c9753386561ba321e7909812078fccea8e6a11def2508`;
- final schema digest:
  `75a88f91b80d3043c94c669e44b84975ad4e2bf5fa532ed45c8936de723244f5`.

A second fresh static-baseline database produced those same values, and the
import-time invariant plus unit test require the exported canonical digest to
equal the hash of `FINAL_M7_CATALOG_SIGNATURE`.

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

- Final Task 8 plus repaired Task 9 PostgreSQL regression: `51 passed` (`26` + `25`).
- Fixed 16-file project-foundation PostgreSQL release gate: `157 passed`, `0 skipped`.
- Recovery gate (`test_m7_backup_restore_postgres.py`, backup archive, restore PostgreSQL, restore safety, retention purge, tombstone journal): `145 passed`, zero skips.
- Fresh standalone blocking-I/O gate: `4 passed`.
- Focused live restore proof: `1 passed`.
- Ruff format check: `6 files already formatted`.
- Ruff lint: `All checks passed!`.
- Full backend collection: `7786 tests collected`, zero collection errors.
- Production residue scan for `PRE_M6`, `pre[_-]cutover`, revision `0013`, and revision `0015`: no matches.
- `git diff --check`: clean.
- Isolated repair PostgreSQL cleanup: no `deerflow_test_*`/`deerflow_restore_*`/`deerflow_autogen_*` databases remained; the port-55440 cluster was stopped, its exact temporary data directory and diagnostic file were removed, and `pg_isready` returned `no response`.
- Second-review PostgreSQL cleanup: the only explicit two-stage generation database was dropped after all random fixtures had self-cleaned; the port-55441 cluster was stopped, its exact temporary directory was removed, and `pg_isready` returned `no response`.

## Documentation

Updated the recovery runbook, root/backend agent guides, and README for archive schema version 7, canonical digest/AAD binding, exact M7 source/target verification, unsupported pre-M7 behavior, and public CLI output.

## Scope boundary

This report covers Task 9 only. Task 10 has not been started, and `.superpowers/sdd/progress.md` was not modified.
