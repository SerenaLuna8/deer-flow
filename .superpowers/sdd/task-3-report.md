# M7 Task 3 Report: remove the global LangGraph/private-work HTTP runtime

## Status

PASS — completed Task 3 only on base commit `bcd3e1ecd61e0d0545596a2bbfbfda3bd2d75915`. The implementation commit is `670d695a` (`refactor: remove global private runtime`). This does not start Task 4, claim M7 completion, or claim release readiness.

## Delivered

- Added `app.private_work.http_runtime` as the project-private HTTP admission boundary, exporting only `format_sse` and `start_private_run`.
- Moved `PrivateRunCreateRequest` and `PrivateThreadTokenUsageResponse` ownership into `gateway.private_work_schemas` and removed client-supplied private-run authority.
- Reduced Gateway lifespan wiring to the PostgreSQL platform services still required by project-private APIs. Removed `RunManager`, the legacy stream bridge, configurable legacy Run/Event stores, scheduled legacy repositories, and orphan-thread migration from Gateway startup.
- Removed the global Thread, Run, Assistant compatibility, Memory, Feedback, Suggestion, Upload, and Artifact routers and their named obsolete tests. Project-private routes remain mounted.
- Made connection inbound admit a project-private durable Run directly and wait on its PostgreSQL terminal state instead of invoking the removed in-process runtime.
- Removed the `/api/langgraph` Nginx rewrite and updated local/Docker/deploy messaging and repository guidance for the M7 topology.

## TDD evidence

Initial Task 3 surface RED before production changes:

```text
5 failed, 1 passed in 1.61s
```

The failures were the intended global route mounts, `RunManager`/stream-bridge wiring, missing project schema/runtime ownership, importable legacy router modules, and Nginx `/api/langgraph` rewrite. The already-passing assertion established that project-private routes existed before the removal.

## Final verification

Required affected PostgreSQL gate:

```text
POSTGRES_TEST_URL=postgresql+asyncpg://jiangfeng@127.0.0.1:55437/postgres \
PYTHONPATH=packages/harness .venv/bin/pytest \
  tests/test_m7_legacy_api_surface.py \
  tests/test_private_work_router.py \
  tests/test_private_work_run_router.py \
  tests/test_private_work_stream_router.py \
  tests/test_private_work_file_router.py \
  tests/test_channel_runtime_identity.py \
  tests/test_m6_private_run_gateway.py \
  tests/test_m6_gateway_reconnect_process.py -q

36 passed in 8.61s
```

PostgreSQL skips: **0**. The final run used unsandboxed localhost access because the sandbox denied TCP access to the designated disposable PostgreSQL listener.

Required blocking-I/O gate:

```text
POSTGRES_TEST_URL=postgresql+asyncpg://jiangfeng@127.0.0.1:55437/postgres \
PYTHONPATH=packages/harness .venv/bin/pytest \
  tests/blocking_io/test_gate_smoke.py \
  tests/blocking_io/test_automations.py -q

9 passed in 0.44s
```

Additional checks:

```text
Ruff check: All checks passed
Ruff format: 105 files already formatted
git diff --check: passed
Production residue scan for /api/langgraph, make_stream_bridge, and RunManager: zero hits
```

## Self-review

- `http_runtime.__all__` exposes exactly `format_sse` and `start_private_run`; `format_sse` emits compact JSON with an optional SSE `id` line first.
- Run admission preserves only the allowed input, command, config, context, and metadata fields while server-owned account/project/owner and non-interactive authority are derived from authenticated scope.
- The standard Gateway no longer mounts a global LangGraph-compatible API or creates an in-process agent runtime. `langgraph_auth` remains only for explicitly separate development tooling.
- The scheduled-task legacy router remains mounted for Task 4's RED boundary, while Gateway lifespan does not initialize its legacy repositories or service singleton.
- `.superpowers/sdd/progress.md` was not changed.

## Risks and open items

