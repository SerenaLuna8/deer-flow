"""Batch 6 secondary-hotspot compatibility contracts.

Characterization tests that pass on the untouched Job, Run Snapshot,
Checkpointer, and Summarization baselines and keep passing while their
legacy modules delegate to owning modules.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
from pathlib import Path

import pytest

import deerflow.persistence.jobs as jobs_package
from app.private_work import checkpointer as checkpointer_legacy
from app.private_work import snapshot_repository as snapshot_legacy
from deerflow.agents.middlewares import summarization_middleware as summarization_legacy
from deerflow.persistence.jobs import sql as jobs_sql_legacy

SELF_PATH = Path(__file__).resolve()
BACKEND_ROOT = SELF_PATH.parents[1]
TESTS_ROOT = BACKEND_ROOT / "tests"
HARNESS_ROOT = BACKEND_ROOT / "packages" / "harness" / "deerflow"
JOBS_ROOT = HARNESS_ROOT / "persistence" / "jobs"
MIDDLEWARES_ROOT = HARNESS_ROOT / "agents" / "middlewares"
PRIVATE_WORK_ROOT = BACKEND_ROOT / "app" / "private_work"
JOBS_SQL_PATH = JOBS_ROOT / "sql.py"
SNAPSHOT_PATH = PRIVATE_WORK_ROOT / "snapshot_repository.py"
CHECKPOINTER_PATH = PRIVATE_WORK_ROOT / "checkpointer.py"
SUMMARIZATION_PATH = MIDDLEWARES_ROOT / "summarization_middleware.py"
JOBS_SQL_MODULE = "deerflow.persistence.jobs.sql"
SNAPSHOT_MODULE = "app.private_work.snapshot_repository"
CHECKPOINTER_MODULE = "app.private_work.checkpointer"
SUMMARIZATION_MODULE = "deerflow.agents.middlewares.summarization_middleware"
JOBS_OWNER_MODULES = ("contracts",)
SNAPSHOT_OWNER_MODULES = ("snapshot_contracts", "snapshot_admission_rules")
CHECKPOINTER_OWNER_MODULES = ("checkpoint_receipt_repair",)
SUMMARIZATION_OWNER_MODULES = ("snip_planner", "turn_compaction", "compaction_receipts")

EXPECTED_JOB_REPOSITORY_INIT_PARAMETERS = ("self", "session", "owner_ref_hasher", "terminal_port")
EXPECTED_SNAPSHOT_REPOSITORY_INIT_PARAMETERS = (
    "self",
    "session_factory",
    "model_ref_resolver",
    "model_catalog",
    "runtime_policy",
    "endpoint_policy",
    "personalization_repository_builder",
    "audit",
)
EXPECTED_PROJECT_CHECKPOINTER_INIT_PARAMETERS = ("self", "raw_saver", "session_factory", "quota", "approval_audit", "run_event_store")
EXPECTED_SCOPED_SAVER_INIT_PARAMETERS = (
    "self",
    "raw_saver",
    "session_factory",
    "context",
    "owner_loop",
    "quota",
    "approval_audit",
    "run_event_store",
    "thread_kind",
)
EXPECTED_SUMMARIZATION_INIT_PARAMETERS = (
    "self",
    "args",
    "compact_all_complete_turns",
    "context_model",
    "context_compaction_observer",
    "dual_output_contract",
    "kwargs",
)
EXPECTED_CREATE_SUMMARIZATION_PARAMETERS = ("app_config", "context_model", "keep", "context_compaction_observer")
EXPECTED_FREEZE_PROFILE_PARAMETERS = ("middlewares", "profile")
EXPECTED_JOBS_SQL_ALL = [
    "DurableDeadTerminalReconciliationRequest",
    "DurableTerminalSuccessorRebindRequest",
    "DurableTerminalTakeoverRequest",
    "EnqueueJob",
    "DeadJobRecord",
    "DeadJobRequeuedEvent",
    "JobAuditPort",
    "JobClaim",
    "JobHeartbeat",
    "JobIdempotencyConflict",
    "JobOwnerRef",
    "JobOwnerRefRequired",
    "JobRepository",
    "JobRetryResult",
    "JobRequeueForbidden",
    "JobScope",
    "JobTerminalResult",
    "JobType",
    "JobUnstartedClaimRelease",
    "RetrySafety",
    "RetentionPurgeJobAuthority",
    "consume_issued_dead_job_requeued_event",
    "retry_backoff_seconds",
]
EXPECTED_CONTEXT_COMPACTION_RESULT_FIELDS = ("summary_text", "messages_to_summarize", "preserved_messages", "total_tokens", "memory_archive_receipt")
EXPECTED_PREPARED_COMPACTION_FIELDS = ("source_messages", "snip_messages", "preserved_messages", "previous_summary", "total_tokens")
EXPECTED_SNIP_PROMPT_PLAN_FIELDS = ("prompts", "hierarchical")
EXPECTED_SNIP_SUMMARY_FIELDS = ("continuity", "tagged_text")
EXPECTED_RUN_MCP_SECRET_SNAPSHOT_FIELDS = ("mcp_server_id", "mcp_server_version_id", "slot_id", "secret_revision", "secret_generation_id", "secret_generation_digest")
EXPECTED_RUN_SKILL_SECRET_SNAPSHOT_FIELDS = ("skill_id", "skill_version_id", "secret_name", "secret_revision", "secret_generation_id", "secret_generation_digest")
EXPECTED_ENQUEUE_JOB_FIELDS = (
    "job_type",
    "scope",
    "idempotency_key",
    "run_id",
    "occurrence_id",
    "max_attempts",
    "owner_private_generation",
    "namespace",
    "origin_trace_id",
    "retry_safety",
    "priority",
    "available_at",
    "predecessor_dead_job_id",
    "execution_domain_affinity",
)
EXPECTED_JOB_CLAIM_FIELDS = (
    "job_id",
    "attempt_id",
    "lease_token",
    "job_type",
    "scope",
    "run_id",
    "occurrence_id",
    "retry_safety",
    "cancel_requested",
    "namespace",
    "origin_trace_id",
    "execution_domain_affinity",
    "predecessor_dead_job_id",
    "settlement_only",
)

JOBS_SQL_COMPATIBILITY_NAMES = frozenset(
    {
        "DeadJobRecord",
        "DeadJobRequeuedEvent",
        "DurableDeadTerminalReconciliationRequest",
        "DurableTerminalSuccessorRebindRequest",
        "DurableTerminalTakeoverRequest",
        "EnqueueJob",
        "JobAuditPort",
        "JobClaim",
        "JobHeartbeat",
        "JobIdempotencyConflict",
        "JobOwnerRef",
        "JobOwnerRefRequired",
        "JobRepository",
        "JobRequeueForbidden",
        "JobScope",
        "JobTerminalEvent",
        "JobTerminalPort",
        "JobTerminalResult",
        "JobType",
        "JobUnstartedClaimRelease",
        "RetentionPurgeJobAuthority",
        "RetrySafety",
        "consume_issued_dead_job_requeued_event",
        "_dead_error_code_for_failure",
        "datetime",
    }
)
JOBS_CONTRACT_NAMES = (
    "JobType",
    "RetrySafety",
    "_SHA256_HEX",
    "_DETERMINISTIC_NONRETRYABLE_ERROR_CODES",
    "_durable_terminal_successor_idempotency_key",
    "_dead_error_code_for_failure",
    "JobScope",
    "RetentionPurgeJobAuthority",
    "EnqueueJob",
    "JobClaim",
    "JobHeartbeat",
    "JobUnstartedClaimRelease",
    "JobOwnerRef",
    "DeadJobRecord",
    "DeadJobRequeuedEvent",
    "JobTerminalEvent",
    "JobTerminalResult",
    "DurableTerminalTakeoverRequest",
    "DurableDeadTerminalReconciliationRequest",
    "DurableTerminalSuccessorRebindRequest",
    "JobRetryResult",
    "JobAuditPort",
    "JobTerminalPort",
    "JobIdempotencyConflict",
    "JobOwnerRefRequired",
    "JobRequeueForbidden",
    "retry_backoff_seconds",
)
JOBS_SQL_RETAINED_NAMES = (
    "_is_durable_terminal_successor",
    "_lease_token_hash",
    "_ISSUED_REQUEUE_EVENTS",
    "_ISSUED_REQUEUE_EVENTS_LOCK",
    "_DeadTerminalReconciliationCursor",
    "_DEAD_TERMINAL_RECONCILIATION_PAGE_SIZE",
    "_DEAD_TERMINAL_RECONCILIATION_CURSORS",
    "_DEAD_TERMINAL_RECONCILIATION_CURSORS_LOCK",
    "_dead_terminal_reconciliation_cursor",
    "_advance_dead_terminal_reconciliation_cursor",
    "consume_issued_dead_job_requeued_event",
    "JobRepository",
)

SNAPSHOT_COMPATIBILITY_NAMES = frozenset(
    {
        "RunSnapshotRepository",
        "RunSnapshotAssetStale",
        "RunMcpSecretSnapshot",
        "RunModelSnapshotAdmissionPort",
        "RunRuntimePolicyAdmissionPort",
        "agent_model_snapshot_purpose",
        "_apply_runtime_recursion_limit",
        "_RunAssetSnapshotAdmissionEncoder",
        "AsyncSession",
        "PrivateRunRepository",
        "PrivateThreadRepository",
        "require_active_run_skill_writer_cohort",
        "encode_run_asset_snapshot",
        "encoded_run_asset_snapshot_json_size",
    }
)
SNAPSHOT_CONTRACT_NAMES = (
    "RunSnapshotAssetStale",
    "RunMcpSecretSnapshot",
    "RunSkillSecretSnapshot",
    "AdmittedRunModelSnapshot",
    "RunModelSnapshotAdmissionPort",
    "RunRuntimePolicyAdmissionPort",
    "agent_model_snapshot_purpose",
)
SNAPSHOT_RULE_MODULE_NAMES = (
    "_FORBIDDEN_PERSISTED_KEY_PARTS",
    "_reject_secret_bearing_keys",
    "_apply_runtime_recursion_limit",
    "_RunAssetSnapshotAdmissionEncoder",
    "_r1_snapshot_schema_version",
)
# legacy staticmethod name on RunSnapshotRepository -> owner function name
SNAPSHOT_RULE_STATICMETHODS = {
    "_asset_allowed": "asset_allowed",
    "_validate_project_mcp_secret_slots": "validate_project_mcp_secret_slots",
    "_validate_dependency_snapshots": "validate_dependency_snapshots",
    "_validate_main_dependency_boundary": "validate_main_dependency_boundary",
}
EXPECTED_SNAPSHOT_ADMISSION_AWAITS = (
    "require_active_run_skill_writer_cohort",
    "admit_current_upload_snapshot",
    "validate_run_asset_closure_in_session",
    "validate_agent_closure_in_session",
    "prepare",
    "lock_agent_runtime_for_admission",
    "_continuation_workload_profile",
    "create_for_snapshot_assembly",
    "scalar",
    "admit_run_snapshot",
    "admit_model_snapshot",
    "admit_model_snapshot",
    "admit_model_snapshot",
    "update_admitted_execution_profile",
    "get",
    "_admit_memory_context_snapshot",
    "flush",
    "seal_asset_closure",
    "touch_activity",
)

CHECKPOINTER_COMPATIBILITY_NAMES = frozenset(
    {
        "ProjectScopedCheckpointer",
        "PRIVATE_SCOPE_MARKER",
        "_ScopedCheckpointSaver",
        "ContextEvidenceRepository",
        "ContextProjectionTransaction",
        "transition_output_delivery_obligation_for_approval_terminal",
        "PrivateWorkRevalidator",
        "PrivateThreadRepository",
    }
)
# legacy _ScopedCheckpointSaver attribute -> owner function name
CHECKPOINT_REPAIR_STATICMETHODS = {
    "_thread_id": "thread_id_from_config",
    "_checkpoint_id": "checkpoint_id_from_config",
    "_validate_context_provider_checkpoint_response": "validate_context_provider_checkpoint_response",
}
CHECKPOINT_REPAIR_DELEGATING_METHODS = (
    "_memory_history_activation",
    "_repair_memory_archive_receipt",
    "_context_compaction_activation",
    "_repair_context_compaction_receipt",
    "_context_provider_checkpoint_activation",
    "_repair_context_provider_checkpoint",
)
CHECKPOINT_REPAIR_OWNER_NAMES = (
    "_MEMORY_ARCHIVE_RECEIPT_FIELDS",
    "thread_id_from_config",
    "checkpoint_id_from_config",
    "memory_history_activation",
    "repair_memory_archive_receipt",
    "context_compaction_activation",
    "repair_context_compaction_receipt",
    "context_provider_checkpoint_activation",
    "validate_context_provider_checkpoint_response",
    "repair_context_provider_checkpoint",
)

SUMMARIZATION_COMPATIBILITY_NAMES = frozenset(
    {
        "DeerFlowSummarizationMiddleware",
        "create_summarization_middleware",
        "freeze_summarization_profile",
        "SnipCompactionFailed",
        "SnipModelOutputInvalid",
        "SnipPromptBudgetTooSmall",
        "SnipSourceTooLarge",
        "ContextCompactionResult",
        "MIN_SNIP_SUMMARY_OUTPUT_TOKENS",
        "MAX_SNIP_HIERARCHICAL_MODEL_CALLS",
        "_ensure_snip_summary_output_budget",
        "ModelRuntime",
        "ModelRuntimeProfile",
    }
)
SNIP_PLANNER_NAMES = (
    "MIN_SNIP_SUMMARY_OUTPUT_TOKENS",
    "MAX_SNIP_HIERARCHICAL_LEAVES",
    "MAX_SNIP_HIERARCHICAL_MODEL_CALLS",
    "SnipPromptBudgetTooSmall",
    "SnipSourceTooLarge",
    "SnipCompactionFailed",
    "SnipModelOutputInvalid",
    "_ensure_snip_summary_output_budget",
    "_SnipSummary",
    "_ProjectionField",
    "_ProjectionFragment",
    "_SnipPromptPlan",
    "_SnipReductionStep",
)
TURN_COMPACTION_NAMES = (
    "_SUMMARY_TRIGGER_MESSAGE_NAME",
    "_ASK_CLARIFICATION_TOOL_NAME",
    "_PreparedCompaction",
)
COMPACTION_RECEIPT_NAMES = (
    "ContextCompactionResult",
    "_resolve_thread_id",
)
# legacy DeerFlowSummarizationMiddleware attribute -> (owner module, owner function)
SUMMARIZATION_STATICMETHOD_ALIASES = {
    "_complete_turn_ranges": ("turn_compaction", "complete_turn_ranges"),
    "_candidate_cutoffs": ("turn_compaction", "candidate_cutoffs"),
    "_snip_messages": ("turn_compaction", "snip_messages"),
    "_summary_count_message": ("turn_compaction", "summary_count_message"),
    "_messages_for_trigger_count": ("turn_compaction", "messages_for_trigger_count"),
    "_context_progress": ("turn_compaction", "context_progress"),
}
SUMMARIZATION_DELEGATING_METHODS = (
    "_plan_reduction_step",
    "_build_snip_prompt_plan",
    "_build_summary_prompt",
    "_receipt",
    "_require_receipt_preconditions",
    "_context_compaction_update",
    "_acontext_compaction_update",
)
SUMMARIZATION_RETAINED_METHODS = (
    "__init__",
    "_get_profile_limits",
    "_parse_snip_response",
    "_create_summary",
    "_acreate_summary",
    "_prompt_within_budget",
    "_prompt_with_repair_within_budget",
    "_invoke_snip_prompt",
    "_ainvoke_snip_prompt",
    "_reduce_snip_summaries",
    "_areduce_snip_summaries",
    "_summarize_with",
    "_asummarize_with",
    "measure_context_usage",
    "_measure_trigger_usage",
    "measure_provider_request_usage",
    "_count_rendered_summary_prompt_tokens",
    "before_model",
    "abefore_model",
    "_requested_cutoff",
    "_provider_safe_retention_cutoff",
    "_compacted_result_fits_provider_profile",
    "_overbudget_progress_cutoff",
    "_profile_trigger_reached",
    "fixed_component_over_trigger",
    "_prepare_compaction",
    "compact_state",
    "acompact_state",
    "_maybe_summarize",
    "_amaybe_summarize",
)
SUMMARIZATION_MOVED_INTERNALS = (
    "_intermediate_summary_text",
    "_reduction_prompt",
    "_assemble_summary_input_text",
    "_build_summary_input_text",
    "_build_summary_prompt_from_formatted",
    "_projection_text",
    "_quoted_projection_value",
    "_projection_material",
    "_render_projection",
    "_projection_prompt",
    "_fit_projection_prefix",
    "_build_projection_prompts",
    "_is_turn_user",
    "_turn_prefix_start",
    "_clarification_request_tool_call_id",
    "_is_clarification_continuation",
    "_archive_context",
    "_source_checkpoint_id",
    "_resolve_compaction_estimator",
    "_context_state_digest",
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _legacy_test_consumers(module_name: str) -> frozenset[str]:
    """Names imported from, patched on, or read as attributes of ``module_name`` across pre-existing tests/."""

    observed: set[str] = set()
    for path in TESTS_ROOT.rglob("*.py"):
        if path.resolve() == SELF_PATH:
            continue
        tree = _parse(path)
        aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update(alias.asname or alias.name for alias in node.names if alias.name == module_name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module == module_name:
                    observed.update(alias.name for alias in node.names)
                else:
                    aliases.update(alias.asname or alias.name for alias in node.names if f"{node.module}.{alias.name}" == module_name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
                observed.add(node.attr)
                continue
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            is_setattr = isinstance(callee, ast.Attribute) and callee.attr == "setattr"
            is_patch = (isinstance(callee, ast.Name) and callee.id == "patch") or (isinstance(callee, ast.Attribute) and callee.attr == "patch")
            if not (is_setattr or is_patch) or not node.args:
                continue
            target = node.args[0]
            if isinstance(target, ast.Constant) and isinstance(target.value, str) and target.value.startswith(f"{module_name}."):
                observed.add(target.value.removeprefix(f"{module_name}.").split(".")[0])
            elif isinstance(target, ast.Name) and target.id in aliases and len(node.args) > 1:
                attribute = node.args[1]
                if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
                    observed.add(attribute.value)
    return frozenset(observed)


def _function_node(path: Path, function_name: str, *, class_name: str | None = None) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = _parse(path)
    scopes: list[ast.AST] = [tree]
    if class_name is not None:
        scopes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
    for scope in scopes:
        for node in ast.walk(scope):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
                return node
    raise AssertionError(f"{function_name} not found in {path.name}")


def _await_callee_names(function: ast.AST) -> tuple[str, ...]:
    """Callee names of every ``await <call>`` in source order."""

    awaited: list[tuple[int, int, str]] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        callee = node.value.func
        if isinstance(callee, ast.Attribute):
            awaited.append((node.lineno, node.col_offset, callee.attr))
        elif isinstance(callee, ast.Name):
            awaited.append((node.lineno, node.col_offset, callee.id))
    return tuple(name for _, _, name in sorted(awaited))


def _module_imports(path: Path) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            if node.module:
                imports.add(f"{prefix}{node.module}")
                imports.update(f"{prefix}{node.module}.{alias.name}" for alias in node.names)
            elif node.level > 0:
                imports.add(prefix)
                imports.update(f"{prefix}{alias.name}" for alias in node.names)
    return imports


def _class_defined_names(path: Path, class_name: str) -> frozenset[str]:
    """Attribute names bound directly in ``class_name``'s body (defs and assignments)."""

    tree = _parse(path)
    class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    names: set[str] = set()
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return frozenset(names)


