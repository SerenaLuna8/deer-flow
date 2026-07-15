# M4 Task 18 implementation and verification report

Date: 2026-07-15

Baseline: `bdb64fe84c09ba1c7933e6a42451c179f01cd457`

Status: **BLOCKED before independent review**. Documentation and the focused M4 gates are ready, but the required full backend and full Playwright gates are not green. M4 is therefore still a candidate: this task does not mark M4 complete, does not change overall progress to 4/8, and does not check Task 18 or the final acceptance checklist.

## Scope delivered

- Updated `README.md` and `README_zh.md` with the M4 candidate project Chats, files/artifacts, Memory and Connections behavior; Viewer behavior; legacy cutover; migration commands; and an explicit M5-M8 boundary.
- Updated root/backend/frontend `AGENTS.md` files with M4 project-owner authority, scoped repositories/checkpointer, PostgreSQL file/Memory/connection authority, scoped frontend client/cache ownership, readiness/capability gating, and the six-file M1-M4 PostgreSQL gate.
- Updated the overall and M4 specs to say M1-M3 are complete while M4 is an implementation/full-gate candidate awaiting independent review.
- Added `docs/operations/m4-private-work-migration.md`, including all ten required staged-migration/operator points.
- Updated the M4 plan header without checking Task 18 or claiming completion.
- Repaired one stale focused backend test that still called `ChannelConnectionRepository.upsert_connection(owner_user_id=...)` after the production API changed to a required `PrivateResourceScope`.
- Repaired the workspace static-demo route so `?mock=true` retains its mock LangGraph client instead of being overwritten by the workspace route client.

The migration runbook states the current implementation truth: `DEER_FLOW_M4_BACKUP_KEY` is reserved for authenticated filesystem backup handling, while the current runnable-first CLI does not consume it and reports `backup_written=false`. It does not claim a backup the code does not create.

## Documentation consistency RED/GREEN

Baseline RED was captured at:

- `docs/superpowers/specs/2026-07-12-project-first-saas-design.md:5`: `3/8, 37.5%`.
- The same file at line 14: `M4 至 M8 尚未开始`.
- The same file at line 434: M4 milestone table `未开始`.
- README/AGENTS descriptions still referred to Task 17-before or a later M4 task for parts that Tasks 11-17 had already implemented.

The required final search:

```text
rg -n "3/8|37.5%|M4.*未开始|private work.*later milestone|M1, M2 and M3 PostgreSQL" README.md README_zh.md AGENTS.md backend/AGENTS.md frontend/AGENTS.md docs .github/workflows
```

now only finds:

- historical M3 completion/design language in `docs/superpowers/specs/2026-07-13-project-shared-assets-m3-design.md`;
- historical M3 implementation-plan instructions in `docs/superpowers/plans/2026-07-13-project-shared-assets-m3.md`;
- Task 18's own RED/search instructions in `docs/superpowers/plans/2026-07-14-project-private-work-m4.md`.

Those matches were manually checked and are historical/expected, not current M4 status claims.

## Disposable PostgreSQL safety record

- Data directory: `/tmp/deerflow_m4_task18_pg.npdqT5/data`
- Bind: `127.0.0.1:55418`
- Admin/maintenance database: `postgres`
- Explicit check database: `deerflow_task18_check`
- `make setup-db` initialized the check database to `0011_private_artifact_tombstone`.
- Integration fixtures created only random `deerflow_test_*` databases from the disposable admin URL.
- No business database URL or business database was used.
- The disposable PostgreSQL server was stopped successfully after verification.

## Backend verification

### Green gates

| Command | Result |
| --- | --- |
| `POSTGRES_TEST_URL=... uv run pytest tests/test_private_work_*.py tests/test_private_* tests/test_project_scoped_checkpointer.py -q` | `427 passed in 45.63s`, 0 skipped |
| `uv run pytest tests/blocking_io -q` | `35 passed in 11.15s` |
| `make lint` | pass, all checks passed |
| `make format` | pass, `1011 files left unchanged` |
| six fixed M1-M4 PostgreSQL integration files | `16 passed in 4.85s`, 0 skipped |
| `make check-db` against `deerflow_task18_check` | healthy; current and target revision both `0011_private_artifact_tombstone` |