- Task 4 and later M7 cleanup remain intentionally untouched; this report does not claim their modules or tests have been removed.
- The full backend execution suite was not run. The independent-review repair below adds complete collection plus the frozen affected, blocking, reviewer-targeted, lifecycle, and delayed-import slices.
- Milestone-ledger acceptance remains owned by the parent review flow; this report does not edit the ledger.

## Independent review repair (2026-07-18)

### Status

PASS — repaired the independent review result of 0 Critical, 2 Important, and 1 Minor in commit `c003cca8` (`fix: repair M7 task 3 review findings`). This remains Task 3-only work; `.superpowers/sdd/progress.md` is unchanged and Task 4 has not started.

### Findings closed

- Restored the `/api/scheduled-tasks` router mount so Task 4 retains its required RED starting point. The Task 3 global `/api/threads` removal still applies, so the nested `/api/threads/{thread_id}/scheduled-tasks` compatibility route is absent. Gateway runtime creates no legacy scheduled repository/service singleton.
- Repaired all collection-time imports of deleted routers/runtime helpers. Tests whose only subject was a deleted global Thread/Run/Memory/upload/artifact or in-Gateway execution surface were removed; surviving SSE, config-sanitization, file-limit, lifecycle, project-channel scope, Worker authority, embedded-client, and harness-drain assertions were migrated to their live modules.
- Migrated `test_gateway_services.py` to `app.private_work.http_runtime.format_sse` and `app.private_work.runtime_context.prepare_private_run_config`; delayed imports of deleted global normalization/start/run services were removed rather than hidden through collection ignores.
- Updated Docker/deploy runtime messaging and backend architecture guidance to describe Gateway durable admission plus independent Worker execution.

### Repair RED evidence

Scheduled-task mount regression before restoring the router:

```text
1 failed, 6 passed in 1.55s
E   AssertionError: assert '/api/scheduled-tasks' in paths
```

Initial complete backend collection after the Task 3 implementation:

```text
8278 tests collected, 12 errors in 3.21s
```

The errors were imports of deleted `thread_runs`, `memory`, `langgraph_runtime`, `build_run_config`, and related global router/service symbols. The review snapshot had reported seven; the fresh branch state exposed twelve.

A focused scheduled/router run then exposed three delayed stale assertions after collection was clean:

```text
3 failed, 179 passed, 2 deselected in 3.06s
```

The final delayed-import slice initially exposed one remaining obsolete Nginx rewrite assertion:

```text
1 failed, 188 passed, 1 skipped, 28 deselected in 7.41s
```

### Final repair verification

Complete backend collection:

```text
PYTHONPATH=packages/harness .venv/bin/pytest --collect-only -q
8285 tests collected in 3.16s
```

Collection errors: **0**.

Reviewer-targeted files:

```text
POSTGRES_TEST_URL=postgresql+asyncpg://jiangfeng@127.0.0.1:55437/postgres \
PYTHONPATH=packages/harness .venv/bin/pytest \
  tests/test_private_work_cutover_guard.py \
  tests/test_client.py \
  tests/test_gateway_services.py -q -rs

133 passed in 2.10s
```

Required Task 3 affected PostgreSQL gate, including the new scheduled-task OpenAPI regression:

```text
37 passed in 9.35s
PostgreSQL skips: 0
```

The original implementation gate had 36 tests; the repair adds one required OpenAPI assertion, so the final count is 37.

Required blocking-I/O gate:

```text
9 passed in 0.45s
```

Scheduled/OpenAPI and runtime-authority focused gate:

```text
62 passed, 1 deselected in 2.54s
```

Project-private Gateway lifecycle/cutover gate:

```text
8 passed in 2.34s
```

Delayed-import and migrated surviving-contract slice:

```text
189 passed, 1 skipped, 28 deselected in 7.20s
```

The single skip is the existing optional Docker CLI/Compose availability test; it is outside the zero-skip PostgreSQL gate.

Additional final checks:

```text
Ruff: All checks passed for 24 modified Python files
Ruff format: 24 files already formatted
git diff --check: passed
Production /api/langgraph, make_stream_bridge, and RunManager scan: zero hits
Deleted router/service test-import scan: zero unexpected hits
Gateway embedded-runtime wording scan: zero hits
```