def _assert_owner_imports_are_clean(path: Path, *, forbidden_modules: tuple[str, ...], forbidden_prefixes: tuple[str, ...]) -> None:
    imports = _module_imports(path)
    offenders = {module for module in imports if module in forbidden_modules or any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden_prefixes)}
    assert offenders == set(), (path.name, sorted(offenders))


def test_batch6_jobs_public_shapes_are_frozen() -> None:
    assert jobs_sql_legacy.__all__ == EXPECTED_JOBS_SQL_ALL
    assert tuple(inspect.signature(jobs_sql_legacy.JobRepository.__init__).parameters) == EXPECTED_JOB_REPOSITORY_INIT_PARAMETERS
    assert tuple(field.name for field in dataclasses.fields(jobs_sql_legacy.EnqueueJob)) == EXPECTED_ENQUEUE_JOB_FIELDS
    assert tuple(field.name for field in dataclasses.fields(jobs_sql_legacy.JobClaim)) == EXPECTED_JOB_CLAIM_FIELDS
    for name in jobs_package.__all__:
        assert hasattr(jobs_package, name), name
    assert jobs_package.JobRepository is jobs_sql_legacy.JobRepository
    assert jobs_package.EnqueueJob is jobs_sql_legacy.EnqueueJob
    assert callable(jobs_sql_legacy.JobTerminalEvent) and callable(jobs_sql_legacy.JobTerminalPort)


