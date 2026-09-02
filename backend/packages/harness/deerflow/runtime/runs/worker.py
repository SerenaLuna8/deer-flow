"""Background agent execution.

Runs an agent graph inside an ``asyncio.Task``, publishing events to
a :class:`StreamBridge` as they are produced.

Uses ``graph.astream(stream_mode=[...])`` which gives correct full-state
snapshots for ``values`` mode, proper ``{node: writes}`` for ``updates``,
and ``(chunk, metadata)`` tuples for ``messages`` mode.

Note: ``events`` mode is not supported through the gateway — it requires
``graph.astream_events()`` which cannot simultaneously produce ``values``
snapshots.  The JS open-source LangGraph API server works around this via
internal checkpoint callbacks that are not exposed in the Python public API.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import os
import sys
import time  # noqa: F401 - compatibility export
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache, partial
from typing import Any, Literal, Protocol, cast

from langgraph.errors import GraphRecursionError
from langgraph.types import Overwrite

from deerflow.agents.goal_state import GoalEvaluation, GoalState
from deerflow.agents.middlewares.tool_call_control import (
    ResolvedGraphToolCallControlProfile,
    ToolCallControlLoopFinalizationFailed,
    ToolCallControlStateInvalid,
)
from deerflow.config.app_config import AppConfig, is_trace_correlation_enabled
from deerflow.config.database_config import CheckpointChannelMode
from deerflow.config.worker_config import DEFAULT_TEXT_DELTA_FLUSH_MS
from deerflow.error_codes import (
    RUN_EXECUTION_FAILED_ERROR_CODE,
    ContextProviderCallAmbiguousError,
    MemoryAuthorityUnavailable,
    PublicRunError,
    PublicRunErrorCode,
)
from deerflow.file_authority import RunFileAuthority
from deerflow.public_error_codes import llm_error_code_for_reason
from deerflow.runtime.checkpoint_mode import (
    CheckpointModeMismatchError,
    aensure_checkpoint_mode_compatible,
    inject_checkpoint_mode,
)
from deerflow.runtime.checkpoint_state import (
    CheckpointStateAccessor,
    build_state_mutation_graph,
    graph_state_schema,
)
from deerflow.runtime.context_carrier import RuntimeContextCarrier
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.events.models import STREAM_TERMINAL_ERROR_CODES
from deerflow.runtime.events.stream_base import StreamBridge
from deerflow.runtime.goal import (
    DEFAULT_MAX_GOAL_CONTINUATIONS,
    DEFAULT_MAX_NO_PROGRESS_CONTINUATIONS,
    GoalWriteConflict,
    _call_checkpointer_method,
    _is_visible_message,
    _message_type,
    attach_goal_evaluation,
    compute_no_progress_count,
    create_goal_evaluator_model,
    evaluate_goal_completion,
    goal_thread_lock,
    latest_visible_assistant_signature,
    make_goal_continuation_message,
    read_thread_goal,
    should_continue_goal,
    visible_conversation_signature,
    write_thread_goal,
)
from deerflow.runtime.host_execution_runner import (
    execute_frozen_host_execution_continuation,
)
from deerflow.runtime.recovered_llm_failures import (
    RunRecoveredLLMFailureRecorder,
)
from deerflow.runtime.serialization import serialize
from deerflow.runtime.user_context import DEFAULT_USER_ID, get_current_user, get_effective_user_id
from deerflow.sandbox.sandbox import (
    AUTHORIZATION_REVOKED_REASON,
    AuthorizationRevoked,
)
from deerflow.sandbox.sandbox_provider import RunScopedReadOnlyMount, get_sandbox_provider
from deerflow.sandbox.security import (
    HostBashExecutionMode,
    resolve_host_bash_execution_mode,
)
from deerflow.subagents.runtime_catalog import trusted_runtime_agent_catalog
from deerflow.token_budget_usage import TokenBudgetUsageRecorder
from deerflow.trace_context import get_current_trace_id, normalize_trace_id
from deerflow.tracing import inject_langfuse_metadata
from deerflow.utils.messages import message_to_text
from deerflow.workspace_changes import (
    WORKSPACE_CHANGES_EVENT_TYPE,
    WORKSPACE_CHANGES_METADATA_KEY,
    capture_workspace_snapshot,
    record_workspace_changes,
    trusted_workspace_change_result,
    workspace_change_event_content,
)
from deerflow.workspace_changes.types import WorkspaceSnapshot

from .checkpoint_rollback import (  # noqa: F401 - compatibility exports
    _ROLLBACK_SUCCEEDED_ERROR,
    RollbackPoint,
    _capture_rollback_point,
    _checkpoint_id,
    _checkpoint_messages_from_values_or_snapshot,
    _collect_pre_existing_message_ids,
    _collect_private_pre_existing_message_ids,
    _linearize_delta_checkpoint_resume,
    _materialized_checkpoint_messages,
    _materialized_checkpoint_snapshot,
    _message_id,
    _new_checkpoint_marker,
    _read_checkpoint_messages,
    _restore_pending_writes,
    _rollback_legacy_full_checkpoint,
    _rollback_point_from_legacy_snapshot,
    _rollback_to_pre_run_checkpoint,
    _settle_rollback,
    _snapshot_values,
)
from .execution_contracts import (
    RunAgentOutcome,
    RunAgentResourceOwnership,
    RunAgentUsageSnapshot,
    RunSemanticStopRecorder,
)
from .manager import RunManager, RunRecord
from .naming import resolve_root_run_name
from .private_file_lifecycle import PrivateFileLifecycle
from .schemas import RunStatus
from .stream_delivery import (  # noqa: F401 - compatibility exports
    _LLM_ERROR_FALLBACK_AUTHORITY_MODES,
    _MESSAGE_TRANSPORT_METADATA_KEYS,
    _TEXT_DELTA_FINISH_KEYS,
    _TEXT_DELTA_FLUSH_BYTES,
    _TEXT_DELTA_FLUSH_DUE,
    _TOOL_CALL_CHUNK_BATCH_SIZE,
    _VALID_LG_MODES,
    _contains_current_run_host_execution_approval,
    _current_run_host_execution_approval_id,
    _error_fallback_from_metadata,
    _extract_llm_error_fallback,
    _iter_with_text_delta_deadline,
    _lg_mode_to_sse_event,
    _LLMErrorFallback,
    _namespaced_sse_event,
    _normalize_stream_namespace,
    _PublicTokenUsageBridge,
    _publish_stream_item,
    _SubagentEventBuffer,
    _TextDeltaCoalescer,
    _ToolCallChunkBatcher,
    _try_extract_llm_error_fallback,
    _unpack_stream_item,
)

logger = logging.getLogger(__name__)

_PRIVATE_OUTPUT_NOT_PRESENTED_ERROR = "Run produced output files but did not present a current-run output"


def _repository_trace_user_id(record: RunRecord) -> str:
    """Resolve trace attribution without consulting runtime storage identity."""
    repository_user = get_current_user()
    if repository_user is not None:
        return str(repository_user.id)
    if record.user_id is not None:
        return str(record.user_id)
    return DEFAULT_USER_ID


def _build_runtime_context(
    thread_id: str,
    run_id: str,
    caller_context: Any | None,
    app_config: AppConfig | None = None,
    *,
    private_scope: object | None = None,
    authorization_checker: Callable[[], Awaitable[None]] | None = None,
    authorization_boundary: object | None = None,
    file_authority: object | None = None,
    memory_authority: object | None = None,
    guardrail_attribution: Mapping[str, object] | None = None,
    run_read_only_mounts: tuple[object, ...] = (),
    runtime_owner_user_id: str | None = None,
    memory_archive_context: object | None = None,
    host_execution_approval_port: object | None = None,
    channel_user_id: str | None = None,
    server_abort_event: object | None = None,
    vision_dispatch_authority: object | None = None,
    run_semantic_stop_recorder: RunSemanticStopRecorder | None = None,
    token_budget_usage_recorder: TokenBudgetUsageRecorder | None = None,
) -> dict[str, Any]:
    """Build the dict that becomes ``ToolRuntime.context`` for the run.

    Always includes ``thread_id`` and ``run_id``. Caller extension keys from
    ``config['context']`` (e.g. ``agent_name`` for the bootstrap flow — issue
    #2677) are merged, while reserved and server-owned keys are discarded. The
    resolved ``AppConfig`` is added by the worker so tools can consume it
    without ambient global lookups.

    langgraph 1.1+ surfaces this as ``runtime.context`` via the parent runtime stored
    under ``config['configurable']['__pregel_runtime']`` — see
    ``langgraph.pregel.main`` where ``parent_runtime.merge(...)`` is invoked.
    """
    runtime_context = RuntimeContextCarrier(
        thread_id=thread_id,
        run_id=run_id,
        app_config=app_config,
        user_id=runtime_owner_user_id,
        private_scope=private_scope,
        authorization_checker=authorization_checker,
        authorization_boundary=authorization_boundary,
        file_authority=file_authority,
        memory_authority=memory_authority,
        guardrail_attribution=guardrail_attribution,
        run_read_only_mounts=run_read_only_mounts or None,
        memory_archive_context=memory_archive_context,
        host_execution_approval_port=host_execution_approval_port,
        host_execution_agent_path=(("lead",) if host_execution_approval_port is not None else None),
        channel_user_id=channel_user_id,
        server_abort_event=server_abort_event,
        vision_dispatch_authority=vision_dispatch_authority,
        run_semantic_stop_recorder=run_semantic_stop_recorder,
        token_budget_usage_recorder=token_budget_usage_recorder,
    ).build(caller_context)
    if private_scope is not None and channel_user_id is None:
        # A private Run without verified IM identity must explicitly clear the
        # inherited host/sandbox variable instead of inheriting Worker state.
        runtime_context[RuntimeContextKeys.CHANNEL_USER_ID] = None
    return runtime_context


class PrivateAgentRuntime(Protocol):
    skill_root: Any
    skills: tuple[Any, ...]
    prompt_bundle: Any
    agent_catalog: Any

    async def materialize_skill_scoped_secrets(
        self,
        container_path: str,
        requested: object,
    ) -> dict[str, dict[str, str]]: ...
    async def aclose(self) -> None: ...


class PrivateRuntimeFactoryUnavailable(RuntimeError):
    """Raised when a private run cannot enter a private-runtime-aware factory."""


@dataclass(frozen=True)
class RunContext:
    """Infrastructure dependencies for a single agent run.

    Groups checkpointer, store, and persistence-related singletons so that
    ``run_agent`` (and any future callers) receive one object instead of a
    growing list of keyword arguments.
    """

    checkpointer: Any
    store: Any | None = field(default=None)
    event_store: Any | None = field(default=None)
    run_events_config: Any | None = field(default=None)
    thread_store: Any | None = field(default=None)
    app_config: AppConfig | None = field(default=None)
    on_run_completed: Any | None = field(default=None)
    private_scope: object | None = field(default=None)
    authorization_checker: Callable[[], Awaitable[None]] | None = field(default=None)
    authorization_boundary: object | None = field(default=None)
    file_authority: RunFileAuthority | None = field(default=None)
    memory_authority: object | None = field(default=None)
    memory_archive_context: object | None = field(default=None)
    guardrail_attribution: Mapping[str, object] | None = field(default=None)
    private_agent_runtime: PrivateAgentRuntime | None = field(default=None)
    host_execution_approval_port: object | None = field(default=None)
    channel_user_id: str | None = field(default=None)
    vision_dispatch_authority: object | None = field(default=None)
    token_budget_usage_recorder: TokenBudgetUsageRecorder | None = field(
        default=None,
    )
    resource_ownership: RunAgentResourceOwnership | None = field(default=None)
    tool_call_control_policy: ResolvedGraphToolCallControlProfile | None = field(
        default=None,
    )
    context_evidence_observer: object | None = field(default=None)
    max_concurrent_subagents: int | None = field(default=None)
    max_total_subagents: int | None = field(default=None)


def _checkpoint_runtime_settings(
    app_config: AppConfig | None,
) -> tuple[CheckpointChannelMode, int | None]:
    """Resolve the Worker checkpoint representation from its exact AppConfig.

    ``RunContext.app_config`` is the same immutable configuration supplied to
    the run-local Agent factory. Keeping both decisions on that object prevents
    a request config from selecting a different checkpoint representation.
    Minimal test/embedded contexts without a real ``AppConfig`` retain the
    historical full-mode behavior.
    """

    database = getattr(app_config, "database", None)
    raw_mode = getattr(database, "checkpoint_channel_mode", "full")
    mode: CheckpointChannelMode = cast(CheckpointChannelMode, raw_mode) if raw_mode in {"full", "delta"} else "full"
    delta = getattr(database, "checkpoint_delta", None)
    raw_frequency = getattr(delta, "snapshot_frequency", None)
    snapshot_frequency = raw_frequency if isinstance(raw_frequency, int) and not isinstance(raw_frequency, bool) and raw_frequency > 0 else None
    return mode, snapshot_frequency


def _install_runtime_context(config: dict, runtime_context: dict[str, Any]) -> None:
    existing_context = config.get("context")
    if isinstance(existing_context, dict):
        installed_context = existing_context
    else:
        installed_context = {}

    private_context = RuntimeContextKeys.PRIVATE_SCOPE in runtime_context or RuntimeContextKeys.RUN_READ_ONLY_MOUNTS in runtime_context
    public_identity_keys = {
        RuntimeContextKeys.THREAD_ID,
        RuntimeContextKeys.RUN_ID,
    }
    for key in tuple(installed_context):
        if not isinstance(key, str):
            installed_context.pop(key, None)
            continue
        if key.startswith(RuntimeContextKeys.RESERVED_PREFIX) or (key in RuntimeContextKeys.SERVER_OWNED_KEYS and (private_context or key not in public_identity_keys)):
            installed_context.pop(key, None)

    for key in public_identity_keys:
        if key not in runtime_context:
            continue
        if private_context:
            installed_context[key] = runtime_context[key]
        else:
            installed_context.setdefault(key, runtime_context[key])

    for key in (
        RuntimeContextKeys.INSTALL_KEYS
        - public_identity_keys
        - {
            RuntimeContextKeys.RUNTIME_AGENT_CATALOG,
        }
    ):
        if key in runtime_context:
            installed_context[key] = runtime_context[key]

    runtime_agent_catalog = trusted_runtime_agent_catalog(
        runtime_context.get(RuntimeContextKeys.RUNTIME_AGENT_CATALOG),
    )
    if runtime_agent_catalog is not None:
        installed_context[RuntimeContextKeys.RUNTIME_AGENT_CATALOG] = runtime_agent_catalog
    config["context"] = installed_context


def _compute_agent_factory_supports_app_config(agent_factory: Any) -> bool:
    try:
        return "app_config" in inspect.signature(agent_factory).parameters
    except (TypeError, ValueError):
        return False


@lru_cache(maxsize=128)
def _cached_agent_factory_supports_app_config(agent_factory: Any) -> bool:
    return _compute_agent_factory_supports_app_config(agent_factory)


def _agent_factory_supports_app_config(agent_factory: Any) -> bool:
    try:
        return _cached_agent_factory_supports_app_config(agent_factory)
    except TypeError:
        # Some callable instances are unhashable; fall back to a direct check.
        return _compute_agent_factory_supports_app_config(agent_factory)


async def _call_agent_factory_off_loop(
    agent_factory: Any,
    config: Any,
    app_config: AppConfig | None,
    private_runtime: PrivateAgentRuntime | None = None,
    *,
    tool_call_control_policy: ResolvedGraphToolCallControlProfile | None = None,
    tool_call_control_scope_id: str | None = None,
    tool_call_control_observer: object | None = None,
    context_evidence_observer: object | None = None,
    resolved_max_concurrent_subagents: int | None = None,
    resolved_max_total_subagents: int | None = None,
) -> Any:
    """Build a synchronous graph without blocking the Gateway event loop."""

    if tool_call_control_policy is not None and (not isinstance(tool_call_control_scope_id, str) or not tool_call_control_scope_id):
        raise PrivateRuntimeFactoryUnavailable(
            "Admitted tool-call control requires an exact execution scope.",
        )

    control_parameters = {
        "tool_call_control_profile": tool_call_control_policy,
        "tool_call_control_scope_id": tool_call_control_scope_id,
        "tool_call_control_observer": tool_call_control_observer,
        "resolved_max_concurrent_subagents": resolved_max_concurrent_subagents,
        "resolved_max_total_subagents": resolved_max_total_subagents,
    }

    context_evidence_parameters = {
        "context_evidence_observer": context_evidence_observer,
    }

    def _bind_control_parameters(
        factory: Any,
        kwargs: dict[str, Any],
    ) -> None:
        if tool_call_control_policy is None:
            return
        try:
            parameters = inspect.signature(factory).parameters
        except (TypeError, ValueError):
            parameters = {}
        if not set(control_parameters).issubset(parameters):
            raise PrivateRuntimeFactoryUnavailable(
                "Private runtime factory does not accept the admitted tool-call control profile.",
            )
        kwargs.update(control_parameters)

    def _bind_context_evidence_parameters(
        factory: Any,
        kwargs: dict[str, Any],
    ) -> None:
        if context_evidence_observer is None:
            return
        try:
            parameters = inspect.signature(factory).parameters
        except (TypeError, ValueError):
            parameters = {}
        if not set(context_evidence_parameters).issubset(parameters):
            raise PrivateRuntimeFactoryUnavailable(
                "Private runtime factory does not accept Context Evidence authority.",
            )
        kwargs.update(context_evidence_parameters)

    def _build() -> Any:
        if private_runtime is not None:
            private_factory = getattr(agent_factory, "private_runtime_factory", None)
            if callable(private_factory):
                private_kwargs: dict[str, Any] = {
                    "config": config,
                    "private_runtime": private_runtime,
                }
                if app_config is not None and _agent_factory_supports_app_config(private_factory):
                    private_kwargs["app_config"] = app_config
                _bind_control_parameters(private_factory, private_kwargs)
                _bind_context_evidence_parameters(
                    private_factory,
                    private_kwargs,
                )
                return private_factory(**private_kwargs)
            try:
                accepts_private_runtime = "private_runtime" in inspect.signature(agent_factory).parameters
            except (TypeError, ValueError):
                accepts_private_runtime = False
            if not accepts_private_runtime:
                raise PrivateRuntimeFactoryUnavailable("Private runtime requires a private-runtime-aware agent factory.")
        kwargs: dict[str, Any] = {"config": config}
        if app_config is not None and _agent_factory_supports_app_config(agent_factory):
            kwargs["app_config"] = app_config
        if private_runtime is not None:
            kwargs["private_runtime"] = private_runtime
        _bind_control_parameters(agent_factory, kwargs)
        _bind_context_evidence_parameters(agent_factory, kwargs)
        return agent_factory(**kwargs)

    return await asyncio.to_thread(_build)


async def run_agent(
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    *,
    ctx: RunContext,
    agent_factory: Any,
    graph_input: dict,
    config: dict,
    stream_modes: list[str] | None = None,
    stream_subgraphs: bool = False,
    interrupt_before: list[str] | Literal["*"] | None = None,
    interrupt_after: list[str] | Literal["*"] | None = None,
) -> RunAgentOutcome:
    """Execute an agent in the background, publishing events to *bridge*."""

    # Unpack infrastructure dependencies from RunContext.
    checkpointer = ctx.checkpointer
    store = ctx.store
    event_store = ctx.event_store
    run_events_config = ctx.run_events_config
    thread_store = ctx.thread_store

    token_usage_tracking_enabled = bool(
        getattr(
            run_events_config,
            "track_token_usage",
            getattr(
                getattr(ctx.app_config, "token_usage", None),
                "enabled",
                True,
            ),
        )
    )
    bridge = _PublicTokenUsageBridge(
        bridge,
        tracking_enabled=token_usage_tracking_enabled,
    )

    run_id = record.run_id
    thread_id = record.thread_id
    private_owner_user_id = record.scope.owner_user_id if ctx.private_agent_runtime is not None and record.scope is not None else None
    requested_modes: set[str] = set(stream_modes or ["values"])
    pre_run_checkpoint_id: str | None = None
    legacy_pre_run_snapshot: dict[str, Any] | None = None
    pre_run_workspace_snapshot: WorkspaceSnapshot | None = None
    workspace_changes_user_id: str | None = None
    run_mounts: tuple[RunScopedReadOnlyMount, ...] = ()
    run_mount_provider: Any | None = None
    run_mount_user_id: str | None = None
    snapshot_capture_failed = False
    llm_error_fallback_message: str | None = None
    llm_error_fallback_code: str | None = None
    # Message ids checkpointed *before* this run started. The stream loop uses
    # this set to mask out ``deerflow_error_fallback`` markers that belong to
    # earlier runs on the same thread — without it, one stale fallback in
    # history would mark every subsequent run on this thread as ``error``.
    pre_existing_message_ids: set[str] = set()
    private_message_boundary_required = ctx.private_scope is not None or record.scope is not None

    # The Agent graph is constructed per Run, so binding its scoped saver is
    # run-local and cannot swap another project's persistence authority.
    accessor: CheckpointStateAccessor | None = None
    rollback_point: RollbackPoint | None = None
    checkpoint_mode, checkpoint_snapshot_frequency = _checkpoint_runtime_settings(ctx.app_config)
    journal = None
    recovered_llm_failure_recorder = RunRecoveredLLMFailureRecorder()
    # Buffers subagent step events for batched persistence (#3779); assigned once
    # streaming starts and flushed in the finally block. Pre-bound to None so the
    # finally is safe even if an exception fires before streaming begins.
    subagent_events: _SubagentEventBuffer | None = None
    private_files = PrivateFileLifecycle(
        run_id=run_id,
        authority=ctx.file_authority,
        set_finalizing=run_manager.set_finalizing,
    )
    rollback_cancellation_pending = False
    defer_terminal_settlement = False
    terminal_published = False
    suspended_approval_id: str | None = None
    terminal_approval_id: str | None = None
    semantic_stop_recorder = RunSemanticStopRecorder()
    local_host_execution_approval_enabled = ctx.host_execution_approval_port is not None and isinstance(ctx.app_config, AppConfig) and resolve_host_bash_execution_mode(ctx.app_config) is HostBashExecutionMode.LOCAL_APPROVAL_REQUIRED

    def _model_output_limit_terminal() -> bool:
        # Once the output-limit middleware has observed an unrecoverable
        # Provider terminal, the completed Lead response is already behind
        # RunJournal's durable barrier. A later ordinary Stop must not rewrite
        # that fact as interrupted. Explicit rollback and authorization
        # revocation retain their stronger state/authority semantics.
        return semantic_stop_recorder.reason == "model_output_limit" and record.abort_action not in {
            "rollback",
            "authorization_revoked",
        }

    async def _settle_requested_rollback() -> bool:
        return await _settle_rollback(
            run_manager=run_manager,
            run_id=run_id,
            rollback=partial(
                _rollback_to_pre_run_checkpoint,
                accessor=accessor,
                checkpointer=checkpointer,
                thread_id=thread_id,
                run_id=run_id,
                rollback_point=rollback_point,
                snapshot_capture_failed=snapshot_capture_failed,
                snapshot_frequency=checkpoint_snapshot_frequency,
                pre_run_checkpoint_id=pre_run_checkpoint_id,
                pre_run_snapshot=legacy_pre_run_snapshot,
                allow_thread_delete=not private_message_boundary_required,
                context_evidence_observer=ctx.context_evidence_observer,
            ),
        )

    # Track whether "events" was requested but skipped
    if "events" in requested_modes:
        logger.info(
            "Run %s: 'events' stream_mode not supported in gateway (requires astream_events + checkpoint callbacks). Skipping.",
            run_id,
        )

    if ctx.resource_ownership is not None:
        ctx.resource_ownership.transfer_to_runner()

    try:
        await private_files.enter_finalizing()
        await run_manager.wait_for_prior_finalizing(thread_id, run_id)

        # Initialize RunJournal + write human_message event.
        # These are inside the try block so any exception (e.g. a DB
        # error writing the event) flows through the except/finally
        # path that publishes an "end" event to the SSE bridge —
        # otherwise a failure here would leave the stream hanging
        # with no terminator.
        if event_store is not None:
            from deerflow.runtime.journal import RunJournal

            journal = RunJournal(
                run_id=run_id,
                thread_id=thread_id,
                event_store=event_store,
                track_token_usage=token_usage_tracking_enabled,
                progress_reporter=lambda snapshot: run_manager.update_run_progress(run_id, **snapshot),
                scope=record.scope,
                recovered_llm_failure_recorder=(recovered_llm_failure_recorder),
                semantic_stop_recorder=semantic_stop_recorder,
            )

        # 1. Mark running
        await run_manager.set_status(run_id, RunStatus.running)

        # Checkpoint representation is Worker-owned startup state. Stamp the
        # run config before constructing the Agent so its state schema matches
        # the saver gate, and validate both the current head and an optional
        # historical selector before any graph state is consumed.
        inject_checkpoint_mode(config, checkpoint_mode)
        configurable = config.setdefault("configurable", {})
        # Durable scope comes from the persisted Run, never from caller
        # config. Private top-level Runs always execute in the root namespace;
        # only an admitted checkpoint_id/checkpoint_map may select history.
        configurable["thread_id"] = thread_id
        if private_message_boundary_required:
            configurable["checkpoint_ns"] = ""
        else:
            configurable.setdefault("checkpoint_ns", "")
        checkpoint_config: dict[str, Any] = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            }
        }
        inject_checkpoint_mode(checkpoint_config, checkpoint_mode)
        if checkpointer is not None:
            try:
                await aensure_checkpoint_mode_compatible(
                    checkpointer,
                    checkpoint_config,
                    checkpoint_mode,
                )
                selected_configurable: dict[str, Any] = {
                    "thread_id": thread_id,
                    "checkpoint_ns": configurable.get("checkpoint_ns", ""),
                }
                for selector_key in ("checkpoint_id", "checkpoint_map"):
                    if selector_key in configurable:
                        selected_configurable[selector_key] = configurable[selector_key]
                selected_checkpoint_config: dict[str, Any] = {"configurable": selected_configurable}
                inject_checkpoint_mode(
                    selected_checkpoint_config,
                    checkpoint_mode,
                )
                has_historical_selector = bool(selected_configurable.get("checkpoint_ns")) or any(selector_key in configurable for selector_key in ("checkpoint_id", "checkpoint_map"))
                if has_historical_selector:
                    await aensure_checkpoint_mode_compatible(
                        checkpointer,
                        selected_checkpoint_config,
                        checkpoint_mode,
                    )
            except CheckpointModeMismatchError:
                raise
            except Exception:
                if private_message_boundary_required:
                    logger.warning(
                        "Private Run pre-run message boundary is unavailable for run %s",
                        run_id,
                    )
                    raise PublicRunError(PublicRunErrorCode.PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE) from None
                raise

            # Full-mode compatibility stubs and legacy tests may not expose a
            # compiled graph state API. Preserve a raw fallback for those
            # callers, while production graphs replace it below with an exact
            # materialized RollbackPoint. Delta never trusts raw channel_values.
            if checkpoint_mode == "full":
                try:
                    ckpt_tuple = await checkpointer.aget_tuple(checkpoint_config)
                    if ckpt_tuple is not None:
                        ckpt_config = getattr(ckpt_tuple, "config", {}).get(
                            "configurable",
                            {},
                        )
                        pre_run_checkpoint_id = ckpt_config.get("checkpoint_id")
                        legacy_pre_run_snapshot = {
                            "checkpoint_ns": ckpt_config.get(
                                "checkpoint_ns",
                                "",
                            ),
                            "checkpoint": copy.deepcopy(getattr(ckpt_tuple, "checkpoint", {})),
                            "metadata": copy.deepcopy(getattr(ckpt_tuple, "metadata", {})),
                            "pending_writes": copy.deepcopy(getattr(ckpt_tuple, "pending_writes", []) or []),
                        }
                        if private_message_boundary_required:
                            pre_existing_message_ids = _collect_private_pre_existing_message_ids(legacy_pre_run_snapshot)
                except Exception:
                    snapshot_capture_failed = True
                    if private_message_boundary_required:
                        logger.warning(
                            "Private Run pre-run message boundary is unavailable for run %s",
                            run_id,
                        )
                        raise PublicRunError(PublicRunErrorCode.PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE) from None
                    logger.warning(
                        "Could not capture pre-run checkpoint snapshot for run %s",
                        run_id,
                        exc_info=True,
                    )

        await private_files.restore()

        if event_store is not None and not private_files.enabled:
            workspace_changes_user_id = private_owner_user_id or get_effective_user_id()
            try:
                pre_run_workspace_snapshot = await capture_workspace_snapshot(
                    thread_id,
                    user_id=workspace_changes_user_id,
                )
            except Exception:
                logger.warning("Could not capture pre-run workspace snapshot for run %s", run_id, exc_info=True)

        # 2. Publish metadata — useStream needs both run_id AND thread_id
        await bridge.publish(
            run_id,
            "metadata",
            {
                "run_id": run_id,
                "thread_id": thread_id,
            },
        )

        # 3. Build the agent
        from langchain_core.runnables import RunnableConfig
        from langgraph.runtime import Runtime

        # Inject runtime context so middlewares and tools (via ToolRuntime.context) can
        # access thread-level data. langgraph-cli does this automatically; we must do it
        # manually here because we drive the graph through ``agent.astream(config=...)``
        # without passing the official ``context=`` parameter.
        runtime_ctx = _build_runtime_context(
            thread_id,
            run_id,
            config.get("context"),
            ctx.app_config,
            private_scope=ctx.private_scope,
            authorization_checker=ctx.authorization_checker,
            authorization_boundary=ctx.authorization_boundary,
            file_authority=private_files.authority,
            memory_authority=ctx.memory_authority,
            guardrail_attribution=ctx.guardrail_attribution,
            run_read_only_mounts=(
                (
                    RunScopedReadOnlyMount(
                        run_id=run_id,
                        container_path=ctx.app_config.skills.container_path,
                        host_path=str(ctx.private_agent_runtime.skill_root),
                    ),
                )
                if (not private_files.enabled and ctx.private_agent_runtime is not None and ctx.app_config is not None)
                else ()
            ),
            runtime_owner_user_id=private_owner_user_id,
            memory_archive_context=ctx.memory_archive_context,
            host_execution_approval_port=ctx.host_execution_approval_port,
            channel_user_id=ctx.channel_user_id,
            server_abort_event=record.abort_event,
            vision_dispatch_authority=ctx.vision_dispatch_authority,
            run_semantic_stop_recorder=semantic_stop_recorder,
            token_budget_usage_recorder=ctx.token_budget_usage_recorder,
        )
        runtime_model_name = None
        prompt_bundle = None
        runtime_skills = None
        runtime_mcp_tools = None
        runtime_agent_catalog = None
        skill_secret_provider = None
        if ctx.private_agent_runtime is not None:
            # Context is merged after configurable by the Agent factory. Keep
            # both channels pinned to the same persisted private-Run model.
            runtime_model_name = record.model_name
            prompt_bundle = getattr(ctx.private_agent_runtime, "prompt_bundle", None)
            runtime_skills = tuple(
                getattr(ctx.private_agent_runtime, "skills", ()),
            )
            runtime_mcp_tools = tuple(
                getattr(ctx.private_agent_runtime, "mcp_tools", ()),
            )
            runtime_agent_catalog = trusted_runtime_agent_catalog(getattr(ctx.private_agent_runtime, "agent_catalog", None))
            raw_skill_secret_provider = getattr(
                ctx.private_agent_runtime,
                "materialize_skill_scoped_secrets",
                None,
            )
            skill_container_path = getattr(ctx.app_config.skills, "container_path", None) if ctx.app_config is not None else None
            if callable(raw_skill_secret_provider) and isinstance(skill_container_path, str):
                skill_secret_provider = partial(
                    raw_skill_secret_provider,
                    skill_container_path,
                )
        incoming_metadata = config.get("metadata") if isinstance(config.get("metadata"), dict) else {}
        deerflow_trace_id = (
            normalize_trace_id(
                incoming_metadata.get(RuntimeContextKeys.TRACE_ID),
            )
            or get_current_trace_id()
        )
        # Expose the run-scoped journal under a sentinel key so middleware can
        # write audit events (e.g. SafetyFinishReasonMiddleware recording
        # suppressed tool calls). Double-underscore prefix marks it as a
        # runtime-internal channel; user code must not depend on the key name.
        RuntimeContextCarrier(
            model_name=runtime_model_name,
            agent_prompt_bundle=prompt_bundle,
            runtime_skills=runtime_skills,
            runtime_mcp_tools=runtime_mcp_tools,
            runtime_agent_catalog=runtime_agent_catalog,
            skill_secret_provider=skill_secret_provider,
            current_run_pre_existing_message_ids=frozenset(
                pre_existing_message_ids,
            ),
            trace_id=deerflow_trace_id,
            run_journal=journal,
            token_usage_tracking_enabled=token_usage_tracking_enabled,
            recovered_llm_failure_recorder=(recovered_llm_failure_recorder),
        ).install_into(runtime_ctx)
        _install_runtime_context(config, runtime_ctx)
        runtime = Runtime(context=cast(Any, runtime_ctx), store=store)
        configurable = config.setdefault("configurable", {})
        configurable["__pregel_runtime"] = runtime
        if ctx.private_agent_runtime is not None:
            # Private admission persists the exact configured model UUID on
            # the Run.  Reassert that authoritative value at the Worker boundary
            # so absent or forged caller config cannot influence the private
            # runtime factory.  ``None`` remains a fail-closed value.
            configurable[RuntimeContextKeys.MODEL_NAME] = record.model_name

        run_mounts = runtime_ctx.get(
            RuntimeContextKeys.RUN_READ_ONLY_MOUNTS,
            (),
        )
        if run_mounts:
            run_mount_provider = get_sandbox_provider()
            run_mount_user_id = private_owner_user_id or get_effective_user_id()
            await asyncio.to_thread(
                run_mount_provider.validate_run_scoped_mounts,
                thread_id,
                user_id=run_mount_user_id,
                mounts=run_mounts,
            )

        # A Local host-execution continuation consumes its app-owned frozen
        # plan and launches it here, before graph construction or any model
        # call.  The returned hidden input replaces the legacy retry prompt;
        # the model only receives the bounded, durably settled result.
        record_metadata = record.metadata
        continuation_required = isinstance(record_metadata, Mapping) and record_metadata.get("execution_approval_continuation") is True
        graph_input = await execute_frozen_host_execution_continuation(
            approval_port=ctx.host_execution_approval_port,
            app_config=ctx.app_config,
            runtime_context=runtime_ctx,
            file_authority=ctx.file_authority,
            graph_input=graph_input,
            continuation_required=continuation_required,
        )

        # Inject RunJournal as a LangChain callback handler.
        # on_llm_end captures token usage; on_chain_start/end captures lifecycle.
        if journal is not None:
            config.setdefault("callbacks", []).append(journal)

        # Inject Langfuse trace-attribute metadata so the langchain CallbackHandler
        # can lift session_id / user_id / trace_name / tags onto the root trace.
        # Shared helper with ``DeerFlowClient.stream`` so both entry points stay
        # in sync; caller-provided metadata wins via setdefault inside the helper.
        inject_langfuse_metadata(
            config,
            thread_id=thread_id,
            user_id=_repository_trace_user_id(record),
            assistant_id=record.assistant_id,
            model_name=record.model_name,
            environment=os.environ.get("ACT_WEAVE_ENV") or os.environ.get("ENVIRONMENT"),
            deerflow_trace_id=deerflow_trace_id,
            include_deerflow_trace_id=is_trace_correlation_enabled(
                ctx.app_config,
            ),
        )

        # Resolve after runtime context installation so context/configurable reflect
        # the agent name that this run will actually execute.
        config.setdefault("run_name", resolve_root_run_name(config, record.assistant_id))
        initial_runnable_config = RunnableConfig(**config)

        def _continuation_runnable_config() -> RunnableConfig:
            continuation_config = dict(config)
            configurable = dict(continuation_config.get("configurable", {}) or {})
            configurable["checkpoint_ns"] = ""
            configurable.pop("checkpoint_id", None)
            configurable.pop("checkpoint_map", None)
            continuation_config["configurable"] = configurable
            return RunnableConfig(**continuation_config)

        tool_call_control_observer = None
        if journal is not None and ctx.tool_call_control_policy is not None:
            from deerflow.runtime.journal import (
                RunJournalToolCallControlObserver,
            )

            tool_call_control_observer = RunJournalToolCallControlObserver(
                journal,
                owner_loop=asyncio.get_running_loop(),
            )

        agent = await _call_agent_factory_off_loop(
            agent_factory,
            initial_runnable_config,
            ctx.app_config,
            ctx.private_agent_runtime,
            tool_call_control_policy=ctx.tool_call_control_policy,
            tool_call_control_scope_id=run_id,
            tool_call_control_observer=tool_call_control_observer,
            context_evidence_observer=ctx.context_evidence_observer,
            resolved_max_concurrent_subagents=ctx.max_concurrent_subagents,
            resolved_max_total_subagents=ctx.max_total_subagents,
        )

        accessor = CheckpointStateAccessor.bind(
            agent,
            checkpointer,
            store=store,
            mode=checkpoint_mode,
        )

        # Capture the rollback point only after the run-local graph has been
        # compiled with its effective state schema. Delta checkpoints do not
        # contain complete raw channel_values, so messages and all restorable
        # channels must come from graph-materialized state. The raw saver is
        # consulted only for exact pending writes.
        if checkpointer is not None:
            can_materialize_state = callable(getattr(agent, "aget_state", None))
            try:
                if can_materialize_state:
                    rollback_point = await _capture_rollback_point(
                        accessor,
                        checkpointer,
                        checkpoint_config,
                    )
                    snapshot_capture_failed = False
                elif checkpoint_mode == "full":
                    rollback_point = _rollback_point_from_legacy_snapshot(
                        thread_id=thread_id,
                        checkpoint_id=pre_run_checkpoint_id,
                        snapshot=legacy_pre_run_snapshot,
                    )
                else:
                    raise RuntimeError("Delta checkpoint state materialization is unavailable")

                if rollback_point is not None:
                    pre_run_checkpoint_id = rollback_point.config.get("configurable", {}).get("checkpoint_id")
                    materialized_values = {"messages": list(rollback_point.messages)}
                    pre_existing_message_ids = _collect_private_pre_existing_message_ids(materialized_values) if private_message_boundary_required else _collect_pre_existing_message_ids(materialized_values)
                else:
                    pre_existing_message_ids = set()
            except Exception:
                snapshot_capture_failed = True
                if private_message_boundary_required:
                    logger.warning(
                        "Private Run pre-run message boundary is unavailable for run %s",
                        run_id,
                    )
                    raise PublicRunError(PublicRunErrorCode.PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE) from None
                logger.warning(
                    "Could not materialize pre-run checkpoint for run %s",
                    run_id,
                    exc_info=True,
                )

            # A historical root selector is a fork. Delta history cannot
            # distinguish writes owned by an abandoned sibling, so express
            # the selector as a whole-state replacement on the current head
            # before streaming. This remains inside the scoped saver and Job
            # lease boundary.
            resumed_messages = await _linearize_delta_checkpoint_resume(
                accessor=accessor,
                checkpointer=checkpointer,
                config=config,
                thread_id=thread_id,
                run_id=run_id,
                snapshot_frequency=checkpoint_snapshot_frequency,
            )
            if resumed_messages is not None:
                materialized_values = {"messages": resumed_messages}
                pre_existing_message_ids = _collect_private_pre_existing_message_ids(materialized_values) if private_message_boundary_required else _collect_pre_existing_message_ids(materialized_values)

        RuntimeContextCarrier(
            current_run_pre_existing_message_ids=frozenset(
                pre_existing_message_ids,
            ),
        ).install_into(runtime_ctx)
        _install_runtime_context(config, runtime_ctx)
        # Linearization removes checkpoint selectors and the trusted message
        # boundary is installed after graph construction. Stream with a fresh
        # config reflecting both mutations.
        initial_runnable_config = RunnableConfig(**config)

        # Capture the effective (resolved) model name from the agent's metadata.
        # _resolve_model_name in agent.py may return the default model if the
        # requested name is not in the allowlist — this update ensures the
        # persisted model_name reflects the actual model used.
        if record.model_name is not None:
            resolved = getattr(agent, "metadata", {}) or {}
            if isinstance(resolved, dict):
                effective = resolved.get("model_name")
                if effective and effective != record.model_name:
                    await run_manager.update_model_name(record.run_id, effective)

        # 4. Persistence is already bound through the run-local accessor.

        # 5. Set interrupt nodes
        if interrupt_before:
            agent.interrupt_before_nodes = interrupt_before
        if interrupt_after:
            agent.interrupt_after_nodes = interrupt_after

        # 6. Build LangGraph stream_mode list
        #    "events" is NOT a valid astream mode — skip it
        #    "messages-tuple" maps to LangGraph's "messages" mode
        lg_modes: list[str] = []
        for m in requested_modes:
            if m == "messages-tuple":
                lg_modes.append("messages")
            elif m == "events":
                # Skipped — see log above
                continue
            elif m in _VALID_LG_MODES:
                lg_modes.append(m)
        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for m in lg_modes:
            if m not in seen:
                seen.add(m)
                deduped.append(m)
        published_lg_modes = frozenset(deduped)
        lg_modes = deduped or ["values"]

        # Semantic outcome cannot depend on which observational lanes the
        # caller chose to receive. Always consume the parent graph's ``values``
        # authority lane; when it was not requested, keep it hidden from the
        # caller's StreamBridge.
        if "values" not in lg_modes:
            lg_modes.append("values")

        logger.info("Run %s: streaming with modes %s (requested: %s)", run_id, lg_modes, requested_modes)

        # Buffer subagent step events and persist them in batches (#3779) instead
        # of one low-frequency put() per step on the hot stream loop. Flushed in
        # the finally block so buffered steps survive abort/exception paths too.
        subagent_events = _SubagentEventBuffer(
            event_store,
            thread_id,
            run_id,
            record.scope,
            token_usage_tracking_enabled=token_usage_tracking_enabled,
        )

        goal_evaluator_model: Any | None = None

        def _get_goal_evaluator_model() -> Any:
            nonlocal goal_evaluator_model
            if goal_evaluator_model is None:
                goal_evaluator_model = create_goal_evaluator_model(
                    model_name=record.model_name,
                    app_config=ctx.app_config,
                )
            return goal_evaluator_model

        def _capture_run_fallback(
            *,
            namespace: tuple[str, ...],
            mode: str,
            chunk: Any,
        ) -> None:
            nonlocal llm_error_fallback_message, llm_error_fallback_code
            if namespace or mode not in _LLM_ERROR_FALLBACK_AUTHORITY_MODES or llm_error_fallback_message is not None:
                return
            fallback = _extract_llm_error_fallback(
                chunk,
                pre_existing_message_ids,
            )
            if fallback is not None:
                llm_error_fallback_message = fallback.message
                llm_error_fallback_code = fallback.error_code

        async def _stream_once(input_payload: Any, stream_config: RunnableConfig) -> None:
            nonlocal suspended_approval_id, llm_error_fallback_message, llm_error_fallback_code
            if suspended_approval_id is not None:
                return
            tool_call_chunk_batcher = _ToolCallChunkBatcher() if "messages" in lg_modes else None
            text_delta_flush_ms = ctx.app_config.worker.stream.text_delta_flush_ms if isinstance(ctx.app_config, AppConfig) else DEFAULT_TEXT_DELTA_FLUSH_MS
            # 0 disables coalescing and restores per-token durable frames.
            text_delta_coalescer = _TextDeltaCoalescer(window_seconds=text_delta_flush_ms / 1000.0) if text_delta_flush_ms > 0 and "messages" in lg_modes else None
            try:
                if len(lg_modes) == 1 and not stream_subgraphs:
                    # Single mode, no subgraphs: astream yields raw chunks.
                    # This path has no root messages lane, so tool-argument
                    # batching is not needed.
                    single_mode = lg_modes[0]
                    stream_kwargs: dict[str, Any] = {
                        "config": stream_config,
                        "stream_mode": single_mode,
                    }
                    if local_host_execution_approval_enabled:
                        stream_kwargs["durability"] = "sync"
                    raw_stream = agent.astream(input_payload, **stream_kwargs)
                    async for chunk in _iter_with_text_delta_deadline(
                        raw_stream,
                        text_delta_coalescer,
                    ):
                        if chunk is _TEXT_DELTA_FLUSH_DUE:
                            assert text_delta_coalescer is not None
                            for frame in text_delta_coalescer.flush():
                                await bridge.publish(
                                    run_id,
                                    _lg_mode_to_sse_event(single_mode),
                                    serialize(frame, mode=single_mode),
                                )
                            continue
                        if record.abort_event.is_set():
                            logger.info(
                                "Run %s abort requested — stopping",
                                run_id,
                            )
                            break
                        _capture_run_fallback(
                            namespace=(),
                            mode=single_mode,
                            chunk=chunk,
                        )
                        current_run_approval_id = _current_run_host_execution_approval_id(
                            chunk,
                            run_id,
                        )
                        if single_mode in published_lg_modes:
                            sse_event = _lg_mode_to_sse_event(single_mode)
                            frames = text_delta_coalescer.push(chunk) if single_mode == "messages" and text_delta_coalescer is not None else [chunk]
                            for frame in frames:
                                await bridge.publish(
                                    run_id,
                                    sse_event,
                                    serialize(frame, mode=single_mode),
                                )
                            if single_mode == "custom":
                                await subagent_events.add(chunk)
                        if current_run_approval_id is not None and suspended_approval_id is None:
                            suspended_approval_id = current_run_approval_id
                            logger.info(
                                "Run %s staged host execution approval — hidden goal continuation is suspended",
                                run_id,
                            )
                    return

                # Multiple modes or subgraphs: astream yields tuples.
                stream_kwargs = {
                    "config": stream_config,
                    "stream_mode": lg_modes,
                    "subgraphs": stream_subgraphs,
                }
                if local_host_execution_approval_enabled:
                    stream_kwargs["durability"] = "sync"
                raw_stream = agent.astream(input_payload, **stream_kwargs)
                async for item in _iter_with_text_delta_deadline(
                    raw_stream,
                    text_delta_coalescer,
                ):
                    if item is _TEXT_DELTA_FLUSH_DUE:
                        assert text_delta_coalescer is not None
                        pending_frames: list[Any] = []
                        for frame in text_delta_coalescer.flush():
                            pending_frames.extend(tool_call_chunk_batcher.push(frame) if tool_call_chunk_batcher is not None else [frame])
                        for publish_chunk in pending_frames:
                            await bridge.publish(
                                run_id,
                                "messages",
                                serialize(publish_chunk, mode="messages"),
                            )
                        continue
                    if record.abort_event.is_set():
                        logger.info(
                            "Run %s abort requested — stopping",
                            run_id,
                        )
                        break

                    namespace, mode, chunk = _unpack_stream_item(
                        item,
                        lg_modes,
                        stream_subgraphs,
                    )
                    if mode is None:
                        continue

                    _capture_run_fallback(
                        namespace=namespace,
                        mode=mode,
                        chunk=chunk,
                    )
                    current_run_approval_id = _current_run_host_execution_approval_id(
                        chunk,
                        run_id,
                    )
                    if mode in published_lg_modes:
                        await _publish_stream_item(
                            bridge=bridge,
                            run_id=run_id,
                            mode=mode,
                            chunk=chunk,
                            namespace=namespace,
                            tool_call_chunk_batcher=tool_call_chunk_batcher,
                            text_delta_coalescer=text_delta_coalescer,
                            subagent_events=subagent_events,
                        )
                    if current_run_approval_id is not None and suspended_approval_id is None:
                        suspended_approval_id = current_run_approval_id
                        logger.info(
                            "Run %s staged host execution approval — hidden goal continuation is suspended",
                            run_id,
                        )
            finally:
                stream_error = sys.exception()
                if text_delta_coalescer is not None or tool_call_chunk_batcher is not None:
                    try:
                        # Terminal flush: coalesced text first (routed through
                        # the tool batcher so any older argument batch keeps
                        # its place), then the tool batcher itself.
                        pending_frames: list[Any] = []
                        if text_delta_coalescer is not None:
                            for frame in text_delta_coalescer.flush():
                                pending_frames.extend(tool_call_chunk_batcher.push(frame) if tool_call_chunk_batcher is not None else [frame])
                        if tool_call_chunk_batcher is not None:
                            pending_frames.extend(tool_call_chunk_batcher.finish())
                        for publish_chunk in pending_frames:
                            await bridge.publish(
                                run_id,
                                "messages",
                                serialize(
                                    publish_chunk,
                                    mode="messages",
                                ),
                            )
                    except Exception:
                        if stream_error is None:
                            raise
                        logger.debug(
                            "Could not flush pending stream chunks for run %s",
                            run_id,
                            exc_info=True,
                        )

        async def _refresh_host_execution_approval_gate() -> None:
            """Recheck durable state when the selected stream hides messages."""

            nonlocal suspended_approval_id
            if suspended_approval_id is not None or not local_host_execution_approval_enabled or checkpointer is None or accessor is None or not callable(getattr(accessor.graph, "aget_state", None)):
                return
            messages = await _materialized_checkpoint_messages(
                accessor,
                thread_id,
            )
            approval_id = _current_run_host_execution_approval_id(
                messages,
                run_id,
            )
            if approval_id is not None:
                suspended_approval_id = approval_id
                logger.info(
                    "Run %s found staged host execution approval in its materialized checkpoint — hidden goal continuation is suspended",
                    run_id,
                )

        # 7. Stream the requested turn, then optionally continue hidden goal turns.
        await _stream_once(graph_input, initial_runnable_config)
        await _refresh_host_execution_approval_gate()
        while suspended_approval_id is None and not record.abort_event.is_set() and semantic_stop_recorder.reason is None and not llm_error_fallback_message and (journal is None or not journal.had_llm_error_fallback):
            continuation_input = await _prepare_goal_continuation_input(
                bridge=bridge,
                checkpointer=checkpointer,
                accessor=accessor,
                thread_id=thread_id,
                run_id=run_id,
                model_name=record.model_name,
                app_config=ctx.app_config,
                snapshot_frequency=checkpoint_snapshot_frequency,
                evaluator_model_factory=_get_goal_evaluator_model,
                abort_event=record.abort_event,
                authorization_boundary=ctx.authorization_boundary,
            )
            if continuation_input is None or record.abort_event.is_set():
                break
            await _stream_once(continuation_input, _continuation_runnable_config())
            await _refresh_host_execution_approval_gate()

        # 8. Final status
        if _model_output_limit_terminal():
            record.terminal_authority = "durable_response"
            await private_files.mark_failed()
            await run_manager.set_status(
                run_id,
                RunStatus.error,
                error=PublicRunErrorCode.MODEL_OUTPUT_LIMIT.value,
            )
        elif record.abort_event.is_set():
            await run_manager.set_finalizing(run_id, True)
            action = record.abort_action
            if action == "rollback":
                await private_files.mark_failed()
                rollback_cancellation_pending |= await _settle_requested_rollback()
            else:
                if action != "authorization_revoked":
                    await private_files.finalize()
                else:
                    await private_files.mark_failed()
                await run_manager.set_status(
                    run_id,
                    RunStatus.interrupted,
                    error=(AUTHORIZATION_REVOKED_REASON if action == "authorization_revoked" else None),
                )
        elif llm_error_fallback_message or (journal is not None and journal.had_llm_error_fallback):
            error_code = llm_error_fallback_code
            if error_code is None and journal is not None:
                error_code = journal.llm_error_fallback_code
            error_code = error_code or llm_error_code_for_reason("generic")
            await private_files.mark_failed()
            await run_manager.set_status(run_id, RunStatus.error, error=error_code)
        else:
            await private_files.finalize()
            if semantic_stop_recorder.reason == "loop_capped":
                await run_manager.set_status(
                    run_id,
                    RunStatus.error,
                    error=PublicRunErrorCode.LOOP_SAFETY_LIMIT.value,
                )
            else:
                obligation_status = await private_files.output_delivery_status()
                if suspended_approval_id is not None:
                    await run_manager.set_status(run_id, RunStatus.success)
                elif obligation_status == "delivered":
                    # The persisted any-one obligation covers the union of source
                    # candidates and continuation outputs.  Delivering an exact
                    # candidate is sufficient even when the command also created
                    # another output during this continuation.
                    await run_manager.set_status(run_id, RunStatus.success)
                elif obligation_status not in {"not_required", "delivered"}:
                    await run_manager.set_status(
                        run_id,
                        RunStatus.error,
                        error=PublicRunErrorCode.OUTPUT_DELIVERY_INCOMPLETE.value,
                    )
                elif private_files.output_delivery_satisfied():
                    await run_manager.set_status(run_id, RunStatus.success)
                else:
                    await run_manager.set_status(
                        run_id,
                        RunStatus.error,
                        error=_PRIVATE_OUTPUT_NOT_PRESENTED_ERROR,
                    )

    except asyncio.CancelledError:
        await run_manager.set_finalizing(run_id, True)
        action = record.abort_action
        model_output_limit_terminal = _model_output_limit_terminal()
        if model_output_limit_terminal:
            record.terminal_authority = "durable_response"
            await private_files.mark_failed()
            await run_manager.set_status(
                run_id,
                RunStatus.error,
                error=PublicRunErrorCode.MODEL_OUTPUT_LIMIT.value,
            )
            logger.info(
                "Run %s preserved observed Provider output-limit terminal across late cancellation",
                run_id,
            )
        elif action == "rollback":
            await private_files.mark_failed()
            rollback_cancellation_pending |= await _settle_requested_rollback()
        else:
            if action != "authorization_revoked":
                await private_files.finalize()
            else:
                await private_files.mark_failed()
            await run_manager.set_status(
                run_id,
                RunStatus.interrupted,
                error=(AUTHORIZATION_REVOKED_REASON if action == "authorization_revoked" else None),
            )
            logger.info("Run %s was cancelled", run_id)
        if private_files.enabled and not model_output_limit_terminal:
            raise

    except AuthorizationRevoked:
        record.abort_action = "authorization_revoked"
        record.abort_event.set()
        await private_files.mark_failed()
        await run_manager.set_status(
            run_id,
            RunStatus.interrupted,
            error=AUTHORIZATION_REVOKED_REASON,
        )
        await bridge.publish(
            run_id,
            "error",
            {
                "message": AUTHORIZATION_REVOKED_REASON,
                "name": AUTHORIZATION_REVOKED_REASON,
            },
        )

    except (
        GraphRecursionError,
        ToolCallControlLoopFinalizationFailed,
        ToolCallControlStateInvalid,
    ) as exc:
        if isinstance(exc, GraphRecursionError):
            error_code = PublicRunErrorCode.GRAPH_RECURSION_LIMIT
        elif isinstance(exc, ToolCallControlLoopFinalizationFailed):
            error_code = PublicRunErrorCode.LOOP_FINALIZATION_FAILED
        else:
            error_code = PublicRunErrorCode.TOOL_CALL_CONTROL_STATE_INVALID
        public_error = PublicRunError(error_code)
        logger.error(
            "Run %s failed with public error %s",
            run_id,
            error_code.value,
        )
        await private_files.mark_failed()
        await run_manager.set_status(
            run_id,
            RunStatus.error,
            error=error_code.value,
        )
        await bridge.publish(
            run_id,
            "error",
            {
                "message": public_error.public_message,
                "name": error_code.value,
            },
        )

    except PublicRunError as exc:
        logger.error(
            "Run %s failed with public error %s",
            run_id,
            exc.code.value,
        )
        await private_files.mark_failed()
        if exc.code in {
            PublicRunErrorCode.MODEL_OUTPUT_LIMIT,
            PublicRunErrorCode.PROVIDER_REQUEST_USAGE_UNSUPPORTED,
            PublicRunErrorCode.PROVIDER_REQUEST_PROFILE_DRIFT,
            PublicRunErrorCode.CONTEXT_CAPACITY_EXCEEDED,
            PublicRunErrorCode.CONTEXT_PROVIDER_CALL_AMBIGUOUS,
        }:
            # Publish the typed terminal immediately after the in-memory Run
            # classification. The durable stream terminal is takeover
            # authority even if this Worker exits before Job settlement.
            await run_manager.set_status(
                run_id,
                RunStatus.error,
                error=exc.code.value,
            )
            if exc.code is PublicRunErrorCode.MODEL_OUTPUT_LIMIT:
                record.terminal_authority = "durable_response"
                await bridge.publish_end(run_id)
                terminal_published = True
            else:
                await bridge.publish(
                    run_id,
                    "error",
                    {
                        "message": exc.public_message,
                        "name": exc.code.value,
                    },
                )
        else:
            await run_manager.set_status(
                run_id,
                RunStatus.error,
                error=exc.public_message,
            )
            await bridge.publish(
                run_id,
                "error",
                {
                    "message": exc.public_message,
                    "name": exc.code.value,
                },
            )

    except (
        ContextProviderCallAmbiguousError,
        MemoryAuthorityUnavailable,
    ):
        # The durable private executor owns retry/dead settlement. Preserve
        # this fatal signal through runtime cleanup instead of publishing a
        # provisional terminal or misclassifying it as revoked authority.
        defer_terminal_settlement = True
        await private_files.mark_failed()
        raise

    except Exception as exc:
        logger.error(
            "Run %s failed with %s",
            run_id,
            type(exc).__name__,
        )
        await private_files.mark_failed()
        await run_manager.set_status(
            run_id,
            RunStatus.error,
            error=RUN_EXECUTION_FAILED_ERROR_CODE,
        )
        await bridge.publish(
            run_id,
            "error",
            {
                "message": "Run execution failed",
                "name": RUN_EXECUTION_FAILED_ERROR_CODE,
            },
        )

    finally:
        try:
            # Persist any subagent step events still buffered (#3779) — including on
            # abort/exception paths, where the stream loop broke before its own flush.
            if subagent_events is not None:
                await subagent_events.flush()

            if event_store is not None and private_files.enabled:
                result = trusted_workspace_change_result(
                    private_files.workspace_changes,
                )
                if result is not None:
                    payload = result.to_dict()
                    try:
                        await event_store.put(
                            thread_id=thread_id,
                            run_id=run_id,
                            event_type=WORKSPACE_CHANGES_EVENT_TYPE,
                            category="workspace",
                            content=workspace_change_event_content(result),
                            metadata={WORKSPACE_CHANGES_METADATA_KEY: payload},
                            scope=record.scope,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to record private workspace changes for run %s",
                            run_id,
                            exc_info=True,
                        )
            elif event_store is not None and pre_run_workspace_snapshot is not None:
                try:
                    await record_workspace_changes(
                        event_store,
                        thread_id,
                        run_id,
                        pre_run_workspace_snapshot,
                        user_id=workspace_changes_user_id,
                    )
                except Exception:
                    logger.warning("Failed to record workspace changes for run %s", run_id, exc_info=True)

            # Flush any buffered journal events and persist completion data
            if journal is not None:
                try:
                    await journal.flush()
                except Exception:
                    logger.warning("Failed to flush journal for run %s", run_id, exc_info=True)

                try:
                    # Persist token usage + convenience fields to RunStore
                    completion = journal.get_completion_data()
                    await run_manager.update_run_completion(run_id, status=record.status.value, **completion)
                except Exception:
                    logger.warning("Failed to persist run completion for %s (non-fatal)", run_id, exc_info=True)

        finally:
            # A private run owns both the agent runtime and file-authority lease
            # until every terminal task completes. These cleanup operations are
            # joined under repeated cancellation before the admission barrier is
            # cleared, so a replacement cannot observe or reuse live resources.
            cleanup_succeeded = True
            mount_release_outcome = None
            try:
                mount_release_outcome = await private_files.release()
            except Exception:
                cleanup_succeeded = False
                logger.warning(
                    "Private file authority cleanup failed for run %s",
                    run_id,
                    exc_info=True,
                )

            if ctx.private_agent_runtime is not None:
                close_private_runtime = (
                    ctx.private_agent_runtime.aclose
                    if mount_release_outcome is None
                    else lambda: ctx.private_agent_runtime.aclose(
                        mount_release_outcome,
                    )
                )
                cleanup_succeeded = (
                    await private_files.join_cleanup(
                        close_private_runtime,
                        failure_message=f"Private runtime cleanup failed for run {run_id}",
                    )
                    and cleanup_succeeded
                )

            if not cleanup_succeeded and record.status is RunStatus.success:
                # The assistant response, checkpoints, and file finalization
                # are already durable at this point.  A best-effort teardown
                # failure must not rewrite that completed user-visible result
                # into an Agent-execution failure.  Keep ``finalizing`` set so
                # the in-process resource barrier is not cleared; the warning
                # remains actionable in Worker logs without misleading the
                # user into retrying an already completed request.
                logger.error(
                    "Private cleanup failed after completed Run %s; preserving successful terminal state",
                    run_id,
                )

            if record.finalizing and cleanup_succeeded:
                if private_files.enabled:
                    await private_files.join_cleanup(
                        lambda: run_manager.set_finalizing(run_id, False),
                        failure_message=f"Failed to clear finalizing state for run {run_id}",
                    )
                else:
                    await run_manager.set_finalizing(run_id, False)

        # Cleanup is allowed to downgrade a provisional success to the final
        # authoritative error status. Terminal observers must run only after
        # those retries finish so every consumer sees the same durable result.
        if checkpointer is not None and thread_store is not None and record.status is RunStatus.success:
            try:
                ckpt_config = {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": "",
                    }
                }
                title = None
                if accessor is not None and callable(getattr(accessor.graph, "aget_state", None)):
                    snapshot = await accessor.aget(ckpt_config)
                    values = getattr(snapshot, "values", None)
                    if isinstance(values, dict):
                        title = values.get("title")
                else:
                    # Compatibility fallback for embedded test graphs without
                    # LangGraph's state API. Production always uses the scoped,
                    # mode-aware accessor above.
                    ckpt_tuple = await checkpointer.aget_tuple(ckpt_config)
                    if ckpt_tuple is not None:
                        ckpt = getattr(ckpt_tuple, "checkpoint", {}) or {}
                        channel_values = ckpt.get("channel_values", {}) if isinstance(ckpt, dict) else {}
                        if isinstance(channel_values, dict):
                            title = channel_values.get("title")
                if title:
                    await thread_store.update_display_name(thread_id, title)
            except Exception:
                logger.debug(
                    "Failed to sync title for thread %s (non-fatal)",
                    thread_id,
                )

        if thread_store is not None and not defer_terminal_settlement:
            try:
                final_status = "idle" if record.status == RunStatus.success else record.status.value
                await thread_store.update_status(thread_id, final_status)
            except Exception:
                logger.debug("Failed to update thread_meta status for %s (non-fatal)", thread_id)

        if ctx.on_run_completed is not None and not defer_terminal_settlement:
            try:
                await ctx.on_run_completed(record)
            except Exception:
                logger.warning("Run completion hook failed for %s (non-fatal)", run_id, exc_info=True)

        if run_mount_provider is not None and run_mount_user_id is not None and run_mounts:
            try:
                await run_mount_provider.release_run_scoped_mounts_async(
                    thread_id,
                    user_id=run_mount_user_id,
                    mounts=run_mounts,
                )
            except Exception:
                logger.warning(
                    "Run-scoped sandbox cleanup failed for run %s",
                    run_id,
                    exc_info=True,
                )

        terminal_approval_id = suspended_approval_id if record.status is RunStatus.success else None
        if terminal_approval_id is not None and local_host_execution_approval_enabled:
            seal_suspension = getattr(
                ctx.host_execution_approval_port,
                "seal_suspended_approval_marker",
                None,
            )
            if not callable(seal_suspension):
                raise RuntimeError(
                    "host execution suspension marker authority is unavailable",
                )
            # This is the last durable side-effect before the public success
            # terminal. It proves the checkpoint-safe pause under the live
            # source Job lease so a later attempt can recover the exact row.
            await seal_suspension(terminal_approval_id)
        if not defer_terminal_settlement and not terminal_published:
            await bridge.publish_end(run_id)
        if private_files.cancellation_pending or rollback_cancellation_pending:
            raise asyncio.CancelledError

    usage = RunAgentUsageSnapshot(
        total_input_tokens=record.total_input_tokens,
        total_output_tokens=record.total_output_tokens,
        total_tokens=record.total_tokens,
        llm_call_count=record.llm_call_count,
        lead_agent_tokens=record.lead_agent_tokens,
        subagent_tokens=record.subagent_tokens,
        middleware_tokens=record.middleware_tokens,
        token_usage_by_model=record.token_usage_by_model,
        token_budget_usage=(ctx.token_budget_usage_recorder.snapshot() if ctx.token_budget_usage_recorder is not None else None),
    )
    if record.status is RunStatus.success:
        return RunAgentOutcome.succeeded(
            usage,
            suspended_approval_id=terminal_approval_id,
        )
    if record.status is RunStatus.interrupted:
        return RunAgentOutcome.cancelled(usage)
    if record.status is RunStatus.error:
        error_code = record.error if record.error in STREAM_TERMINAL_ERROR_CODES else "AGENT_EXECUTION_FAILED"
        return RunAgentOutcome.failed(
            usage,
            public_error_code=error_code,
        )
    raise RuntimeError(
        f"Run {run_id} finished without a semantic terminal outcome",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _goal_instance_matches(left: GoalState | None, right: GoalState | None) -> bool:
    if not left or not right:
        return False
    same_status = left.get("status") == right.get("status") == "active"
    same_objective = left.get("objective") == right.get("objective")
    same_created_at = left.get("created_at") == right.get("created_at")
    return same_status and same_objective and same_created_at


async def _materialized_checkpoint_goal(
    accessor: CheckpointStateAccessor,
    thread_id: str,
) -> GoalState | None:
    values = _snapshot_values(await _materialized_checkpoint_snapshot(accessor, thread_id))
    goal = values.get("goal")
    return copy.deepcopy(goal) if isinstance(goal, dict) else None


def _build_run_local_mutation_accessor(
    *,
    accessor: CheckpointStateAccessor,
    checkpointer: Any,
    as_node: str,
    snapshot_frequency: int | None,
) -> CheckpointStateAccessor:
    mutation_graph = build_state_mutation_graph(
        as_node,
        accessor.mode,
        graph_state_schema(accessor.graph),
        snapshot_frequency=snapshot_frequency,
    )
    return CheckpointStateAccessor.bind(
        mutation_graph,
        checkpointer,
        mode=accessor.mode,
    )


async def _write_materialized_goal(
    *,
    accessor: CheckpointStateAccessor,
    checkpointer: Any,
    thread_id: str,
    goal: GoalState | None,
    as_node: str,
    expected_checkpoint_id: str | None,
    snapshot_frequency: int | None,
) -> dict[str, Any]:
    """Replace the goal through a run-local, mode-matched state graph."""

    snapshot = await _materialized_checkpoint_snapshot(accessor, thread_id)
    current_checkpoint_id = _checkpoint_id(snapshot)
    if current_checkpoint_id is None:
        raise LookupError(f"Thread {thread_id} checkpoint not found")
    if expected_checkpoint_id is not None and current_checkpoint_id != expected_checkpoint_id:
        raise GoalWriteConflict(f"Thread {thread_id} goal checkpoint changed while preparing write")

    mutation_accessor = _build_run_local_mutation_accessor(
        accessor=accessor,
        checkpointer=checkpointer,
        as_node=as_node,
        snapshot_frequency=snapshot_frequency,
    )
    await mutation_accessor.aupdate(
        {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
                "checkpoint_id": current_checkpoint_id,
            }
        },
        {"goal": Overwrite(copy.deepcopy(goal))},
        as_node=as_node,
    )
    return _snapshot_values(await _materialized_checkpoint_snapshot(accessor, thread_id))


def _read_checkpoint_goal(checkpoint_tuple: Any) -> GoalState | None:
    checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {}
    channel_values = checkpoint.get("channel_values", {}) if isinstance(checkpoint, dict) else {}
    raw_goal = channel_values.get("goal") if isinstance(channel_values, dict) else None
    return copy.deepcopy(raw_goal) if isinstance(raw_goal, dict) else None


def _has_durable_goal_turn_receipt(checkpoint_tuple: Any, messages: list[Any]) -> bool:
    """Return true when a completed visible assistant turn is safely checkpointed.

    ``pending_writes`` is the durability signal: a ``CheckpointTuple`` carries no
    ``tasks`` field (those live on a ``StateSnapshot``), so the presence of any
    queued writes is what tells us the turn is still in flight.
    """
    if _checkpoint_id(checkpoint_tuple) is None:
        return False
    if getattr(checkpoint_tuple, "pending_writes", None):
        return False
    visible_messages = []
    for message in messages:
        if _is_visible_message(message) and message_to_text(message).strip():
            visible_messages.append(message)
    if not visible_messages:
        return False
    return _message_type(visible_messages[-1]) == "ai"


def _stand_down_reason(goal: GoalState, evaluation: GoalEvaluation, no_progress_count: int) -> str | None:
    if evaluation["satisfied"]:
        return None
    if evaluation["blocker"] != "goal_not_met_yet":
        return f"blocked:{evaluation['blocker']}"
    # Default caps mirror should_continue_goal so the two gate functions agree on
    # a goal dict that is missing these fields.
    if int(goal.get("continuation_count", 0)) >= int(goal.get("max_continuations", DEFAULT_MAX_GOAL_CONTINUATIONS)):
        return "max_continuations_reached"
    if no_progress_count >= int(goal.get("max_no_progress_continuations", DEFAULT_MAX_NO_PROGRESS_CONTINUATIONS)):
        return "no_progress_detected"
    return None


async def _persist_goal_evaluation(
    *,
    bridge: StreamBridge,
    checkpointer: Any,
    accessor: CheckpointStateAccessor | None = None,
    thread_id: str,
    run_id: str,
    goal: GoalState,
    evaluation: GoalEvaluation,
    no_progress_count: int,
    continuation_count: int | None = None,
    stand_down_reason: str | None = None,
    evidence_signature: str = "",
    snapshot_frequency: int | None = None,
) -> GoalState | None:
    try:
        async with goal_thread_lock(thread_id):
            if accessor is not None:
                snapshot = await _materialized_checkpoint_snapshot(
                    accessor,
                    thread_id,
                )
                current_goal = _snapshot_values(snapshot).get("goal")
                current_goal = copy.deepcopy(current_goal) if isinstance(current_goal, dict) else None
                expected_checkpoint_id = _checkpoint_id(snapshot)
            else:
                checkpoint_tuple = await _call_checkpointer_method(
                    checkpointer,
                    "aget_tuple",
                    "get_tuple",
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": "",
                        }
                    },
                )
                if checkpoint_tuple is None:
                    return None
                current_goal = _read_checkpoint_goal(checkpoint_tuple)
                expected_checkpoint_id = _checkpoint_id(checkpoint_tuple)
            if current_goal is None or not _goal_instance_matches(goal, current_goal):
                return None
            # The caller may have computed its next count before another
            # continuation committed. Advance from the fresh, locked value so
            # a stale writer cannot overwrite or collapse a real attempt.
            if continuation_count is not None:
                current_count = int(current_goal.get("continuation_count", 0))
                continuation_count = max(continuation_count, current_count + 1)
            updated_goal = attach_goal_evaluation(
                current_goal,
                evaluation,
                run_id=run_id,
                continuation_count=continuation_count,
                no_progress_count=no_progress_count,
                stand_down_reason=stand_down_reason,
                evidence_signature=evidence_signature,
            )
            if accessor is not None:
                values = await _write_materialized_goal(
                    accessor=accessor,
                    checkpointer=checkpointer,
                    thread_id=thread_id,
                    goal=updated_goal,
                    as_node="goal_evaluator",
                    expected_checkpoint_id=expected_checkpoint_id,
                    snapshot_frequency=snapshot_frequency,
                )
            else:
                values = await write_thread_goal(
                    checkpointer,
                    thread_id,
                    updated_goal,
                    as_node="goal_evaluator",
                    expected_checkpoint_id=expected_checkpoint_id,
                )
        await bridge.publish(run_id, "values", serialize(values, mode="values"))
        return updated_goal
    except GoalWriteConflict:
        return None
    except Exception:
        logger.warning("Could not persist goal evaluation for thread %s", thread_id, exc_info=True)
        return None


async def _reread_goal_and_checkpoint(
    checkpointer: Any,
    thread_id: str,
    *,
    accessor: CheckpointStateAccessor | None = None,
) -> tuple[GoalState | None, Any]:
    """Re-read the goal and latest checkpoint together for a concurrency re-check."""
    goal = await _materialized_checkpoint_goal(accessor, thread_id) if accessor is not None else await read_thread_goal(checkpointer, thread_id)
    checkpoint_tuple = await _call_checkpointer_method(
        checkpointer,
        "aget_tuple",
        "get_tuple",
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
    )
    return goal, checkpoint_tuple


async def _prepare_goal_continuation_input(
    *,
    bridge: StreamBridge,
    checkpointer: Any,
    accessor: CheckpointStateAccessor | None = None,
    thread_id: str,
    run_id: str,
    model_name: str | None,
    app_config: AppConfig | None,
    snapshot_frequency: int | None = None,
    evaluator_model_factory: Any | None = None,
    abort_event: asyncio.Event | None = None,
    authorization_boundary: object | None = None,
) -> dict[str, Any] | None:
    """Evaluate the active goal and return a hidden continuation input if needed.

    NOTE: The re-reads below catch a racing user message or ``/goal clear``
    before we queue a continuation. Goal writes then serialize per thread and
    pass the checkpoint id they read from, so stale evaluator writes stand down
    instead of clobbering a newer goal change.
    """
    if checkpointer is None:
        return None
    if abort_event is not None and abort_event.is_set():
        return None

    try:
        goal = await _materialized_checkpoint_goal(accessor, thread_id) if accessor is not None else await read_thread_goal(checkpointer, thread_id)
    except Exception:
        logger.warning("Could not read goal for thread %s after run %s", thread_id, run_id, exc_info=True)
        return None
    if not goal or goal.get("status") != "active":
        return None

    async def _persist(
        goal: GoalState,
        evaluation: GoalEvaluation,
        no_progress_count: int,
        *,
        stand_down_reason: str | None = None,
        continuation_count: int | None = None,
    ) -> GoalState | None:
        """Record the evaluation against the still-current goal instance."""
        return await _persist_goal_evaluation(
            bridge=bridge,
            checkpointer=checkpointer,
            accessor=accessor,
            thread_id=thread_id,
            run_id=run_id,
            goal=goal,
            evaluation=evaluation,
            no_progress_count=no_progress_count,
            continuation_count=continuation_count,
            stand_down_reason=stand_down_reason,
            evidence_signature=evidence_signature,
            snapshot_frequency=snapshot_frequency,
        )

    try:
        checkpoint_tuple = await _call_checkpointer_method(
            checkpointer,
            "aget_tuple",
            "get_tuple",
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
        )
        if checkpoint_tuple is None:
            return None
        checkpoint_id_before = _checkpoint_id(checkpoint_tuple)
        messages = await _materialized_checkpoint_messages(accessor, thread_id) if accessor is not None else _read_checkpoint_messages(checkpoint_tuple)
        conversation_signature_before = visible_conversation_signature(messages)
        evidence_signature = latest_visible_assistant_signature(messages)

        if not _has_durable_goal_turn_receipt(checkpoint_tuple, messages):
            evaluation = GoalEvaluation(
                satisfied=False,
                blocker="run_failed",
                reason="No durable assistant end-of-turn receipt was available.",
                evidence_summary="",
            )
            no_progress_count = compute_no_progress_count(goal, evaluation, evidence_signature=evidence_signature)
            await _persist(goal, evaluation, no_progress_count, stand_down_reason="no_durable_end_of_turn")
            return None

        if abort_event is not None and abort_event.is_set():
            return None
        evaluator_model = evaluator_model_factory() if evaluator_model_factory is not None else None
        evaluation = await evaluate_goal_completion(
            goal,
            messages,
            model=evaluator_model,
            model_name=model_name,
            app_config=app_config,
            authorization_boundary=authorization_boundary,
            abort_event=abort_event,
        )
        if abort_event is not None and abort_event.is_set():
            return None
    except AuthorizationRevoked:
        raise
    except Exception:
        logger.warning("Goal evaluator failed for thread %s after run %s", thread_id, run_id, exc_info=True)
        return None

    no_progress_count = compute_no_progress_count(goal, evaluation, evidence_signature=evidence_signature)

    # Re-check that neither the goal nor the visible conversation changed while the
    # evaluator ran — a user message or /goal clear racing the evaluation must win.
    try:
        current_goal, current_checkpoint_tuple = await _reread_goal_and_checkpoint(
            checkpointer,
            thread_id,
            accessor=accessor,
        )
    except Exception:
        logger.warning("Could not re-check goal state for thread %s after evaluation", thread_id, exc_info=True)
        return None

    if not _goal_instance_matches(goal, current_goal) or current_checkpoint_tuple is None:
        return None

    checkpoint_changed = _checkpoint_id(current_checkpoint_tuple) != checkpoint_id_before
    current_messages = await _materialized_checkpoint_messages(accessor, thread_id) if accessor is not None else _read_checkpoint_messages(current_checkpoint_tuple)
    messages_changed = visible_conversation_signature(current_messages) != conversation_signature_before
    if checkpoint_changed or messages_changed:
        await _persist(current_goal, evaluation, no_progress_count, stand_down_reason="thread_changed_after_evaluation")
        return None

    if evaluation["satisfied"]:
        try:
            async with goal_thread_lock(thread_id):
                latest_checkpoint_tuple = await _call_checkpointer_method(
                    checkpointer,
                    "aget_tuple",
                    "get_tuple",
                    {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
                )
                if latest_checkpoint_tuple is None:
                    return None
                latest_goal = (
                    await _materialized_checkpoint_goal(
                        accessor,
                        thread_id,
                    )
                    if accessor is not None
                    else _read_checkpoint_goal(latest_checkpoint_tuple)
                )
                if latest_goal is None or not _goal_instance_matches(goal, latest_goal):
                    return None
                if accessor is not None:
                    values = await _write_materialized_goal(
                        accessor=accessor,
                        checkpointer=checkpointer,
                        thread_id=thread_id,
                        goal=None,
                        as_node="goal_evaluator",
                        expected_checkpoint_id=_checkpoint_id(latest_checkpoint_tuple),
                        snapshot_frequency=snapshot_frequency,
                    )
                else:
                    values = await write_thread_goal(
                        checkpointer,
                        thread_id,
                        None,
                        as_node="goal_evaluator",
                        expected_checkpoint_id=_checkpoint_id(latest_checkpoint_tuple),
                    )
            await bridge.publish(run_id, "values", serialize(values, mode="values"))
        except GoalWriteConflict:
            return None
        except Exception:
            logger.warning("Could not clear satisfied goal for thread %s", thread_id, exc_info=True)
        return None

    stand_down_reason = _stand_down_reason(goal, evaluation, no_progress_count)
    if stand_down_reason is not None or not should_continue_goal(goal, evaluation, no_progress_count=no_progress_count):
        await _persist(goal, evaluation, no_progress_count, stand_down_reason=stand_down_reason)
        return None

    next_count = int(goal.get("continuation_count", 0)) + 1
    updated_goal = await _persist(goal, evaluation, no_progress_count, continuation_count=next_count)
    if updated_goal is None:
        return None

    # Final guard: the persist above bumped the checkpoint id, so only the visible
    # conversation signature is meaningful for detecting a racing user turn here.
    try:
        latest_goal, latest_checkpoint_tuple = await _reread_goal_and_checkpoint(
            checkpointer,
            thread_id,
            accessor=accessor,
        )
    except Exception:
        logger.warning("Could not verify queued goal continuation for thread %s", thread_id, exc_info=True)
        return None
    if not _goal_instance_matches(updated_goal, latest_goal) or latest_checkpoint_tuple is None:
        return None
    latest_messages = await _materialized_checkpoint_messages(accessor, thread_id) if accessor is not None else _read_checkpoint_messages(latest_checkpoint_tuple)
    if visible_conversation_signature(latest_messages) != conversation_signature_before:
        # The first persist already counted this continuation attempt. This
        # second write only records why delivery stood down; passing the same
        # count again would make the fresh-count race guard add a second unit.
        await _persist(
            latest_goal,
            evaluation,
            no_progress_count,
            stand_down_reason="thread_changed_before_continuation",
        )
        return None

    logger.info(
        "Run %s continuing thread %s for active goal (%d/%d)",
        run_id,
        thread_id,
        updated_goal.get("continuation_count", next_count),
        updated_goal.get("max_continuations", 0),
    )
    return {"messages": [make_goal_continuation_message(updated_goal, evaluation)]}