### Repair scope and remaining concerns

- Deleted tests were limited to global HTTP/runtime behavior that no longer exists: global Run cancel/messages/events/token usage/regenerate/wait, Gateway orphan recovery, and Gateway-auth injection into file-backed setup/update Agent tools. Project-private Run/stream/file/cutover coverage and independent Worker authority remain collected and green.
- `tests/test_channel_runtime_worker_scope.py` remains in place for Task 5 and now asserts project admission strips message authority and the Worker rebuilds exact scope from issued `PrivateWorkContext`.
- `tests/test_multi_worker_postgres_gate.py` remains in place and now pins that Gateway has no embedded runner while `RunAgentPrivateExecutor` owns agent execution.
- The complete backend execution suite was not run. Completion evidence is the full zero-error collection plus the frozen Task 3 PostgreSQL, blocking, reviewer-targeted, scheduled/OpenAPI, lifecycle, and direct delayed-import slices above.
- Task 4 still owns complete removal of the mounted legacy scheduled-task router and implementation.

## Final frozen review repair (2026-07-18)

### Status

Task 3 direct scope PASS in implementation commit `cf8b7651` (`fix: finish M7 task 3 review repair`). The required complete backend execution was actually run and is not globally green because 126 remaining failure candidates belong to later M7 tasks. No later-task production code was repaired, Task 4 was not started, and `.superpowers/sdd/progress.md` remains unchanged.

### Findings closed

- Project connection inbound tests now inject `build_gateway_project_run_launcher(..., start_private_run_fn=...)`, use a `PrivateRunService` pending-to-success double, assert two durable reads, and read the final project-scoped checkpoint.
- Worker identity coverage now executes `RunAgentPrivateExecutor` and asserts both owner ContextVars plus the issued private resource scope; it no longer imports or patches the empty Gateway services module.
- The deleted global `/api/threads` endpoint is asserted as an ordinary 404. Runtime lifecycle coverage retains only its valid Gateway PostgreSQL Store lifespan contract.
- Backend guidance now names `app.private_work.http_runtime.start_private_run` admission and independent `RunAgentPrivateExecutor` execution.
- Complete-suite discovery exposed 13 additional Task 3 stale tests: 11 orphan-thread/admin migration tests, one Gateway `get_run_context` test, and one global Thread stream OpenAPI test. They were removed or reduced to their surviving marker-free contracts; the direct Task 3 remaining count is now zero.

### RED and focused evidence

The frozen four-file RED command produced:

```text
1 failed, 22 passed, 11 skipped in 0.77s
FAILED tests/test_private_runtime_context.py::test_launch_registered_private_run_derives_task_identities_from_admitted_owner[asyncio]
AttributeError: app.gateway.services has no attribute run_agent
```

Final focused PostgreSQL run covering the four named files plus the three direct-residual files:

```text
45 passed, 0 skipped, 1 warning in 5.17s
```

Final Task 3 affected PostgreSQL plus scheduled/OpenAPI combined gate:

```text
66 passed, 0 skipped, 1 warning in 10.12s
```

The original frozen Task 3 PostgreSQL gate also ran independently before the final direct-residual cleanup:

```text
37 passed in 9.27s
PostgreSQL skips: 0
```

Final blocking-I/O gate:

```text
9 passed in 0.46s
```

Final complete collection:

```text
8274 tests collected in 3.17s
Collection errors: 0
```

### Complete backend execution result

The mandated command was run without interruption:

```text
POSTGRES_TEST_URL=postgresql+asyncpg://jiangfeng@127.0.0.1:55437/postgres \
PYTHONPATH=packages/harness .venv/bin/pytest -q

137 failed, 8130 passed, 18 skipped, 12 warnings, 2 errors in 513.01s (0:08:33)
```

The two setup errors were:

- `tests/test_legacy_automation_reads_postgres.py::test_expand_legacy_reads_return_exact_dtos_and_hide_other_owners`
- `tests/test_legacy_automation_reads_postgres.py::test_expand_legacy_mutations_are_409_and_write_nothing`

Both attempt to downgrade the forward-only `0015_project_reliability_finalize` migration to `0011_private_artifact_tombstone`. Per the frozen instruction, no open-ended repair was made.

An initial `pytest --lf --collect-only -q` selected 139 nodes and identified 13 direct Task 3 stale contracts. After repairing only those, their focused files passed and the same command selected 126 remaining nodes:

```text
126/522 tests collected (396 deselected) in 1.25s
```

Remaining-node classification:

- Task 3 deleted global runtime/direct stale imports: **0**
- Task 2 deterministic system assets and removed filesystem authority: **62**
- Task 4 legacy Automation API/read surface: **2**
- Task 5 channel authority: **3**
- Task 7 legacy config/fallback stores: **3**
- Task 8 migration reset and migration CLI removal: **56**
- Other: **0**

Exact remaining `--lf --collect-only` node list:

```text
tests/blocking_io/test_wechat_channel_state.py::test_wechat_inbound_file_staging_does_not_block_event_loop
tests/integration/test_m3_asset_migration_postgres.py::test_migration_is_idempotent_and_checksum_change_creates_version
tests/integration/test_m3_asset_migration_postgres.py::test_system_agent_mcp_skill_and_secret_migrate_as_one_validated_catalog
tests/integration/test_m3_asset_migration_postgres.py::test_identical_agent_source_reuses_frozen_dependency_versions
tests/integration/test_m3_asset_migration_postgres.py::test_cutover_requires_every_validation_probe[counts]
tests/integration/test_m3_asset_migration_postgres.py::test_cutover_requires_every_validation_probe[checksums]
tests/integration/test_m3_asset_migration_postgres.py::test_cutover_requires_every_validation_probe[dependencies]
tests/integration/test_m3_asset_migration_postgres.py::test_cutover_requires_every_validation_probe[decrypt]
tests/test_automation_migration.py::test_legacy_migration_reaches_head_is_redacted_and_idempotent
tests/test_automation_migration.py::test_invalid_legacy_source_fails_before_expand_ddl[viewer-mapped project membership is unavailable]
tests/test_automation_migration.py::test_invalid_legacy_source_fails_before_expand_ddl[agent-fresh Agent is not executable]
tests/test_automation_migration.py::test_invalid_legacy_source_fails_before_expand_ddl[reuse_scope-reuse Thread scope does not match owner map]
tests/test_automation_migration.py::test_invalid_legacy_source_fails_before_expand_ddl[orphan_run-orphan automation run]
tests/test_automation_migration.py::test_invalid_legacy_source_fails_before_expand_ddl[orphan_task_history-orphan automation run]
tests/test_automation_migration.py::test_invalid_legacy_source_fails_before_expand_ddl[status-unsupported legacy Automation status]
tests/test_automation_migration.py::test_staged_rerun_rejects_target_tamper_and_source_fingerprint_change
tests/test_automation_migration.py::test_partial_domain_ledger_resumes_without_treating_unwritten_domain_as_tamper
tests/test_automation_migration.py::test_atomic_staging_rolls_back_before_first_ledger
tests/test_automation_migration.py::test_finalize_rechecks_actual_target_digest_before_destructive_ddl
tests/test_automation_migration.py::test_final_schema_pre_marker_crash_resumes_by_revalidating_receipts_only
tests/test_automation_migration.py::test_final_schema_resume_rejects_target_tamper_without_rebuilding_lossy_source
tests/test_automation_migration.py::test_execute_locks_legacy_writers_and_finalize_rejects_post_stage_drift
tests/test_automation_migration.py::test_public_execute_revalidates_writer_drift_after_finalize_commits
tests/test_automation_migration.py::test_finalize_first_blocks_scheduler_select_for_update_before_ddl
tests/test_automation_migration.py::test_scheduler_select_for_update_first_makes_finalize_wait_without_deadlock
tests/test_automation_migration.py::test_finalize_rejects_unknown_expanded_column_before_destructive_ddl[scheduled_tasks-user_id]
tests/test_automation_migration.py::test_finalize_rejects_unknown_expanded_column_before_destructive_ddl[scheduled_task_runs-error]
tests/test_automation_migration.py::test_negative_legacy_run_count_fails_preflight_without_writes[False]
tests/test_automation_migration.py::test_negative_legacy_run_count_fails_preflight_without_writes[True]
tests/test_automation_migration.py::test_finalize_rejects_constraint_invalid_target_before_destructive_ddl
tests/test_automation_migration.py::test_unknown_legacy_source_column_fails_dry_run_and_execute_without_writes[0011-scheduled_tasks]
tests/test_automation_migration.py::test_unknown_legacy_source_column_fails_dry_run_and_execute_without_writes[0011-scheduled_task_runs]
tests/test_automation_migration.py::test_unknown_legacy_source_column_fails_dry_run_and_execute_without_writes[0012-scheduled_tasks]
tests/test_automation_migration.py::test_unknown_legacy_source_column_fails_dry_run_and_execute_without_writes[0012-scheduled_task_runs]
tests/test_channels.py::TestChannelManager::test_handle_command_slash_skill_reports_disabled_skill
tests/test_channels.py::TestChannelManager::test_handle_command_uninstalled_slash_skill_stays_unknown_command
tests/test_github_agents_config.py::test_load_agent_config_reads_github_block
tests/test_github_agents_config.py::test_load_agent_config_without_github_block_is_none
tests/test_github_agents_config.py::test_load_agent_config_rejects_duplicate_repo_bindings
tests/test_initialize_admin.py::test_initialize_bootstraps_default_project_and_real_csrf_flow
tests/test_legacy_automation_reads_postgres.py::test_expand_legacy_reads_return_exact_dtos_and_hide_other_owners
tests/test_legacy_automation_reads_postgres.py::test_expand_legacy_mutations_are_409_and_write_nothing
tests/test_local_sandbox_provider_mounts.py::TestLocalSandboxProviderMounts::test_setup_path_mappings_uses_configured_skills_container_path_as_reserved_prefix
tests/test_local_sandbox_provider_mounts.py::TestLocalSandboxProviderMounts::test_setup_path_mappings_skips_relative_host_path
tests/test_local_sandbox_provider_mounts.py::TestLocalSandboxProviderMounts::test_setup_path_mappings_skips_non_absolute_container_path
tests/test_local_sandbox_provider_mounts.py::TestLocalSandboxProviderMounts::test_setup_path_mappings_logs_actionable_error_for_missing_host_path
tests/test_local_sandbox_provider_mounts.py::TestLocalSandboxProviderMounts::test_setup_path_mappings_normalizes_container_path_trailing_slash
tests/test_m3_shared_assets_schema_postgres.py::test_m3_schema_has_all_typed_tables
tests/test_m4_private_work_schema_postgres.py::test_0011_accepts_manual_shape_and_retries_with_exact_partial_index
tests/test_m4_private_work_schema_postgres.py::test_0011_downgrade_is_retry_safe_and_can_upgrade_again
tests/test_m4_private_work_schema_postgres.py::test_0011_missing_artifacts_table_fails_closed_without_stamping_head
tests/test_m4_private_work_schema_postgres.py::test_0011_rejects_wrong_existing_index_shape_without_stamping_head[columns]
tests/test_m4_private_work_schema_postgres.py::test_0011_rejects_wrong_existing_index_shape_without_stamping_head[unique]
tests/test_m4_private_work_schema_postgres.py::test_0011_rejects_wrong_existing_index_shape_without_stamping_head[predicate]
tests/test_m4_private_work_schema_postgres.py::test_m4_finalize_schema_has_private_scope_and_composite_fks
tests/test_m4_private_work_schema_postgres.py::test_fresh_and_staged_private_work_catalogs_are_identical
tests/test_m4_private_work_schema_postgres.py::test_0009_downgrade_rejects_scoped_channel_rows_before_schema_changes[revoked]
tests/test_m4_private_work_schema_postgres.py::test_0009_downgrade_rejects_scoped_channel_rows_before_schema_changes[frozen]
tests/test_m5_automation_schema_postgres.py::test_m5_final_schema_has_private_scope_and_occurrence_constraints
tests/test_m5_automation_schema_postgres.py::test_fresh_and_staged_m5_catalogs_are_identical
tests/test_m5_automation_schema_postgres.py::test_nonempty_automation_domain_fails_before_finalize_ddl
tests/test_m5_automation_schema_postgres.py::test_cross_project_agent_fails_finalize_before_destructive_ddl
tests/test_m6_private_sse_reconnect_postgres.py::test_replays_only_frames_after_last_event_id_across_gateway_restart
tests/test_m6_private_sse_reconnect_postgres.py::test_replay_corrects_provisional_success_when_cancel_wins_settlement
tests/test_mcp_custom_interceptors.py::test_custom_interceptor_loaded_and_appended
tests/test_mcp_custom_interceptors.py::test_multiple_custom_interceptors
tests/test_mcp_custom_interceptors.py::test_custom_interceptor_builder_returning_none_is_skipped
tests/test_mcp_custom_interceptors.py::test_custom_interceptor_resolve_error_logs_warning_and_continues
tests/test_mcp_custom_interceptors.py::test_custom_interceptor_builder_exception_logs_warning_and_continues
tests/test_mcp_custom_interceptors.py::test_no_mcp_interceptors_field_is_safe
tests/test_mcp_custom_interceptors.py::test_custom_interceptor_coexists_with_oauth_interceptor
tests/test_mcp_custom_interceptors.py::test_mcp_interceptors_single_string_is_normalized
tests/test_mcp_custom_interceptors.py::test_mcp_interceptors_invalid_type_logs_warning
tests/test_mcp_custom_interceptors.py::test_custom_interceptor_non_callable_return_logs_warning
tests/test_mcp_routing_metadata.py::test_get_mcp_tools_tags_effective_routing_metadata[http]
tests/test_mcp_routing_metadata.py::test_get_mcp_tools_tags_effective_routing_metadata[stdio]
tests/test_mcp_session_pool.py::test_http_transport_tools_not_pooled
tests/test_mcp_session_pool.py::test_non_stdio_tool_call_timeout_warns_that_it_is_ignored
tests/test_mcp_session_pool.py::test_stdio_tool_call_timeout_does_not_raise_typeerror
tests/test_mcp_session_pool.py::test_mcp_tools_routed_to_source_server_with_prefix_overlap
tests/test_mcp_sync_wrapper.py::test_mcp_tool_sync_wrapper_generation
tests/test_mcp_sync_wrapper.py::test_mcp_tool_loading_skips_failed_server
tests/test_persistence_autogen_script.py::test_autogen_builds_temp_db_at_head_without_data_dir
tests/test_persistence_autogen_script.py::test_autogen_temp_db_is_at_head
tests/test_persistence_autogen_script.py::test_autogen_temp_db_comes_from_migration_history_not_current_metadata
tests/test_project_governance_schema_postgres.py::test_m2_schema_has_governance_constraints
tests/test_project_schema_postgres.py::test_downgrade_with_project_data_fails_without_mutation[False]
tests/test_project_schema_postgres.py::test_downgrade_with_project_data_fails_without_mutation[True]
tests/test_project_schema_postgres.py::test_downgrade_with_empty_project_tables_returns_to_0004
tests/test_replay_golden.py::test_replay_write_read_file_ultra_matches_golden
tests/test_sandbox_search_tools.py::test_ls_tool_masks_skills_host_paths
tests/test_sandbox_search_tools.py::test_ls_tool_skills_path_uses_sandbox_mapping_user_id_not_contextvar
tests/test_setup_agent_tool.py::test_setup_agent_rejects_invalid_agent_name_before_writing
tests/test_setup_agent_tool.py::test_setup_agent_rejects_absolute_agent_name_before_writing
tests/test_setup_agent_tool.py::TestSetupAgentNoDataLoss::test_existing_agent_dir_preserved_on_failure
tests/test_setup_agent_tool.py::TestSetupAgentNoDataLoss::test_new_agent_dir_cleaned_up_on_failure
tests/test_setup_agent_tool.py::TestSetupAgentNoDataLoss::test_successful_setup_creates_files
tests/test_setup_agent_tool.py::TestSetupAgentNoDataLoss::test_runtime_user_id_used_when_contextvar_missing
tests/test_setup_agent_tool.py::TestSetupAgentEmptySoulGuard::test_empty_soul_returns_error_and_does_not_write
tests/test_setup_agent_tool.py::TestSetupAgentEmptySoulGuard::test_whitespace_only_soul_returns_error_and_does_not_write
tests/test_setup_agent_tool.py::TestSetupAgentEmptySoulGuard::test_empty_soul_does_not_overwrite_existing_global_soul
tests/test_setup_agent_tool.py::TestSetupAgentEmptySoulGuard::test_empty_soul_does_not_overwrite_existing_per_agent_soul
tests/test_skill_container_path_defaults.py::test_mnt_skills_literal_is_owned_by_skill_constants_module
tests/test_sqlite_to_postgres_migration.py::test_real_postgres_two_source_user_reconciliation_and_ledger_replay
tests/test_task_tool_core_logic.py::test_task_tool_builds_catalog_tools_off_provider_owner_loop
tests/test_update_agent_tool.py::test_update_agent_rejects_missing_agent_name
tests/test_update_agent_tool.py::test_update_agent_rejects_invalid_agent_name
tests/test_update_agent_tool.py::test_update_agent_rejects_unknown_agent
tests/test_update_agent_tool.py::test_update_agent_requires_at_least_one_field
tests/test_update_agent_tool.py::test_update_agent_rejects_unknown_model
tests/test_update_agent_tool.py::test_update_agent_accepts_known_model
tests/test_update_agent_tool.py::test_update_agent_treats_nullish_optional_text_as_omitted
tests/test_update_agent_tool.py::test_update_agent_treats_nullish_string_list_fields_as_omitted
tests/test_update_agent_tool.py::test_update_agent_updates_soul_only
tests/test_update_agent_tool.py::test_update_agent_updates_description_only
tests/test_update_agent_tool.py::test_update_agent_preserves_github_block_on_description_change
tests/test_update_agent_tool.py::test_update_agent_skills_empty_list_disables_all
tests/test_update_agent_tool.py::test_update_agent_skills_omitted_keeps_existing
tests/test_update_agent_tool.py::test_update_agent_no_op_when_values_match_existing
tests/test_update_agent_tool.py::test_update_agent_forces_name_to_directory
tests/test_update_agent_tool.py::test_update_agent_failure_preserves_existing_files
tests/test_update_agent_tool.py::test_update_agent_soul_failure_does_not_replace_config
tests/test_update_agent_tool.py::test_update_agent_only_writes_under_current_user
tests/test_update_agent_tool.py::test_update_agent_round_trips_known_fields
tests/test_update_agent_tool.py::test_update_agent_refuses_on_webhook_channel
tests/test_update_agent_tool.py::test_update_agent_proceeds_on_non_webhook_channel
```

### Static verification

```text
Ruff check: All checks passed for 7 modified Python files
Ruff format: 7 files already formatted
git diff --check: passed
Production /api/langgraph, make_stream_bridge, and Gateway RunManager scan: zero hits
Deleted Gateway service import scan: only the intentional source-absence assertion remains
Old services.py::start_run and Gateway-owned run wording scan: zero hits
```

The complete suite was not rerun after deleting the 13 direct stale tests because the frozen instruction required recording and stopping on unrelated later-task failures. Focused Task 3 gates, complete collection, static checks, and the exact remaining-node audit were rerun after that cleanup.