def test_batch6_snapshot_public_shapes_are_frozen() -> None:
    assert tuple(inspect.signature(snapshot_legacy.RunSnapshotRepository.__init__).parameters) == EXPECTED_SNAPSHOT_REPOSITORY_INIT_PARAMETERS
    assert tuple(field.name for field in dataclasses.fields(snapshot_legacy.RunMcpSecretSnapshot)) == EXPECTED_RUN_MCP_SECRET_SNAPSHOT_FIELDS
    assert tuple(field.name for field in dataclasses.fields(snapshot_legacy.RunSkillSecretSnapshot)) == EXPECTED_RUN_SKILL_SECRET_SNAPSHOT_FIELDS
    assert issubclass(snapshot_legacy.RunSnapshotAssetStale, Exception)
    assert not hasattr(snapshot_legacy, "__all__")


def test_batch6_checkpointer_public_shapes_are_frozen() -> None:
    assert tuple(inspect.signature(checkpointer_legacy.ProjectScopedCheckpointer.__init__).parameters) == EXPECTED_PROJECT_CHECKPOINTER_INIT_PARAMETERS
    assert tuple(inspect.signature(checkpointer_legacy._ScopedCheckpointSaver.__init__).parameters) == EXPECTED_SCOPED_SAVER_INIT_PARAMETERS
    assert checkpointer_legacy.PRIVATE_SCOPE_MARKER == "deerflow_private_scope"
    assert not hasattr(checkpointer_legacy, "__all__")


