# M4 Task 7 PostgreSQL File Authority Report

## Scope and outcome

Task 7 establishes PostgreSQL `files`, `file_chunks`, and `artifacts` as the
authoritative private-file primitives for future project routes. It adds the
scoped repository, streaming upload/finalize lifecycle, conversion staging,
bounded download/artifact streaming, safe response headers, and the minimal
shared upload contract extraction required by Step 6.

This task deliberately does **not** mount Task 11 project HTTP routes, connect
Task 8 sandbox restore/finalizers, or replace the existing legacy
`/api/threads/{thread_id}/uploads` and `/api/artifacts` host-directory flows.

## Schema and repository authority

- Alembic revision `0010_private_file_source` extends the Task 1 final schema
  without rewriting revisions `0008` or `0009`.
- `files.source_file_id` is a nullable same-scope/same-Thread composite
  self-reference. It is allowed only for `kind='workspace'`, must be non-self,
  and references a ready source through repository validation.
- Chunk constraints enforce `size = octet_length(content)` and
  `0 < size <= 1 MiB`; empty files are represented by zero chunks.
- Every repository query binds `project_id + owner_user_id + thread_id`.
  Staging locks the active scoped Thread; append/finalize enforce contiguous
  chunk indexes, per-chunk size/hash, and whole-file size/hash.
- Ready bytes and metadata are immutable. Soft deletion keeps chunks and allows
  a later same-path version. List ordering is stable keyset pagination by
  `(logical_path, version, id)` across all ready kinds.
- Ready reads, chunk pages, artifacts, and listing join an active Thread, so a
  deleted or frozen Thread hides bytes immediately even before Task 8 adds
  cross-resource marking.

## Service transaction and failure boundary

- HTTP bodies are never awaited while a database transaction is open. The
  service stages known file IDs in a short transaction, persists exact 1 MiB
  chunks in short revalidating transactions, and finalizes a multi-file batch
  atomically.
- The finalization transaction is the authoritative commit point. Cancellation
  before it triggers shielded exact-ID cleanup; cancellation racing the commit
  is deferred until the committed result is known.
- Count/single/total defaults are independently fixed at
  `10/100 MiB/100 MiB` for private files and `10/50 MiB/100 MiB` for legacy
  uploads. Config overrides and invalid-value fallback use a pure shared
  resolver without making project code call host-directory helpers.
- A body `AsyncIterable` ordinary exception is sanitized to
  `PRIVATE_WORK_UNAVAILABLE` only after exact staging cleanup. Existing
  `PrivateWorkError` semantics are preserved, while cancellation and other
  `BaseException` control flow are cleaned and re-raised unchanged.
- Listing and soft deletion require `private_work.read_own`. A Viewer can list,
  download, and delete their own ready file, but cannot upload or convert.
  Missing/inactive/cross-scope/not-ready resources remain indistinguishable 404s.

## Conversion and streaming hardening

- Conversion revalidates `private_work.create` before creating a temporary
  directory or worker. Directories are mode `0700` and sources mode `0600`.
- Blocking resource operations are joined before cancellation cleanup. Temp
  directory creation cleans up if chmod fails; close and recursive cleanup
  failures are observable in server logs rather than silently ignored.
- Converter output is opened through anchored directory FDs with no-follow,
  directory, close-on-exec, and nonblocking flags. The verified object must be a
  single-link regular file, and the same FD is streamed into PostgreSQL. This
  rejects final/ancestor symlinks, hardlinks, FIFOs, escapes, and path-swap TOCTOU.
- Download uses short sessions and bounded keyset chunk pages. It validates the
  complete first page before returning a stream, revalidates authorization and
  active Thread state per page, and maps chunk/whole-file tampering to a stable
  unavailable error without exposing logical or host paths.
- Logical paths are NFC-normalized POSIX paths and reject absolute/drive,
  backslash, dot/empty segments, control/format characters, and oversized
  values. MIME input uses strict bounded ASCII type/subtype/parameter syntax.
- Filenames remove all Unicode category-C characters and use RFC 5987 encoding
  with `X-Content-Type-Options: nosniff`. HTML/XHTML/SVG,
  JavaScript/ECMAScript, XML, and PDF are forced to attachment after MIME
  normalization. Artifact metadata cannot downgrade an active file MIME to inline.

## Step 6 legacy compatibility

`app.upload_contracts` is a pure schema/limit module with no authorization or
host I/O. It owns caller-supplied `UploadLimitDefaults`, the shared positive
limit resolver, and the existing legacy `UploadedFileInfo`, `UploadResponse`,
`UploadListResponse`, and `UploadLimits` models. The legacy router explicitly
re-exports those names, preserving imports, OpenAPI, and runtime behavior. The
legacy schema's host `path` field is documented as legacy-only and is not reused
as a future project/private response.

## TDD and review evidence

Representative initial RED evidence included missing private file modules,
missing revision/source schema, active-Thread visibility gaps, strict MIME
acceptance gaps, conversion cancellation/FD safety gaps, and list/delete
contract gaps. The final review wave also reproduced the temp chmod leak:

```text
test_conversion_dir_chmod_failure_removes_created_directory: FAILED
left deerflow-private-convert-* directory after chmod failure
```

The shared contract extraction began with four expected import failures for the
missing `app.upload_contracts` module. After implementation:

```text
# Shared contract + legacy upload exact
43 passed, 0 failed

# Sensitive body RuntimeError after one persisted 1 MiB chunk, real PostgreSQL
1 passed, exact file/chunk cleanup and fixed public error

# Task 7 repository/service/streaming + shared contract focused
94 passed, 0 failed, 0 skipped

# Task 7 full file/blocking-I/O + legacy upload/artifact
146 passed, 0 failed, 0 skipped
```

The independent static reviewer reran the non-PostgreSQL path/MIME/header/temp,
shared-contract, and legacy router matrix and reported `102 passed`; the final
code review before commit reported **0 Critical / 0 Important / 0 Minor**.

## Release-gate evidence

All PostgreSQL tests used the disposable instance on port `55479`; fixtures
created only generated `deerflow_test_*` databases.

```text
# M4/M3 schema, migration environment, and default bootstrap
78 passed, 0 failed, 0 skipped

# Task 2-6 context/auth/import/Thread/Run/governance regression
185 passed, 0 failed, 0 skipped

# Task 4 Run/Event/Snapshot + M3 shared asset wider regression
172 passed, 0 failed, 0 skipped

# Mandatory staged runtime audit
123 passed, 6 declared Task 11 failures, 0 skipped
```

The six mandatory-audit failures are the predeclared Task 11 staged cases. Each
still stops at legacy `POST /api/threads` with intentional
`409 PRIVATE_WORK_CUTOVER`; none enters Task 7 logic:

1. `test_stream_run_completes_and_persists_runtime_state`
2. `test_stream_run_executes_real_lead_agent_setup_agent_business_path`
3. `test_cancel_interrupt_stops_running_background_run`
4. `test_cancel_interrupt_generates_missing_title_from_checkpoint`
5. `test_cancel_wait_false_generates_title_from_graph_input_before_checkpoint`
6. `test_cancel_rollback_restores_pre_run_checkpoint`

## Quality gates

```text
ruff check: All checks passed (19 changed Python files)
ruff format --check: 19 files already formatted
python -m compileall app/private_work app/gateway packages/harness/deerflow: passed
git diff --check: passed
```

Task 7 architecture, limits, transaction boundaries, MIME/header rules, and
Task 8/11 ownership boundaries are recorded in `backend/AGENTS.md`.
