# Task 2 Channel Runtime Identity Fix

## Outcome

Closed the remaining Important finding from the post-fix independent review without starting Task 3. Internally authenticated channel runs now carry a separate, path-safe runtime/storage identity from `ChannelManager` to Gateway. Gateway uses it only for the live execution context and the background task's user `ContextVar`; thread/run ownership, persistence attribution, project membership, checkpoint authority, and private-work authority remain unchanged.

Implementation commit: `bd97048b917d75980fd20e20aa7cb2a4925891dd`

## RED evidence

The cross-layer regression initially exercised the real `ChannelManager._handle_chat` branch, Gateway `start_run()`, real `RunManager`, and `MemoryThreadMetaStore`:

```text
cd backend && uv run pytest tests/test_channel_runtime_identity.py -q
6 failed, 1 passed
```

The failures showed that the runtime identity header was absent, two unbound platform users both executed as `default`, and the live runtime context had no channel bucket identity.

Two focused failure cycles found additional boundary requirements:

- A request stamped with the internal system role but a session auth source was incorrectly accepted until the runtime-header reader also required `AUTH_SOURCE_INTERNAL` (`1 failed`).
- A channel adapter that rewrote `InboundMessage` during file receive caused owner identity to drift until run headers were cached before ingestion (`1 failed`).

## GREEN implementation

- Added `X-DeerFlow-Runtime-User-Id` to process-internal run requests and normalized it with `make_safe_user_id`.
- Required both verified internal auth source and the synthetic internal system role before Gateway honors the header.
- Kept the runtime identity distinct from the existing raw owner header.
- Cached owner/runtime headers before channel file ingestion so rewritten inbound messages cannot alter either identity.
- Installed the runtime identity only in live `config.context.user_id` and the background task's inherited user `ContextVar`.
- Left `RunManager.create_or_reject(user_id=...)`, thread ownership, metadata, persisted run kwargs, and checkpoint `configurable` authority unchanged.
- Kept ordinary client `body.context.user_id`, project fields, roles, capabilities, and forged runtime headers untrusted and stripped/ignored.
- Documented the channel-to-Gateway identity boundary in `backend/AGENTS.md`.

## Verification

Final focused assertion run after all code and test edits:

```text
cd backend && uv run pytest tests/test_channel_runtime_identity.py -q
7 passed in 0.54s
```

Channel/auth/Gateway regression set:

```text
cd backend && uv run pytest tests/test_channel_runtime_identity.py tests/test_internal_auth.py tests/test_auth_middleware.py tests/test_auth.py tests/test_channels.py tests/test_gateway_services.py -q
472 passed, 3 skipped, 6 warnings in 21.21s
```

Task 2 focused regression set:

```text
cd backend && uv run pytest tests/test_private_work_context.py tests/test_private_work_error_mapping.py tests/test_private_work_import_firewall.py tests/test_project_context.py tests/test_project_capabilities.py tests/test_gateway_services.py tests/test_channel_runtime_identity.py -q
158 passed, 5 skipped in 1.12s
```

The skipped tests require `POSTGRES_TEST_URL`, as required by the repository's disposable-PostgreSQL safety rules. The warnings were existing deprecation warnings.

Static and patch checks:

```text
uv run ruff check app/channels/manager.py app/gateway/internal_auth.py app/gateway/services.py tests/test_channels.py tests/test_channel_runtime_identity.py tests/test_internal_auth.py
All checks passed!

uv run ruff format --check app/channels/manager.py app/gateway/internal_auth.py app/gateway/services.py tests/test_channels.py tests/test_channel_runtime_identity.py tests/test_internal_auth.py
6 files already formatted

git diff --check
exit 0
```

## Files changed

- `backend/app/channels/manager.py`
- `backend/app/gateway/internal_auth.py`
- `backend/app/gateway/services.py`
- `backend/tests/test_channel_runtime_identity.py`
- `backend/tests/test_channels.py`
- `backend/tests/test_internal_auth.py`
- `backend/AGENTS.md`

## Self-review and residual risks

- The new header is not a general identity credential: non-internal callers cannot activate it, and tests cover forged session requests.
- Bound channels retain their trusted owner contract; unbound channels gain only their sanitized execution/storage bucket.
- Tests assert the runtime identity does not appear in persisted run kwargs, metadata, thread ownership, or checkpoint `configurable` data and does not mint project/private authority.
- No database, migration, repository/checkpointer scoping, frontend, credential, or other Task 3 work was added.
- Residual operational risk is limited to external channel SDK/header transport behavior; manager tests cover create/wait/stream header forwarding, while the cross-layer test covers Gateway consumption and execution identity.