def test_batch6_summarization_public_shapes_are_frozen() -> None:
    middleware = summarization_legacy.DeerFlowSummarizationMiddleware
    assert tuple(inspect.signature(middleware.__init__).parameters) == EXPECTED_SUMMARIZATION_INIT_PARAMETERS
    assert tuple(inspect.signature(summarization_legacy.create_summarization_middleware).parameters) == EXPECTED_CREATE_SUMMARIZATION_PARAMETERS
    assert tuple(inspect.signature(summarization_legacy.freeze_summarization_profile).parameters) == EXPECTED_FREEZE_PROFILE_PARAMETERS
    assert tuple(field.name for field in dataclasses.fields(summarization_legacy.ContextCompactionResult)) == EXPECTED_CONTEXT_COMPACTION_RESULT_FIELDS
    assert tuple(field.name for field in dataclasses.fields(summarization_legacy._PreparedCompaction)) == EXPECTED_PREPARED_COMPACTION_FIELDS
    assert tuple(field.name for field in dataclasses.fields(summarization_legacy._SnipPromptPlan)) == EXPECTED_SNIP_PROMPT_PLAN_FIELDS
    assert tuple(field.name for field in dataclasses.fields(summarization_legacy._SnipSummary)) == EXPECTED_SNIP_SUMMARY_FIELDS
    assert not hasattr(summarization_legacy, "__all__")