The first focused run was `1 failed, 234 passed, 192 skipped` because it had no PostgreSQL URL and a stale unit call used the removed `owner_user_id` argument. The exact regression test passed after supplying `PrivateResourceScope`, and the fresh focused rerun with the disposable PostgreSQL URL produced the 427/427 result above.

### Blocking full-backend result

```text
POSTGRES_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:55418/postgres uv run pytest -q
81 failed, 8094 passed, 18 skipped, 10 errors, 12 warnings in 313.29s
```

This is a release blocker. It is not treated as a documentation-only warning and was not expanded into an open-ended 81-item repair sweep during Task 18.

The 10 setup errors are all `tests/test_console_router.py` cases. Their legacy fixtures insert `runs.project_id = NULL`, which the M4 final schema correctly rejects:

- `TestConsoleStats::test_headline_counters`
- `TestConsoleRuns::test_listing_orders_paginates_and_joins_titles`
- `TestConsoleRuns::test_offset_pagination`
- `TestConsoleRuns::test_status_filter`
- `TestConsoleUsage::test_daily_buckets_and_model_breakdown`
- `TestConsoleUsage::test_window_excludes_old_rows_but_stats_include_them`
- `TestPricing::test_costs_use_cache_hit_price`
- `TestPricing::test_cache_hits_billed_at_miss_price_without_hit_price`
- `TestPricing::test_costs_null_without_pricing`
- `TestUserScoping::test_rows_filtered_by_resolved_user`

The 81 failed nodes group as follows.

### Channel connection router: 25

- `test_get_providers_only_returns_enabled_channels_and_setup_fields`
- `test_get_providers_uses_existing_channels_config`
- `test_get_providers_reports_connected_without_binding_in_auth_disabled_mode`
- `test_get_providers_reports_unconfigured_when_runtime_channel_is_missing`
- `test_get_providers_reports_configured_channel_not_running`
- `test_get_providers_provider_unavailable_overrides_stale_connected_row`
- `test_get_providers_restarts_configured_channel_when_service_can_reconcile`
- `test_get_providers_uses_newest_connection_status_per_provider`
- `test_get_connections_returns_current_user_connections_only`
- `test_connect_telegram_returns_deep_link_and_persists_state`
- `test_connect_slack_returns_binding_command_and_persists_state`
- `test_connect_binding_code_caps_pending_states_per_provider`
- `test_connect_discord_returns_binding_command_and_persists_state`
- `test_connect_existing_binding_code_channels_return_command_and_persist_state`
- `test_configure_provider_runtime_credentials_enables_connect_without_file_edits`
- `test_runtime_config_endpoints_require_admin`
- `test_configure_telegram_runtime_uses_new_bot_username_for_deep_link_without_mutating_config`
- `test_configure_provider_runtime_credentials_survive_local_restart`
- `test_configure_provider_runtime_credentials_preserves_masked_secrets`
- `test_disconnect_provider_runtime_config_clears_connected_state`
- `test_disconnect_provider_runtime_config_suppresses_file_config_and_stops_channel`
- `test_disconnect_provider_runtime_config_revokes_all_provider_connections`
- `test_get_providers_preserves_revoked_status_when_provider_unavailable`
- `test_disconnect_connection_revokes_current_user_connection`
- `test_disconnect_connection_is_current_user_scoped`

All are in `tests/test_channel_connections_router.py`. The shared cause is the old owner-only channel connection fixture/adapter contract versus the final scoped repository API.

### Channel runtime identity/scope: 9

- `tests/test_channel_runtime_identity.py::test_unbound_channel_runtime_identity_crosses_gateway_and_background_task`
- `tests/test_channel_runtime_identity.py::test_unbound_platform_users_never_converge_on_default`
- `tests/test_channel_runtime_identity.py::test_runtime_identity_never_becomes_run_owner_thread_owner_or_private_authority`
- `tests/test_channel_runtime_identity.py::test_non_internal_runtime_header_is_ignored_and_body_user_id_is_stripped`
- `tests/test_channel_runtime_identity.py::test_bound_owner_header_contract_is_preserved_for_all_channel_run_calls[wait]`
- `tests/test_channel_runtime_identity.py::test_bound_owner_header_contract_is_preserved_for_all_channel_run_calls[create]`
- `tests/test_channel_runtime_identity.py::test_bound_owner_header_contract_is_preserved_for_all_channel_run_calls[stream]`
- `tests/test_channel_runtime_worker_scope.py::test_real_worker_keeps_runtime_storage_separate_from_repository_and_checkpoint_scope`
- `tests/test_channel_runtime_worker_scope.py::test_real_worker_preserves_bound_owner_for_repository_and_runtime_storage`

