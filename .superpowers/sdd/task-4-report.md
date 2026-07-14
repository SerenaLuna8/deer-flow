# Task 4 implementation report

## Outcome

Task 4 scopes the existing run runtime instead of introducing a second manager or
worker. Project runs carry `PrivateResourceScope`; PostgreSQL run, event, and
feedback paths require `project_id + owner_user_id`; run admission can atomically
persist the exact M3 asset and MCP grant closure without secret material.

## TDD evidence

- Initial focused RED: `3 failed` for missing `RunRecord.scope`, missing
  `RunManager.create(..., scope=...)`, and missing production snapshot repository.
- Event/feedback RED: scoped event write failed because `DbRunEventStore.put()`
  accepted only legacy ownership.
- Focused GREEN: private run repository and snapshot tests pass against disposable
  PostgreSQL databases, including cross-owner/project/UUID/pagination/mutation,
  event history, feedback, exact order, rollback, and composite FK cases.
- Task 4 Step 6 gate: `146 passed, 1 warning`.
- Task 1-3 boundary gate: `82 passed` for final schema, context provenance, error
  mapping, import firewalls, private Thread repository/service, scoped checkpointer,
  and legacy cutover.
- Journal/worker/event-index compatibility gate: `88 passed`.
- Combined Task 4 plus boundary/compatibility gate: `316 passed, 1 warning`.

The known runtime-lifecycle staging audit remains exactly `2 passed, 6 failed`:
all six failures stop at the intentional legacy `409 PRIVATE_WORK_CUTOVER` and
belong to Task 11. No default authority was added to make them pass.

## Security properties

- Memory hits compare scope before returning a project run. Background status,
  model, progress, and completion writes derive scope from the registered
  `RunRecord`; legacy `scope=None` calls retain the old test-double interface.
- Startup orphan recovery uses only `list_inflight_trusted_unscoped()`.
- Event and feedback writes resolve a scoped parent run and derive project/owner;
  event payload `user_id` cannot choose ownership.
- `create_run_with_snapshot()` uses one PostgreSQL transaction. Root Agent is
  order 0, followed by resolver-stable Skill then MCP versions. It verifies the
  exact published version/checksum/dependency order, active credential scope and
  required slot grants, then locks and compares catalog generation before insert.
- Snapshot records contain only asset identity/version/checksum/generation and
  MCP version/slot/grant/credential-version UUIDs. Tests seed a real envelope and
  prove key ID, nonce, ciphertext, envelope/storage locator, and secret markers do
  not appear in persisted or returned snapshot objects.
- PostgreSQL composite FKs, not just Python checks, reject an event, asset
  snapshot, or output file linked to a run from the wrong owner scope.

## Compatibility and follow-up

The production SQL paths deliberately do not guess project or owner. A separate
enlarged legacy-suite audit is `35 passed, 29 failed`: 3 DB event-filter tests,
14 feedback repository tests, and 3 SQL token-usage tests call final-schema SQL
without a scope or scoped parent; 9 owner-isolation tests use the removed
context-user/explicit-`None` authority model and some fail earlier in Task 3
Thread creation. Those tests exercise pre-M4 contracts and need test-only scoped
fixtures/adapters rather than a production fallback. The Task 4 files instead
use seeded, provenance-issued scope. The six legacy runtime lifecycle cases
remain staged for Task 11 as required.
