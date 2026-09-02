"""Run-local runtime context, RunContext dependencies, and Agent factory binding."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache, partial
from typing import Any, Protocol, cast

from deerflow.agents.middlewares.tool_call_control import (
    ResolvedGraphToolCallControlProfile,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.database_config import CheckpointChannelMode
from deerflow.file_authority import RunFileAuthority
from deerflow.runtime.context_carrier import RuntimeContextCarrier
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.recovered_llm_failures import RunRecoveredLLMFailureRecorder
from deerflow.runtime.user_context import DEFAULT_USER_ID, get_current_user
from deerflow.sandbox.sandbox_provider import RunScopedReadOnlyMount
from deerflow.subagents.runtime_catalog import trusted_runtime_agent_catalog
from deerflow.token_budget_usage import TokenBudgetUsageRecorder
from deerflow.trace_context import get_current_trace_id, normalize_trace_id

from .execution_contracts import RunAgentResourceOwnership, RunSemanticStopRecorder
from .manager import RunRecord

__all__ = ["BoundRunRuntime", "PrivateAgentRuntime", "PrivateRuntimeFactoryUnavailable", "RunContext", "bind_run_runtime_context"]


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


@dataclass(frozen=True, slots=True)
class BoundRunRuntime:
    """Runtime context installed into one Run's config before graph construction."""

    runtime_context: dict[str, Any]
    trace_id: str | None


def bind_run_runtime_context(
    *,
    ctx: RunContext,
    record: RunRecord,
    config: dict[str, Any],
    private_owner_user_id: str | None,
    file_authority: object | None,
    private_files_enabled: bool,
    journal: Any | None,
    token_usage_tracking_enabled: bool,
    recovered_llm_failure_recorder: RunRecoveredLLMFailureRecorder,
    semantic_stop_recorder: RunSemanticStopRecorder,
    pre_existing_message_ids: set[str],
) -> BoundRunRuntime:
    """Build and install ``ToolRuntime.context`` and the parent ``Runtime`` for one Run.

    Mutates ``config`` exactly as the inline phase did: installs the sanitized
    runtime context, stores the parent runtime under
    ``configurable["__pregel_runtime"]``, and re-asserts the persisted private
    model name so absent or forged caller config cannot influence the private
    runtime factory.
    """
    from langgraph.runtime import Runtime

    run_id = record.run_id
    thread_id = record.thread_id
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
        file_authority=file_authority,
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
            if (not private_files_enabled and ctx.private_agent_runtime is not None and ctx.app_config is not None)
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
    runtime = Runtime(context=cast(Any, runtime_ctx), store=ctx.store)
    configurable = config.setdefault("configurable", {})
    configurable["__pregel_runtime"] = runtime
    if ctx.private_agent_runtime is not None:
        # Private admission persists the exact configured model UUID on
        # the Run.  Reassert that authoritative value at the Worker boundary
        # so absent or forged caller config cannot influence the private
        # runtime factory.  ``None`` remains a fail-closed value.
        configurable[RuntimeContextKeys.MODEL_NAME] = record.model_name
    return BoundRunRuntime(runtime_context=runtime_ctx, trace_id=deerflow_trace_id)