def test_batch6_jobs_sql_test_consumers_stay_within_the_frozen_inventory() -> None:
    observed = _legacy_test_consumers(JOBS_SQL_MODULE)
    assert observed <= JOBS_SQL_COMPATIBILITY_NAMES, observed - JOBS_SQL_COMPATIBILITY_NAMES
    for name in JOBS_SQL_COMPATIBILITY_NAMES - {"datetime"}:
        assert hasattr(jobs_sql_legacy, name), name


def test_batch6_snapshot_test_consumers_stay_within_the_frozen_inventory() -> None:
    observed = _legacy_test_consumers(SNAPSHOT_MODULE)
    assert observed <= SNAPSHOT_COMPATIBILITY_NAMES, observed - SNAPSHOT_COMPATIBILITY_NAMES
    for name in SNAPSHOT_CONTRACT_NAMES + SNAPSHOT_RULE_MODULE_NAMES:
        assert hasattr(snapshot_legacy, name), name
    for name in SNAPSHOT_RULE_STATICMETHODS:
        assert isinstance(snapshot_legacy.RunSnapshotRepository.__dict__[name], staticmethod), name


def test_batch6_snapshot_admission_await_order_is_frozen() -> None:
    function = _function_node(SNAPSHOT_PATH, "create_run_with_snapshot_in_session", class_name="RunSnapshotRepository")
    assert _await_callee_names(function) == EXPECTED_SNAPSHOT_ADMISSION_AWAITS
    source = inspect.getsource(snapshot_legacy.RunSnapshotRepository.create_run_with_snapshot_in_session)
    assert "require_active_run_skill_writer_cohort" in source and "PrivateWorkUnavailable" in source