### Gateway/runtime lifecycle: 25

- `tests/test_durable_context_middleware.py::TestSkillContextInjection::test_skill_reference_injected_not_body`
- `tests/test_durable_context_middleware.py::TestSkillContextInjection::test_skill_reference_survives_summarization_and_stays_injected`
- `tests/test_gateway_run_drain_shutdown.py::test_shutdown_surfaces_failed_interrupted_persist`
- `tests/test_gateway_services.py::test_start_run_validates_before_lifecycle_side_effects_and_allows_retry[missing-checkpoint-interrupt]`
- `tests/test_gateway_services.py::test_start_run_validates_before_lifecycle_side_effects_and_allows_retry[missing-checkpoint-rollback]`
- `tests/test_gateway_services.py::test_start_run_validates_before_lifecycle_side_effects_and_allows_retry[failed-checkpoint-interrupt]`
- `tests/test_gateway_services.py::test_start_run_validates_before_lifecycle_side_effects_and_allows_retry[failed-checkpoint-rollback]`
- `tests/test_gateway_services.py::test_start_run_validates_before_lifecycle_side_effects_and_allows_retry[malformed-message-interrupt]`
- `tests/test_gateway_services.py::test_start_run_validates_before_lifecycle_side_effects_and_allows_retry[malformed-message-rollback]`
- `tests/test_gateway_services.py::test_start_run_validates_before_lifecycle_side_effects_and_allows_retry[invalid-config-interrupt]`
- `tests/test_gateway_services.py::test_start_run_validates_before_lifecycle_side_effects_and_allows_retry[invalid-config-rollback]`
- `tests/test_gateway_services.py::test_start_run_authorizes_before_checkpoint_saver_probe`
- `tests/test_gateway_services.py::test_start_run_normalizes_checkpoint_control_for_persistence_saver_and_live[config-only]`
- `tests/test_gateway_services.py::test_start_run_normalizes_checkpoint_control_for_persistence_saver_and_live[typed-overrides]`
- `tests/test_gateway_services.py::test_start_run_sanitizes_live_persisted_and_response_run_control_without_touching_input`
- `tests/test_gateway_services.py::test_start_run_translates_resume_command_to_langgraph_command`
- `tests/test_gateway_services.py::test_start_run_uses_normalized_input_without_command`
- `tests/test_gateway_services.py::test_start_run_uses_internal_owner_header_for_persistence`
- `tests/test_gateway_services.py::test_start_run_stamps_internal_owner_guardrail_attribution`
- `tests/test_runtime_lifecycle_e2e.py::test_stream_run_completes_and_persists_runtime_state`
- `tests/test_runtime_lifecycle_e2e.py::test_stream_run_executes_real_lead_agent_setup_agent_business_path`
- `tests/test_runtime_lifecycle_e2e.py::test_cancel_interrupt_stops_running_background_run`
- `tests/test_runtime_lifecycle_e2e.py::test_cancel_interrupt_generates_missing_title_from_checkpoint`
- `tests/test_runtime_lifecycle_e2e.py::test_cancel_wait_false_generates_title_from_graph_input_before_checkpoint`
- `tests/test_runtime_lifecycle_e2e.py::test_cancel_rollback_restores_pre_run_checkpoint`

### Persistence/bootstrap/fixture/golden/legacy: 16

