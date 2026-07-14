# Task 4 review fixes

## Status

DONE. Review findings I1, I2, and I3 are closed without adding default authority,
a production fallback, SQLite, RLS, or Task 5+ wiring.

All PostgreSQL evidence below used the isolated PostgreSQL 14 cluster rooted at
`/tmp/deerflow-m4-task4-pg.4TpPaF` and the existing test fixture's random
`deerflow_test_*` databases. No business database was used.

## TDD evidence

### RED

- The unmodified I3 audit was reproduced as exactly `29 failed, 35 passed`:
  3 event-filter failures, 14 feedback failures, 3 token-usage failures, 6
  Run/Event/Feedback owner-isolation failures, 2 Task 3 Thread fixture failures,
  and 1 obsolete explicit-`None` global-bypass assertion.
- New real-PostgreSQL I1/I2 tests first produced `8 failed, 3 passed`. The failures
  proved that the local grant scan rejected a retired semantic version, accepted a
  missing active envelope, accepted scope and schema mismatches, exposed
  `request_id="unknown"`, and did not call the M3 batch lock primitive.

### GREEN

- Focused snapshot suite: `11 passed`.
- Snapshot plus session-bound private Run repository: `16 passed`.
- Exact Task 4 Step 6 gate: `154 passed, 1 warning`.
- Expanded I3 regression audit: `64 passed`.
- Task 1-3 schema/context/firewall/Thread/checkpointer regression gate:
  `260 passed, 1 warning`.
- Frozen M3 asset-resolution integration suite: `11 passed`.
- Staged Task 11 runtime lifecycle audit remains exactly
  `2 passed, 6 failed, 1 warning`; all six failures still stop at the intentional
  legacy Thread `409 PRIVATE_WORK_CUTOVER`. No authority was invented to pass it.

## I1: exact M3 credential closure

`RunSnapshotRepository` no longer has `_grant_rows()` or a parallel closure
implementation. Inside the existing Run snapshot transaction it constructs exact
`McpCredentialClosureTarget` values and calls
`lock_mcp_credential_closures(..., load_envelopes=False)` once for the whole MCP
set. Snapshot grant IDs come only from the returned locked materials.

This directly inherits M3's transaction-global slot -> logical credential ->
semantic version -> active envelope -> grant lock order and its validation of:

- active grants pinned to active or retired semantic versions;
- one active envelope without loading envelope bytes;
- exact MCP/credential scope and project equality;
- normalized slot/version payload-schema equality;
- required slots and stable grant references.

The new PostgreSQL tests cover retired-version acceptance, inactive envelope,
scope mismatch, schema mismatch, and concurrent repin/revoke serialization.
Concurrency tests use `asyncio.Event` barriers and bounded `asyncio.wait_for`; they
contain no sleeps and prove the mutation cannot commit while the snapshot owns the
closure locks. Snapshot rows retain the pre-mutation locked IDs.

## I2: stable public errors and real request IDs

Asset/version/checksum/dependency/credential-closure/catalog-generation failures
now use an internal stale marker and map at the `PrivateWorkContext` boundary to
`PRIVATE_WORK_ASSET_STALE`. Session-bound Run invariant failures use
`PrivateRunConflict`, which carries no fake request ID, and map at the same public
boundary to `PRIVATE_WORK_CONFLICT`. SQLAlchemy DB failures map to
`PRIVATE_WORK_UNAVAILABLE`.

Tests assert the exception code, fixed sanitized message, exact
`context.request_id`, and HTTP mapper payload for stale admission, true missing
Thread/run conflict, secret-bearing request conflict, and database unavailability.
No `PrivateWorkConflict("unknown")` remains in the Task 4 Run/snapshot path.

## I3: final-schema regression fixtures

The four review-listed regression files now seed real project/member/Agent/Thread/
Run parents and pass provenance-issued `PrivateResourceScope` on every final-schema
PostgreSQL product call.

- Event task-ID-before-LIMIT, cursor pagination, and all-event behavior are kept.
- Feedback retains create/read/list/group/aggregate/delete/upsert coverage under
  the one-owner-per-private-Run unique contract. Legacy `user_id` cannot select
  ownership.
- SQL token aggregation retains by-model fallback, Memory/SQL parity, and active
  progress coverage.
- Run/Event/Feedback/Thread cross-owner reads and mutations fail closed.
- The removed explicit-`None` product bypass is replaced with a test that names
  `TrustedUnscopedThreadMetaStore` as the cutover-only adapter; ordinary unscoped
  Run/Event/Feedback reads are separately proven fail closed.

## Secret-zero and scope audit

The snapshot DTOs and rows contain only asset/version/checksum/generation and MCP
version/slot/grant/credential-version UUIDs. `load_envelopes=False` validates and
locks the active envelope ID but neither returns nor persists envelope bytes. The
secret-zero test serializes every returned snapshot and rejects secret, envelope,
key ID, nonce, ciphertext, storage locator, and seeded key markers. Database
composite FKs continue to reject wrong-scope Run-linked event, asset-snapshot, and
file rows.

## Verification commands

All successful gates were run with:

```text
POSTGRES_TEST_URL=postgresql://postgres@/postgres?host=/tmp/deerflow-m4-task4-pg.4TpPaF&port=55445
```

Commands and final results:

```text
uv run pytest tests/test_private_run_repository.py tests/test_private_run_snapshot.py tests/test_run_manager.py tests/test_run_repository.py tests/test_run_event_store.py tests/test_run_events_endpoint.py tests/test_thread_messages_feedback.py -q
154 passed, 1 warning

uv run pytest tests/test_run_event_store_filter.py tests/test_feedback.py tests/test_token_usage_by_model.py tests/test_owner_isolation.py -q
64 passed

uv run pytest tests/test_m4_private_work_schema_postgres.py tests/test_project_schema_postgres.py tests/test_project_governance_schema_postgres.py tests/test_m3_shared_assets_schema_postgres.py tests/test_persistence_migrations_env.py tests/test_default_project_bootstrap.py tests/test_private_work_context.py tests/test_private_work_error_mapping.py tests/test_private_work_import_firewall.py tests/test_project_context.py tests/test_project_capabilities.py tests/test_private_thread_repository.py tests/test_project_scoped_checkpointer.py tests/test_private_thread_service.py tests/test_thread_meta_repo.py tests/test_threads_router.py tests/test_goal_runtime.py tests/test_thread_state_promoted.py -q
260 passed, 1 warning

uv run pytest tests/integration/test_m3_asset_resolution_postgres.py -q
11 passed

uv run pytest tests/test_runtime_lifecycle_e2e.py -q
6 failed, 2 passed, 1 warning (expected Task 11 staging gate)
```

Static verification also passed:

```text
uv run ruff check .
All checks passed!

uv run ruff format --check .
946 files already formatted

.venv/bin/python -m compileall -q app/private_work packages/harness/deerflow/persistence/run packages/harness/deerflow/persistence/feedback packages/harness/deerflow/runtime/events/store tests/test_private_run_snapshot.py tests/test_run_event_store_filter.py tests/test_feedback.py tests/test_token_usage_by_model.py tests/test_owner_isolation.py
exit 0

git diff --check
exit 0
```