def test_batch6_checkpointer_test_consumers_stay_within_the_frozen_inventory() -> None:
    observed = _legacy_test_consumers(CHECKPOINTER_MODULE)
    assert observed <= CHECKPOINTER_COMPATIBILITY_NAMES, observed - CHECKPOINTER_COMPATIBILITY_NAMES
    saver_names = _class_defined_names(CHECKPOINTER_PATH, "_ScopedCheckpointSaver")
    for name in tuple(CHECKPOINT_REPAIR_STATICMETHODS) + CHECKPOINT_REPAIR_DELEGATING_METHODS:
        assert name in saver_names, name
    for name in CHECKPOINT_REPAIR_STATICMETHODS:
        assert isinstance(checkpointer_legacy._ScopedCheckpointSaver.__dict__[name], staticmethod), name


def test_batch6_summarization_test_consumers_stay_within_the_frozen_inventory() -> None:
    observed = _legacy_test_consumers(SUMMARIZATION_MODULE)
    assert observed <= SUMMARIZATION_COMPATIBILITY_NAMES, observed - SUMMARIZATION_COMPATIBILITY_NAMES
    for name in SUMMARIZATION_COMPATIBILITY_NAMES:
        assert hasattr(summarization_legacy, name), name
    class_names = _class_defined_names(SUMMARIZATION_PATH, "DeerFlowSummarizationMiddleware")
    for name in SUMMARIZATION_RETAINED_METHODS + SUMMARIZATION_DELEGATING_METHODS + tuple(SUMMARIZATION_STATICMETHOD_ALIASES):
        assert name in class_names, name
    assert "state_schema" in class_names