- `tests/test_legacy_system_asset_runtime.py::test_legacy_start_rejects_project_agent_before_checkpoint_or_run_launch[asyncio]`
- `tests/test_legacy_system_asset_runtime.py::test_legacy_start_preserves_system_agent_runtime[asyncio]`
- `tests/test_persistence_autogen_script.py::test_autogen_builds_temp_db_at_head_without_data_dir`
- `tests/test_persistence_autogen_script.py::test_autogen_temp_db_is_at_head`
- `tests/test_persistence_autogen_script.py::test_autogen_temp_db_comes_from_migration_history_not_current_metadata`
- `tests/test_persistence_bootstrap_regression.py::test_legacy_database_recovers_token_usage_column`
- `tests/test_persistence_bootstrap_regression.py::test_legacy_database_with_manual_alter_still_bootstraps`
- `tests/test_persistence_timezone.py::test_thread_meta_emits_tz_aware_timestamps[asyncio]`
- `tests/test_persistence_timezone.py::test_run_repository_emits_tz_aware_timestamps[asyncio]`
- `tests/test_persistence_timezone.py::test_feedback_repository_emits_tz_aware_timestamps[asyncio]`
- `tests/test_persistence_timezone.py::test_run_event_store_emits_tz_aware_timestamps[asyncio]`
- `tests/test_postgres_fixture.py::test_repository_fixture_failure_closes_global_engine_before_database_drop[test_feedback]`
- `tests/test_postgres_fixture.py::test_channel_fixture_failure_disposes_owned_engine[test_additional_channel_connections-helper_args0]`
- `tests/test_postgres_fixture.py::test_channel_fixture_failure_disposes_owned_engine[test_slack_channel_connections-helper_args2]`
- `tests/test_replay_golden.py::test_replay_write_read_file_ultra_matches_golden`
- `tests/test_setup_agent_http_e2e_real_server.py::test_real_http_create_agent_lands_in_authenticated_user_dir`

### SQLite migration compatibility: 6

- `test_explicit_user_reconciliation_builds_one_ordered_absorption`
- `test_business_normalization_decodes_json_bool_utc_and_stable_key`
- `test_business_normalization_rejects_unapproved_nullable_composite_key`
- `test_business_normalization_rejects_missing_required_and_invalid_typed_values`
- `test_channel_active_identity_partial_unique_ignores_revoked_only`
- `test_real_postgres_two_source_user_reconciliation_and_ledger_replay`

All are in `tests/test_sqlite_to_postgres_migration.py`. Observed failures include pre-M4 known-column/business-key maps (`feedback.user_id` and missing `project_id`) rather than failure of the fixed six-file M4 PostgreSQL gate.

The concentrated repair seams are therefore shared fixtures/adapters, not 81 unrelated product bugs: channel owner-only fixtures, gateway/console final-schema factories, persistence fixture builders, and SQLite normalization maps. The full suite must still be rerun after that separate repair wave.

## Frontend verification

| Command | Result |
| --- | --- |
| `pnpm check` after the frontend repair | pass |
| `pnpm test` after the frontend repair | 116 files, 836 passed, 0 skipped, 0 snapshots changed |
| isolated static mock regression | 1 passed |

Full Playwright remains blocked:

1. First full run: `155 passed, 1 failed`; `thread-history.spec.ts` proved `?mock=true` loaded the real workspace run-history client. The scoped chat page now retains the contextual mock client in mock mode. The isolated regression passed.
2. Second full run: `155 passed, 1 failed`; the repaired static mock test passed, but `artifact-stream-state.spec.ts::keeps artifact trigger after stream values omit artifacts` failed.
3. The artifact case also failed isolated. A three-repeat run produced `2 passed, 1 failed`.
4. An A/B run with the Task 18 mock-client change temporarily removed also produced `2 passed, 1 failed`, proving that artifact fluctuation is not caused by the Task 18 client repair. The A/B experiment and an ineffective SSE `end` experiment were reverted.

The artifact failure was frozen as a release blocker rather than subjected to repeated open-ended repair/review loops.

## Root diagnostics and repository checks

- `make check-db`: pass and healthy against the disposable check database.
- `make doctor`: exit 2 / Make exit 1 with 2 errors and 2 warnings. The worktree has no local `config.yaml`; `.env` and `frontend/.env` are reported as setup warnings, and doctor reports its aggregate PostgreSQL health check failed even though the direct `make check-db` immediately before it was healthy. This is recorded as diagnostic failure, not rewritten as success.
- `git diff --check`: pass.
- `git status --short`: only Task 18 expected documentation plus the two focused regression repairs before this report was added.

## Required next decision

Do not start independent review or completion steps from this report. A single, bounded repair wave should first address:

1. the shared full-backend fixture/adapter seams above, then rerun the complete backend and the six fixed PostgreSQL files;
2. the existing `artifact-stream-state` Playwright race, then rerun full Playwright once;
3. root doctor only after a Task-local test configuration policy is chosen, because this worktree intentionally lacks user-local config.

Only after fresh green outputs may Task 18 proceed to independent review and the final verification-before-completion step.