def test_batch6_summarization_seam_is_called_by_bare_name_in_the_factory() -> None:
    factory = _function_node(SUMMARIZATION_PATH, "create_summarization_middleware")
    called = {node.func.id for node in ast.walk(factory) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "_ensure_snip_summary_output_budget" in called


def test_batch6_owner_modules_never_import_facades_or_forbidden_packages() -> None:
    for name in JOBS_OWNER_MODULES:
        path = JOBS_ROOT / f"{name}.py"
        if path.exists():
            _assert_owner_imports_are_clean(path, forbidden_modules=(JOBS_SQL_MODULE, ".sql"), forbidden_prefixes=("app", "sqlalchemy.orm", "sqlalchemy.dialects"))
    for name in SUMMARIZATION_OWNER_MODULES:
        path = MIDDLEWARES_ROOT / f"{name}.py"
        if path.exists():
            _assert_owner_imports_are_clean(path, forbidden_modules=(SUMMARIZATION_MODULE, ".summarization_middleware"), forbidden_prefixes=("app", "sqlalchemy"))
    for name in SNAPSHOT_OWNER_MODULES:
        path = PRIVATE_WORK_ROOT / f"{name}.py"
        if path.exists():
            _assert_owner_imports_are_clean(path, forbidden_modules=(SNAPSHOT_MODULE, ".snapshot_repository"), forbidden_prefixes=())
    for name in CHECKPOINTER_OWNER_MODULES:
        path = PRIVATE_WORK_ROOT / f"{name}.py"
        if path.exists():
            _assert_owner_imports_are_clean(path, forbidden_modules=(CHECKPOINTER_MODULE, ".checkpointer"), forbidden_prefixes=())


def test_jobs_contracts_owner_is_the_exact_legacy_export() -> None:
    owner = importlib.import_module("deerflow.persistence.jobs.contracts")
    for name in JOBS_CONTRACT_NAMES:
        assert getattr(jobs_sql_legacy, name) is getattr(owner, name), name
    for name in JOBS_SQL_RETAINED_NAMES:
        assert hasattr(jobs_sql_legacy, name), name
        assert not hasattr(owner, name), name
    assert jobs_sql_legacy.__all__ == EXPECTED_JOBS_SQL_ALL
    for name in jobs_package.__all__:
        if name.endswith("Row"):
            continue
        assert getattr(jobs_package, name) is getattr(jobs_sql_legacy, name), name


def test_jobs_contracts_requeue_event_registry_stays_with_the_repository() -> None:
    owner = importlib.import_module("deerflow.persistence.jobs.contracts")
    event = object.__new__(owner.DeadJobRequeuedEvent)
    assert jobs_sql_legacy.consume_issued_dead_job_requeued_event(event) is False
    assert jobs_sql_legacy.consume_issued_dead_job_requeued_event(object()) is False
    assert jobs_sql_legacy.DeadJobRequeuedEvent is owner.DeadJobRequeuedEvent


def test_jobs_contracts_retry_backoff_characterization() -> None:
    owner = importlib.import_module("deerflow.persistence.jobs.contracts")
    assert owner.retry_backoff_seconds is jobs_sql_legacy.retry_backoff_seconds
    assert owner.retry_backoff_seconds(attempt_count=1, initial_seconds=5, max_seconds=60) == 5
    assert owner.retry_backoff_seconds(attempt_count=3, initial_seconds=5, max_seconds=60) == 20
    assert owner.retry_backoff_seconds(attempt_count=10, initial_seconds=5, max_seconds=60) == 60
    with pytest.raises(ValueError, match="invalid retry backoff inputs"):
        owner.retry_backoff_seconds(attempt_count=0, initial_seconds=5, max_seconds=60)


def test_snapshot_owners_are_the_exact_legacy_exports() -> None:
    contracts = importlib.import_module("app.private_work.snapshot_contracts")
    rules = importlib.import_module("app.private_work.snapshot_admission_rules")
    for name in SNAPSHOT_CONTRACT_NAMES:
        assert getattr(snapshot_legacy, name) is getattr(contracts, name), name
    for name in SNAPSHOT_RULE_MODULE_NAMES:
        assert getattr(snapshot_legacy, name) is getattr(rules, name), name
    for legacy_name, owner_name in SNAPSHOT_RULE_STATICMETHODS.items():
        descriptor = snapshot_legacy.RunSnapshotRepository.__dict__[legacy_name]
        assert isinstance(descriptor, staticmethod), legacy_name
        assert descriptor.__func__ is getattr(rules, owner_name), legacy_name
    assert rules.RunSnapshotAssetStale is contracts.RunSnapshotAssetStale
    rules_imports = _module_imports(PRIVATE_WORK_ROOT / "snapshot_admission_rules.py")
    assert "app.private_work.snapshot_contracts" in rules_imports
    assert not {SNAPSHOT_MODULE, ".snapshot_repository"} & rules_imports
    assert not {SNAPSHOT_MODULE, ".snapshot_repository", "app.private_work.snapshot_admission_rules"} & _module_imports(PRIVATE_WORK_ROOT / "snapshot_contracts.py")
