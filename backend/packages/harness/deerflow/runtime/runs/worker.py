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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache, partial
from typing import Any, Literal, Protocol, cast

from langgraph.checkpoint.base import empty_checkpoint

from deerflow.agents.goal_state import GoalEvaluation, GoalState
from deerflow.config.app_config import AppConfig
from deerflow.file_authority import RunFileAuthority
from deerflow.runtime.context_keys import CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY
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
from deerflow.runtime.serialization import serialize
from deerflow.runtime.user_context import DEFAULT_USER_ID, get_current_user, get_effective_user_id
from deerflow.sandbox.sandbox import (
    AUTHORIZATION_REVOKED_REASON,
    AuthorizationRevoked,
)
from deerflow.sandbox.sandbox_provider import RunScopedReadOnlyMount, get_sandbox_provider
from deerflow.trace_context import DEERFLOW_TRACE_METADATA_KEY, get_current_trace_id, normalize_trace_id
from deerflow.tracing import inject_langfuse_metadata
from deerflow.utils.messages import message_to_text
from deerflow.workspace_changes import (
    WORKSPACE_CHANGES_EVENT_TYPE,
    WORKSPACE_CHANGES_METADATA_KEY,
    capture_workspace_snapshot,
    record_workspace_changes,
    trusted_workspace_change_result,
)
from deerflow.workspace_changes.types import WorkspaceSnapshot

from .manager import RunManager, RunRecord
from .naming import resolve_root_run_name
from .schemas import RunStatus

logger = logging.getLogger(__name__)

# Valid stream_mode values for LangGraph's graph.astream()
_VALID_LG_MODES = {"values", "updates", "checkpoints", "tasks", "debug", "messages", "custom"}
_PRIVATE_CLEANUP_MAX_ATTEMPTS = 3
_PRIVATE_RUN_MESSAGE_BOUNDARY_ERROR = "Private Run pre-run message boundary is unavailable"


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
    run_read_only_mounts: tuple[object, ...] = (),
    runtime_owner_user_id: str | None = None,
) -> dict[str, Any]:
    """Build the dict that becomes ``ToolRuntime.context`` for the run.

    Always includes ``thread_id`` and ``run_id``. Additional keys from the caller's
    ``config['context']`` (e.g. ``agent_name`` for the bootstrap flow — issue #2677)
    are merged in but never override ``thread_id``/``run_id``. The resolved
    ``AppConfig`` is added by the worker so tools can consume it without ambient
    global lookups.

    langgraph 1.1+ surfaces this as ``runtime.context`` via the parent runtime stored
    under ``config['configurable']['__pregel_runtime']`` — see
    ``langgraph.pregel.main`` where ``parent_runtime.merge(...)`` is invoked.
    """
    runtime_ctx: dict[str, Any] = {"thread_id": thread_id, "run_id": run_id}
    if isinstance(caller_context, dict):
        for key, value in caller_context.items():
            if key in {
                CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY,
                "stop_reason",
            }:
                continue
            runtime_ctx.setdefault(key, value)
    if app_config is not None:
        runtime_ctx["app_config"] = app_config
    if private_scope is not None:
        runtime_ctx["private_scope"] = private_scope
    if authorization_checker is not None:
        runtime_ctx["__authorization_checker"] = authorization_checker
    if authorization_boundary is not None:
        runtime_ctx["__authorization_boundary"] = authorization_boundary
    if file_authority is not None:
        runtime_ctx["__file_authority"] = file_authority
    if run_read_only_mounts:
        runtime_ctx["__run_read_only_mounts"] = run_read_only_mounts
    if runtime_owner_user_id is not None:
        runtime_ctx["user_id"] = runtime_owner_user_id
    return runtime_ctx


class PrivateAgentRuntime(Protocol):
    skill_root: Any
    skills: tuple[Any, ...]
    prompt_bundle: Any

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
    private_agent_runtime: PrivateAgentRuntime | None = field(default=None)


def _install_runtime_context(config: dict, runtime_context: dict[str, Any]) -> None:
    existing_context = config.get("context")
    if isinstance(existing_context, dict):
        # Both are Worker-owned ephemeral values. Never retain a client value
        # or a stale value from a reused config object.
        existing_context.pop(CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY, None)
        existing_context.pop("stop_reason", None)
        if "private_scope" in runtime_context or "__run_read_only_mounts" in runtime_context:
            existing_context["thread_id"] = runtime_context["thread_id"]
            existing_context["run_id"] = runtime_context["run_id"]
            if "user_id" in runtime_context:
                existing_context["user_id"] = runtime_context["user_id"]
        else:
            existing_context.setdefault("thread_id", runtime_context["thread_id"])
            existing_context.setdefault("run_id", runtime_context["run_id"])
        if DEERFLOW_TRACE_METADATA_KEY in runtime_context:
            existing_context.setdefault(DEERFLOW_TRACE_METADATA_KEY, runtime_context[DEERFLOW_TRACE_METADATA_KEY])
        if "app_config" in runtime_context:
            existing_context["app_config"] = runtime_context["app_config"]
        for key in (
            "model_name",
            "private_scope",
            "__authorization_checker",
            "__authorization_boundary",
            "__file_authority",
            "__run_read_only_mounts",
            "__agent_prompt_bundle",
            "__runtime_skills",
            "__runtime_mcp_tools",
            "__skill_scoped_secrets",
            "__skill_secret_provider",
            CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY,
        ):
            if key in runtime_context:
                existing_context[key] = runtime_context[key]
        return

    config["context"] = {key: value for key, value in runtime_context.items() if key != "stop_reason"}


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
) -> Any:
    """Build a synchronous graph without blocking the Gateway event loop."""

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
        return agent_factory(**kwargs)

    return await asyncio.to_thread(_build)


class _SubagentEventBuffer:
    """Buffer subagent ``task_*`` step events and flush them in one locked batch (#3779).

    The live SSE bridge already forwards these events for real-time display; this
    additionally writes them so the subtask card's step history survives a reload.

    ``RunEventStore.put`` is documented as a low-frequency path — on Postgres each
    call opens its own transaction and takes a per-thread advisory lock. A deep
    subagent (``general-purpose`` runs up to ``max_turns=150``) emits hundreds of
    ``task_running`` steps on the hot stream loop, so persisting each with
    ``put()`` would serialize against the run's own message-batch writer. This
    accumulates recognized subagent events and writes them with ``put_batch``,
    which acquires the lock once per batch, honoring the store's contract.

    Best-effort: a missing store (run_events not configured) or an unrecognized
    chunk is a no-op, flush failures are logged but never propagate into the
    stream loop, and terminal ``subagent.end`` events flush eagerly so a completed
    subagent's step history is durable promptly rather than only at run end.
    """

    #: Flush once this many events are buffered, bounding memory and reload lag on
    #: a single deep subagent without paying a per-step lock.
    FLUSH_THRESHOLD = 25

    def __init__(self, event_store: Any | None, thread_id: str, run_id: str, scope: Any | None = None) -> None:
        self._event_store = event_store
        self._thread_id = thread_id
        self._run_id = run_id
        self._scope = scope
        self._pending: list[dict[str, Any]] = []

    async def add(self, chunk: Any) -> None:
        """Buffer one custom stream chunk; flush on a terminal event or threshold."""
        if self._event_store is None:
            return
        # Lazy import: importing deerflow.subagents at module load triggers its
        # package __init__ (executor → agents → tools → task_tool), which imports
        # back from deerflow.subagents and deadlocks at gateway startup. Deferring
        # it to call time (after all modules are loaded) breaks that cycle.
        from deerflow.subagents.step_events import subagent_run_event

        record = subagent_run_event(chunk)
        if record is None:
            return
        self._pending.append({"thread_id": self._thread_id, "run_id": self._run_id, **record})
        if record["event_type"] == "subagent.end" or len(self._pending) >= self.FLUSH_THRESHOLD:
            await self.flush()

    async def flush(self) -> None:
        """Persist buffered events in one ``put_batch`` call; swallow store errors."""
        if self._event_store is None or not self._pending:
            return
        batch = self._pending
        self._pending = []
        try:
            if self._scope is None:
                await self._event_store.put_batch(batch)
            else:
                await self._event_store.put_batch(batch, scope=self._scope)
        except Exception:
            logger.warning("Run %s: failed to persist %d subagent step event(s)", self._run_id, len(batch), exc_info=True)


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
) -> None:
    """Execute an agent in the background, publishing events to *bridge*."""

    # Unpack infrastructure dependencies from RunContext.
    checkpointer = ctx.checkpointer
    store = ctx.store
    event_store = ctx.event_store
    run_events_config = ctx.run_events_config
    thread_store = ctx.thread_store

    run_id = record.run_id
    thread_id = record.thread_id
    private_owner_user_id = record.scope.owner_user_id if ctx.private_agent_runtime is not None and record.scope is not None else None
    requested_modes: set[str] = set(stream_modes or ["values"])
    pre_run_checkpoint_id: str | None = None
    pre_run_snapshot: dict[str, Any] | None = None
    pre_run_workspace_snapshot: WorkspaceSnapshot | None = None
    workspace_changes_user_id: str | None = None
    run_mounts: tuple[RunScopedReadOnlyMount, ...] = ()
    run_mount_provider: Any | None = None
    run_mount_user_id: str | None = None
    snapshot_capture_failed = False
    llm_error_fallback_message: str | None = None
    # Message ids checkpointed *before* this run started. The stream loop uses
    # this set to mask out ``deerflow_error_fallback`` markers that belong to
    # earlier runs on the same thread — without it, one stale fallback in
    # history would mark every subsequent run on this thread as ``error``.
    pre_existing_message_ids: set[str] = set()
    private_message_boundary_required = ctx.private_scope is not None or record.scope is not None

    journal = None
    # Buffers subagent step events for batched persistence (#3779); assigned once
    # streaming starts and flushed in the finally block. Pre-bound to None so the
    # finally is safe even if an exception fires before streaming begins.
    subagent_events: _SubagentEventBuffer | None = None
    private_files_restored = False
    private_files_finalized = False
    private_files_failed = False
    private_cleanup_cancellation_pending = False
    private_finalization_result: object | None = None

    async def _abort_private_files() -> None:
        """Durably fail private finalization before publishing a terminal run."""

        nonlocal private_files_failed, private_cleanup_cancellation_pending
        authority = ctx.file_authority
        if authority is None or private_files_finalized or private_files_failed:
            return
        task = asyncio.create_task(authority.mark_failed())
        cancellation_pending = False
        try:
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise
                    cancellation_pending = True
            task.result()
        except Exception:
            logger.warning(
                "Private file finalization failure marker failed for run %s",
                run_id,
                exc_info=True,
            )
        finally:
            private_files_failed = True
            private_cleanup_cancellation_pending |= cancellation_pending

    async def _finalize_private_files() -> None:
        nonlocal private_files_finalized, private_finalization_result
        authority = ctx.file_authority
        if authority is None or not private_files_restored or private_files_finalized:
            return
        await run_manager.set_finalizing(run_id, True)
        task = asyncio.create_task(authority.finalize())
        cancellation_pending = False
        try:
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise
                    cancellation_pending = True
                    continue
        except BaseException:
            await _abort_private_files()
            raise
        private_finalization_result = task.result()
        private_files_finalized = True
        if cancellation_pending:
            raise asyncio.CancelledError

    async def _join_private_cleanup(
        operation: Callable[[], Awaitable[Any]],
        *,
        failure_message: str,
    ) -> bool:
        """Join and retry one private cleanup despite repeated cancellation."""

        nonlocal private_cleanup_cancellation_pending
        cancellation_pending = False
        try:
            for attempt in range(1, _PRIVATE_CLEANUP_MAX_ATTEMPTS + 1):
                task = asyncio.create_task(operation())
                try:
                    while True:
                        try:
                            await asyncio.shield(task)
                            break
                        except asyncio.CancelledError:
                            if task.cancelled():
                                logger.warning(
                                    "%s (attempt %d/%d)",
                                    failure_message,
                                    attempt,
                                    _PRIVATE_CLEANUP_MAX_ATTEMPTS,
                                )
                                break
                            cancellation_pending = True
                    if task.cancelled():
                        continue
                    task.result()
                    return True
                except Exception:
                    logger.warning(
                        "%s (attempt %d/%d)",
                        failure_message,
                        attempt,
                        _PRIVATE_CLEANUP_MAX_ATTEMPTS,
                    )
            return False
        finally:
            private_cleanup_cancellation_pending |= cancellation_pending

    # Track whether "events" was requested but skipped
    if "events" in requested_modes:
        logger.info(
            "Run %s: 'events' stream_mode not supported in gateway (requires astream_events + checkpoint callbacks). Skipping.",
            run_id,
        )

    try:
        if ctx.file_authority is not None:
            await run_manager.set_finalizing(run_id, True)
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
                track_token_usage=getattr(run_events_config, "track_token_usage", True),
                progress_reporter=lambda snapshot: run_manager.update_run_progress(run_id, **snapshot),
                scope=record.scope,
            )

        # 1. Mark running
        await run_manager.set_status(run_id, RunStatus.running)

        if ctx.file_authority is not None:
            restore = getattr(ctx.file_authority, "restore", None)
            if not callable(restore):
                raise RuntimeError("Private file authority is unavailable")
            await restore()
            private_files_restored = True

        if event_store is not None and ctx.file_authority is None:
            workspace_changes_user_id = private_owner_user_id or get_effective_user_id()
            try:
                pre_run_workspace_snapshot = await capture_workspace_snapshot(
                    thread_id,
                    user_id=workspace_changes_user_id,
                )
            except Exception:
                logger.warning("Could not capture pre-run workspace snapshot for run %s", run_id, exc_info=True)

        # Snapshot the latest pre-run checkpoint so rollback can restore it.
        if checkpointer is not None:
            try:
                config_for_check = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
                ckpt_tuple = await checkpointer.aget_tuple(config_for_check)
                if ckpt_tuple is not None:
                    ckpt_config = getattr(ckpt_tuple, "config", {}).get("configurable", {})
                    pre_run_checkpoint_id = ckpt_config.get("checkpoint_id")
                    pre_run_snapshot = {
                        "checkpoint_ns": ckpt_config.get("checkpoint_ns", ""),
                        "checkpoint": copy.deepcopy(getattr(ckpt_tuple, "checkpoint", {})),
                        "metadata": copy.deepcopy(getattr(ckpt_tuple, "metadata", {})),
                        "pending_writes": copy.deepcopy(getattr(ckpt_tuple, "pending_writes", []) or []),
                    }
                    pre_existing_message_ids = _collect_private_pre_existing_message_ids(pre_run_snapshot) if private_message_boundary_required else _collect_pre_existing_message_ids(pre_run_snapshot)
            except Exception:
                snapshot_capture_failed = True
                if private_message_boundary_required:
                    logger.warning(
                        "Private Run pre-run message boundary is unavailable for run %s",
                        run_id,
                    )
                    raise RuntimeError(_PRIVATE_RUN_MESSAGE_BOUNDARY_ERROR) from None
                logger.warning(
                    "Could not capture pre-run checkpoint snapshot for run %s",
                    run_id,
                    exc_info=True,
                )

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
            file_authority=ctx.file_authority,
            run_read_only_mounts=(
                (
                    RunScopedReadOnlyMount(
                        run_id=run_id,
                        container_path=ctx.app_config.skills.container_path,
                        host_path=str(ctx.private_agent_runtime.skill_root),
                    ),
                )
                if (ctx.file_authority is None and ctx.private_agent_runtime is not None and ctx.app_config is not None)
                else ()
            ),
            runtime_owner_user_id=private_owner_user_id,
        )
        if ctx.private_agent_runtime is not None:
            # Context is merged after configurable by the Agent factory. Keep
            # both channels pinned to the same persisted private-Run model.
            runtime_ctx["model_name"] = record.model_name
            prompt_bundle = getattr(ctx.private_agent_runtime, "prompt_bundle", None)
            if prompt_bundle is not None:
                runtime_ctx["__agent_prompt_bundle"] = prompt_bundle
            runtime_ctx["__runtime_skills"] = tuple(getattr(ctx.private_agent_runtime, "skills", ()))
            runtime_ctx["__runtime_mcp_tools"] = tuple(getattr(ctx.private_agent_runtime, "mcp_tools", ()))
            skill_secret_provider = getattr(
                ctx.private_agent_runtime,
                "materialize_skill_scoped_secrets",
                None,
            )
            skill_container_path = getattr(ctx.app_config.skills, "container_path", None) if ctx.app_config is not None else None
            if callable(skill_secret_provider) and isinstance(skill_container_path, str):
                runtime_ctx["__skill_secret_provider"] = partial(
                    skill_secret_provider,
                    skill_container_path,
                )
        runtime_ctx[CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY] = frozenset(pre_existing_message_ids)
        incoming_metadata = config.get("metadata") if isinstance(config.get("metadata"), dict) else {}
        deerflow_trace_id = normalize_trace_id(incoming_metadata.get(DEERFLOW_TRACE_METADATA_KEY)) or get_current_trace_id()
        if deerflow_trace_id:
            runtime_ctx[DEERFLOW_TRACE_METADATA_KEY] = deerflow_trace_id
        # Expose the run-scoped journal under a sentinel key so middleware can
        # write audit events (e.g. SafetyFinishReasonMiddleware recording
        # suppressed tool calls). Double-underscore prefix marks it as a
        # runtime-internal channel; user code must not depend on the key name.
        if journal is not None:
            runtime_ctx["__run_journal"] = journal
        _install_runtime_context(config, runtime_ctx)
        runtime = Runtime(context=cast(Any, runtime_ctx), store=store)
        configurable = config.setdefault("configurable", {})
        configurable["__pregel_runtime"] = runtime
        if ctx.private_agent_runtime is not None:
            # Private admission persists the exact configured logical model on
            # the Run.  Reassert that authoritative value at the Worker boundary
            # so absent or forged caller config cannot influence the private
            # runtime factory.  ``None`` remains a fail-closed value.
            configurable["model_name"] = record.model_name

        run_mounts = runtime_ctx.get("__run_read_only_mounts", ())
        if run_mounts:
            run_mount_provider = get_sandbox_provider()
            run_mount_user_id = private_owner_user_id or get_effective_user_id()
            await asyncio.to_thread(
                run_mount_provider.validate_run_scoped_mounts,
                thread_id,
                user_id=run_mount_user_id,
                mounts=run_mounts,
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
            environment=os.environ.get("DEER_FLOW_ENV") or os.environ.get("ENVIRONMENT"),
            deerflow_trace_id=deerflow_trace_id,
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

        agent = await _call_agent_factory_off_loop(
            agent_factory,
            initial_runnable_config,
            ctx.app_config,
            ctx.private_agent_runtime,
        )

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

        # 4. Attach checkpointer and store
        if checkpointer is not None:
            agent.checkpointer = checkpointer
        if store is not None:
            agent.store = store

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
        if not lg_modes:
            lg_modes = ["values"]

        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for m in lg_modes:
            if m not in seen:
                seen.add(m)
                deduped.append(m)
        lg_modes = deduped

        logger.info("Run %s: streaming with modes %s (requested: %s)", run_id, lg_modes, requested_modes)

        # Buffer subagent step events and persist them in batches (#3779) instead
        # of one low-frequency put() per step on the hot stream loop. Flushed in
        # the finally block so buffered steps survive abort/exception paths too.
        subagent_events = _SubagentEventBuffer(event_store, thread_id, run_id, record.scope)

        goal_evaluator_model: Any | None = None

        def _get_goal_evaluator_model() -> Any:
            nonlocal goal_evaluator_model
            if goal_evaluator_model is None:
                goal_evaluator_model = create_goal_evaluator_model(
                    model_name=record.model_name,
                    app_config=ctx.app_config,
                )
            return goal_evaluator_model

        async def _stream_once(input_payload: Any, stream_config: RunnableConfig) -> None:
            nonlocal llm_error_fallback_message
            if len(lg_modes) == 1 and not stream_subgraphs:
                # Single mode, no subgraphs: astream yields raw chunks
                single_mode = lg_modes[0]
                async for chunk in agent.astream(input_payload, config=stream_config, stream_mode=single_mode):
                    if record.abort_event.is_set():
                        logger.info("Run %s abort requested — stopping", run_id)
                        break
                    llm_error_fallback_message = llm_error_fallback_message or _extract_llm_error_fallback_message(chunk, pre_existing_message_ids)
                    sse_event = _lg_mode_to_sse_event(single_mode)
                    await bridge.publish(run_id, sse_event, serialize(chunk, mode=single_mode))
                    if single_mode == "custom":
                        await subagent_events.add(chunk)
                return
            # Multiple modes or subgraphs: astream yields tuples
            async for item in agent.astream(
                input_payload,
                config=stream_config,
                stream_mode=lg_modes,
                subgraphs=stream_subgraphs,
            ):
                if record.abort_event.is_set():
                    logger.info("Run %s abort requested — stopping", run_id)
                    break

                namespace, mode, chunk = _unpack_stream_item(item, lg_modes, stream_subgraphs)
                if mode is None:
                    continue

                llm_error_fallback_message = llm_error_fallback_message or _extract_llm_error_fallback_message(chunk, pre_existing_message_ids)
                sse_event = _namespaced_sse_event(mode, namespace)
                await bridge.publish(run_id, sse_event, serialize(chunk, mode=mode))
                if mode == "custom":
                    await subagent_events.add(chunk)

        # 7. Stream the requested turn, then optionally continue hidden goal turns.
        await _stream_once(graph_input, initial_runnable_config)
        while not record.abort_event.is_set() and not llm_error_fallback_message and (journal is None or not journal.had_llm_error_fallback):
            continuation_input = await _prepare_goal_continuation_input(
                bridge=bridge,
                checkpointer=checkpointer,
                thread_id=thread_id,
                run_id=run_id,
                model_name=record.model_name,
                app_config=ctx.app_config,
                evaluator_model_factory=_get_goal_evaluator_model,
                abort_event=record.abort_event,
                authorization_boundary=ctx.authorization_boundary,
            )
            if continuation_input is None or record.abort_event.is_set():
                break
            await _stream_once(continuation_input, _continuation_runnable_config())

        # 8. Final status
        if record.abort_event.is_set():
            await run_manager.set_finalizing(run_id, True)
            action = record.abort_action
            if action == "rollback":
                await _abort_private_files()
                await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
                try:
                    await _rollback_to_pre_run_checkpoint(
                        checkpointer=checkpointer,
                        thread_id=thread_id,
                        run_id=run_id,
                        pre_run_checkpoint_id=pre_run_checkpoint_id,
                        pre_run_snapshot=pre_run_snapshot,
                        snapshot_capture_failed=snapshot_capture_failed,
                    )
                    logger.info("Run %s rolled back to pre-run checkpoint %s", run_id, pre_run_checkpoint_id)
                except Exception:
                    logger.warning("Failed to rollback checkpoint for run %s", run_id, exc_info=True)
            else:
                if action != "authorization_revoked":
                    await _finalize_private_files()
                else:
                    await _abort_private_files()
                await run_manager.set_status(
                    run_id,
                    RunStatus.interrupted,
                    error=(AUTHORIZATION_REVOKED_REASON if action == "authorization_revoked" else None),
                )
        elif llm_error_fallback_message or (journal is not None and journal.had_llm_error_fallback):
            error_msg = llm_error_fallback_message
            if error_msg is None and journal is not None:
                error_msg = journal.llm_error_fallback_message
            error_msg = error_msg or "LLM provider failed after retries"
            await _abort_private_files()
            await run_manager.set_status(run_id, RunStatus.error, error=error_msg)
        else:
            await _finalize_private_files()
            await run_manager.set_status(run_id, RunStatus.success)

    except asyncio.CancelledError:
        await run_manager.set_finalizing(run_id, True)
        action = record.abort_action
        if action == "rollback":
            await _abort_private_files()
            await run_manager.set_status(run_id, RunStatus.error, error="Rolled back by user")
            try:
                await _rollback_to_pre_run_checkpoint(
                    checkpointer=checkpointer,
                    thread_id=thread_id,
                    run_id=run_id,
                    pre_run_checkpoint_id=pre_run_checkpoint_id,
                    pre_run_snapshot=pre_run_snapshot,
                    snapshot_capture_failed=snapshot_capture_failed,
                )
                logger.info("Run %s was cancelled and rolled back", run_id)
            except Exception:
                logger.warning("Run %s cancellation rollback failed", run_id, exc_info=True)
        else:
            if action != "authorization_revoked":
                await _finalize_private_files()
            else:
                await _abort_private_files()
            await run_manager.set_status(
                run_id,
                RunStatus.interrupted,
                error=(AUTHORIZATION_REVOKED_REASON if action == "authorization_revoked" else None),
            )
            logger.info("Run %s was cancelled", run_id)
        if ctx.file_authority is not None:
            raise

    except AuthorizationRevoked:
        record.abort_action = "authorization_revoked"
        record.abort_event.set()
        await _abort_private_files()
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

    except Exception as exc:
        error_msg = f"{exc}"
        logger.exception("Run %s failed: %s", run_id, error_msg)
        await _abort_private_files()
        await run_manager.set_status(run_id, RunStatus.error, error=error_msg)
        await bridge.publish(
            run_id,
            "error",
            {
                "message": error_msg,
                "name": type(exc).__name__,
            },
        )

    finally:
        try:
            # Persist any subagent step events still buffered (#3779) — including on
            # abort/exception paths, where the stream loop broke before its own flush.
            if subagent_events is not None:
                await subagent_events.flush()

            if event_store is not None and ctx.file_authority is not None:
                changes = getattr(private_finalization_result, "workspace_changes", None)
                result = trusted_workspace_change_result(changes)
                if result is not None:
                    payload = result.to_dict()
                    summary = result.summary
                    changed_file_count = summary.created + summary.modified + summary.deleted
                    try:
                        await event_store.put(
                            thread_id=thread_id,
                            run_id=run_id,
                            event_type=WORKSPACE_CHANGES_EVENT_TYPE,
                            category="workspace",
                            content=f"{changed_file_count} file{'s' if changed_file_count != 1 else ''} changed +0 -0",
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
            if ctx.private_agent_runtime is not None:
                cleanup_succeeded = (
                    await _join_private_cleanup(
                        ctx.private_agent_runtime.aclose,
                        failure_message=f"Private runtime cleanup failed for run {run_id}",
                    )
                    and cleanup_succeeded
                )

            if ctx.file_authority is not None:
                release = getattr(ctx.file_authority, "release", None)
                if not callable(release):
                    release = getattr(ctx.file_authority, "close", None)
                if callable(release):
                    cleanup_succeeded = (
                        await _join_private_cleanup(
                            release,
                            failure_message=f"Private file authority cleanup failed for run {run_id}",
                        )
                        and cleanup_succeeded
                    )
                else:
                    cleanup_succeeded = False
                    logger.warning(
                        "Private file authority cleanup failed for run %s: release unavailable",
                        run_id,
                    )

            if not cleanup_succeeded and record.status is RunStatus.success:
                await _join_private_cleanup(
                    lambda: run_manager.set_status(
                        run_id,
                        RunStatus.error,
                        error="Private run cleanup failed",
                    ),
                    failure_message=f"Failed to record private cleanup error for run {run_id}",
                )

            if record.finalizing and cleanup_succeeded:
                if ctx.file_authority is not None:
                    await _join_private_cleanup(
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
                ckpt_tuple = await checkpointer.aget_tuple(ckpt_config)
                if ckpt_tuple is not None:
                    ckpt = getattr(ckpt_tuple, "checkpoint", {}) or {}
                    title = ckpt.get("channel_values", {}).get("title")
                    if title:
                        await thread_store.update_display_name(thread_id, title)
            except Exception:
                logger.debug(
                    "Failed to sync title for thread %s (non-fatal)",
                    thread_id,
                )

        if thread_store is not None:
            try:
                final_status = "idle" if record.status == RunStatus.success else record.status.value
                await thread_store.update_status(thread_id, final_status)
            except Exception:
                logger.debug("Failed to update thread_meta status for %s (non-fatal)", thread_id)

        if ctx.on_run_completed is not None:
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

        await bridge.publish_end(run_id)
        asyncio.create_task(bridge.cleanup(run_id, delay=60))
        if private_cleanup_cancellation_pending:
            raise asyncio.CancelledError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _checkpoint_id(checkpoint_tuple: Any) -> str | None:
    config = getattr(checkpoint_tuple, "config", {}) or {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    checkpoint_id = configurable.get("checkpoint_id") if isinstance(configurable, dict) else None
    if isinstance(checkpoint_id, str):
        return checkpoint_id
    checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {}
    if isinstance(checkpoint, dict) and isinstance(checkpoint.get("id"), str):
        return checkpoint["id"]
    return None


def _goal_instance_matches(left: GoalState | None, right: GoalState | None) -> bool:
    if not left or not right:
        return False
    same_status = left.get("status") == right.get("status") == "active"
    same_objective = left.get("objective") == right.get("objective")
    same_created_at = left.get("created_at") == right.get("created_at")
    return same_status and same_objective and same_created_at


def _read_checkpoint_messages(checkpoint_tuple: Any) -> list[Any]:
    checkpoint = getattr(checkpoint_tuple, "checkpoint", {}) or {}
    channel_values = checkpoint.get("channel_values", {}) if isinstance(checkpoint, dict) else {}
    messages = channel_values.get("messages", []) if isinstance(channel_values, dict) else []
    return messages if isinstance(messages, list) else []


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
    thread_id: str,
    run_id: str,
    goal: GoalState,
    evaluation: GoalEvaluation,
    no_progress_count: int,
    continuation_count: int | None = None,
    stand_down_reason: str | None = None,
    evidence_signature: str = "",
) -> GoalState | None:
    try:
        async with goal_thread_lock(thread_id):
            checkpoint_tuple = await _call_checkpointer_method(
                checkpointer,
                "aget_tuple",
                "get_tuple",
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
            )
            if checkpoint_tuple is None:
                return None
            current_goal = _read_checkpoint_goal(checkpoint_tuple)
            if current_goal is None or not _goal_instance_matches(goal, current_goal):
                return None
            expected_checkpoint_id = _checkpoint_id(checkpoint_tuple)
            updated_goal = attach_goal_evaluation(
                current_goal,
                evaluation,
                run_id=run_id,
                continuation_count=continuation_count,
                no_progress_count=no_progress_count,
                stand_down_reason=stand_down_reason,
                evidence_signature=evidence_signature,
            )
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


async def _reread_goal_and_checkpoint(checkpointer: Any, thread_id: str) -> tuple[GoalState | None, Any]:
    """Re-read the goal and latest checkpoint together for a concurrency re-check."""
    goal = await read_thread_goal(checkpointer, thread_id)
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
    thread_id: str,
    run_id: str,
    model_name: str | None,
    app_config: AppConfig | None,
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
        goal = await read_thread_goal(checkpointer, thread_id)
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
            thread_id=thread_id,
            run_id=run_id,
            goal=goal,
            evaluation=evaluation,
            no_progress_count=no_progress_count,
            continuation_count=continuation_count,
            stand_down_reason=stand_down_reason,
            evidence_signature=evidence_signature,
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
        messages = _read_checkpoint_messages(checkpoint_tuple)
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
        current_goal, current_checkpoint_tuple = await _reread_goal_and_checkpoint(checkpointer, thread_id)
    except Exception:
        logger.warning("Could not re-check goal state for thread %s after evaluation", thread_id, exc_info=True)
        return None

    if not _goal_instance_matches(goal, current_goal) or current_checkpoint_tuple is None:
        return None

    checkpoint_changed = _checkpoint_id(current_checkpoint_tuple) != checkpoint_id_before
    messages_changed = visible_conversation_signature(_read_checkpoint_messages(current_checkpoint_tuple)) != conversation_signature_before
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
                latest_goal = _read_checkpoint_goal(latest_checkpoint_tuple)
                if latest_goal is None or not _goal_instance_matches(goal, latest_goal):
                    return None
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
        latest_goal, latest_checkpoint_tuple = await _reread_goal_and_checkpoint(checkpointer, thread_id)
    except Exception:
        logger.warning("Could not verify queued goal continuation for thread %s", thread_id, exc_info=True)
        return None
    if not _goal_instance_matches(updated_goal, latest_goal) or latest_checkpoint_tuple is None:
        return None
    if visible_conversation_signature(_read_checkpoint_messages(latest_checkpoint_tuple)) != conversation_signature_before:
        await _persist(
            latest_goal,
            evaluation,
            no_progress_count,
            continuation_count=next_count,
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


async def _rollback_to_pre_run_checkpoint(
    *,
    checkpointer: Any,
    thread_id: str,
    run_id: str,
    pre_run_checkpoint_id: str | None,
    pre_run_snapshot: dict[str, Any] | None,
    snapshot_capture_failed: bool,
) -> None:
    """Restore thread state to the checkpoint snapshot captured before run start."""
    if checkpointer is None:
        logger.info("Run %s rollback requested but no checkpointer is configured", run_id)
        return

    if snapshot_capture_failed:
        logger.warning("Run %s rollback skipped: pre-run checkpoint snapshot capture failed", run_id)
        return

    if pre_run_snapshot is None:
        await _call_checkpointer_method(checkpointer, "adelete_thread", "delete_thread", thread_id)
        logger.info("Run %s rollback reset thread %s to empty state", run_id, thread_id)
        return

    checkpoint_to_restore = None
    metadata_to_restore: dict[str, Any] = {}
    checkpoint_ns = ""
    checkpoint = pre_run_snapshot.get("checkpoint")
    if not isinstance(checkpoint, dict):
        logger.warning("Run %s rollback skipped: invalid pre-run checkpoint snapshot", run_id)
        return
    checkpoint_to_restore = checkpoint
    if checkpoint_to_restore.get("id") is None and pre_run_checkpoint_id is not None:
        checkpoint_to_restore = {**checkpoint_to_restore, "id": pre_run_checkpoint_id}
    if checkpoint_to_restore.get("id") is None:
        logger.warning("Run %s rollback skipped: pre-run checkpoint has no checkpoint id", run_id)
        return
    restore_marker = _new_checkpoint_marker()
    checkpoint_to_restore = {
        **checkpoint_to_restore,
        "id": restore_marker["id"],
        "ts": restore_marker["ts"],
    }
    metadata = pre_run_snapshot.get("metadata", {})
    metadata_to_restore = metadata if isinstance(metadata, dict) else {}
    raw_checkpoint_ns = pre_run_snapshot.get("checkpoint_ns")
    checkpoint_ns = raw_checkpoint_ns if isinstance(raw_checkpoint_ns, str) else ""

    channel_versions = checkpoint_to_restore.get("channel_versions")
    new_versions = dict(channel_versions) if isinstance(channel_versions, dict) else {}

    restore_config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}}
    restored_config = await _call_checkpointer_method(
        checkpointer,
        "aput",
        "put",
        restore_config,
        checkpoint_to_restore,
        metadata_to_restore if isinstance(metadata_to_restore, dict) else {},
        new_versions,
    )
    if not isinstance(restored_config, dict):
        raise RuntimeError(f"Run {run_id} rollback restore returned invalid config: expected dict")
    restored_configurable = restored_config.get("configurable", {})
    if not isinstance(restored_configurable, dict):
        raise RuntimeError(f"Run {run_id} rollback restore returned invalid config payload")
    restored_checkpoint_id = restored_configurable.get("checkpoint_id")
    if not restored_checkpoint_id:
        raise RuntimeError(f"Run {run_id} rollback restore did not return checkpoint_id")

    pending_writes = pre_run_snapshot.get("pending_writes", [])
    if not pending_writes:
        return

    writes_by_task: dict[str, list[tuple[str, Any]]] = {}
    for item in pending_writes:
        if not isinstance(item, (tuple, list)) or len(item) != 3:
            raise RuntimeError(f"Run {run_id} rollback failed: pending_write is not a 3-tuple: {item!r}")
        task_id, channel, value = item
        if not isinstance(channel, str):
            raise RuntimeError(f"Run {run_id} rollback failed: pending_write has non-string channel: task_id={task_id!r}, channel={channel!r}")
        writes_by_task.setdefault(str(task_id), []).append((channel, value))

    for task_id, writes in writes_by_task.items():
        await _call_checkpointer_method(
            checkpointer,
            "aput_writes",
            "put_writes",
            restored_config,
            writes,
            task_id=task_id,
        )


def _new_checkpoint_marker() -> dict[str, str]:
    marker = empty_checkpoint()
    return {"id": marker["id"], "ts": marker["ts"]}


def _lg_mode_to_sse_event(mode: str) -> str:
    """Map LangGraph internal stream_mode name to SSE event name.

    LangGraph's ``astream(stream_mode="messages")`` produces message
    tuples.  The SSE protocol calls this ``messages-tuple`` when the
    client explicitly requests it, but the default SSE event name used
    by LangGraph Platform is simply ``"messages"``.
    """
    # All LG modes map 1:1 to SSE event names — "messages" stays "messages"
    return mode


def _namespaced_sse_event(mode: str, namespace: tuple[str, ...]) -> str:
    """Encode a LangGraph namespace in the SSE event name.

    The LangGraph SDK treats ``<mode>|<namespace segment>|...`` as the
    subgraph form of a stream event. Keeping the payload unchanged while
    suffixing the event name prevents child ``values`` and ``messages`` chunks
    from being projected into the lead-agent conversation.
    """
    event = _lg_mode_to_sse_event(mode)
    if not namespace:
        return event
    return "|".join((event, *namespace))


def _error_fallback_message_from_metadata(metadata: dict[str, Any], content: Any) -> str:
    detail = metadata.get("error_detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    reason = metadata.get("error_reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    if isinstance(content, str) and content.strip():
        return content.strip()[:2000]
    return "LLM provider failed after retries"


def _message_id(obj: Any) -> str | None:
    """Best-effort extraction of a stable message id from a message-like object."""
    msg_id = getattr(obj, "id", None)
    if isinstance(msg_id, str) and msg_id:
        return msg_id
    if isinstance(obj, dict):
        raw = obj.get("id")
        if isinstance(raw, str) and raw:
            return raw
    return None


def _try_extract_from_message(obj: Any, pre_existing_ids: set[str] | None = None) -> str | None:
    """Try to extract fallback marker from a single message object or dict.

    Messages whose id appears in ``pre_existing_ids`` are skipped — those are
    history checkpointed by a *prior* run on this thread and any fallback
    marker on them was already accounted for when that earlier run finished.
    Without this filter, a single past run that ended with a fallback marker
    would mark every subsequent run on the same thread as ``error``, because
    LangGraph replays the full message history through ``stream_mode="values"``.
    """
    if pre_existing_ids:
        msg_id = _message_id(obj)
        if msg_id is not None and msg_id in pre_existing_ids:
            return None

    additional_kwargs = getattr(obj, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict) and additional_kwargs.get("deerflow_error_fallback"):
        return _error_fallback_message_from_metadata(additional_kwargs, getattr(obj, "content", None))

    if isinstance(obj, dict):
        nested_kwargs = obj.get("additional_kwargs")
        if isinstance(nested_kwargs, dict) and nested_kwargs.get("deerflow_error_fallback"):
            return _error_fallback_message_from_metadata(nested_kwargs, obj.get("content"))
    return None


def _extract_llm_error_fallback_message(value: Any, pre_existing_ids: set[str] | None = None) -> str | None:
    """Find LLM fallback markers in streamed LangGraph chunks.

    Error fallback messages returned by model-call middleware are not guaranteed
    to pass through LLM end callbacks, but they do appear in graph state chunks.

    Messages whose id appears in ``pre_existing_ids`` are ignored — they are
    history from prior runs on the same thread (LangGraph replays the full
    messages channel in ``stream_mode="values"`` chunks), and any error
    fallback in that history was already resolved when its run finished.
    """
    # Fast path: large state chunks produced by stream_mode="values" have a
    # top-level "messages" list. Scanning only that list avoids expensive deep
    # recursion into large state dicts.
    if isinstance(value, dict):
        messages = value.get("messages")
        if isinstance(messages, (list, tuple)):
            for msg in messages:
                result = _try_extract_from_message(msg, pre_existing_ids)
                if result is not None:
                    return result
            # Fallback marker is attached to an AI message in the messages
            # channel; it will never appear elsewhere in a values chunk.
            return None
        # No top-level "messages" — this is likely an "updates" chunk (small
        # dict keyed by node name). Fall through to deep walk, which is cheap
        # for these payloads.

    # Deep walk for updates / messages / tuple / list modes. Payloads are
    # small, so full recursion is acceptable here.
    seen: set[int] = set()

    def walk(obj: Any) -> str | None:
        oid = id(obj)
        if oid in seen:
            return None
        seen.add(oid)

        result = _try_extract_from_message(obj, pre_existing_ids)
        if result is not None:
            return result

        if isinstance(obj, dict):
            for item in obj.values():
                result = walk(item)
                if result is not None:
                    return result
            return None

        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                result = walk(item)
                if result is not None:
                    return result
        return None

    return walk(value)


def _collect_pre_existing_message_ids(snapshot: dict[str, Any] | None) -> set[str]:
    """Pull stable message ids out of a pre-run checkpoint snapshot.

    Used by :func:`run_agent` to mask stale ``deerflow_error_fallback`` markers
    on history messages so they don't trip the current run's failure path. A
    missing or malformed snapshot yields an empty set (best-effort — we
    intentionally never raise from this helper).
    """
    if not isinstance(snapshot, dict):
        return set()
    checkpoint = snapshot.get("checkpoint")
    if not isinstance(checkpoint, dict):
        return set()
    channel_values = checkpoint.get("channel_values")
    if not isinstance(channel_values, dict):
        return set()
    messages = channel_values.get("messages")
    if not isinstance(messages, (list, tuple)):
        return set()
    ids: set[str] = set()
    for msg in messages:
        msg_id = _message_id(msg)
        if msg_id is not None:
            ids.add(msg_id)
    return ids


def _collect_private_pre_existing_message_ids(
    snapshot: dict[str, Any],
) -> set[str]:
    """Validate an exact private-Run checkpoint message boundary.

    A present checkpoint with no messages is a valid first-run boundary.
    Historical messages must all carry distinct stable IDs; otherwise a
    resumed Run cannot distinguish old task dispatches from new results.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("invalid snapshot")
    checkpoint = snapshot.get("checkpoint")
    if not isinstance(checkpoint, dict):
        raise ValueError("invalid checkpoint")
    channel_values = checkpoint.get("channel_values", {})
    if not isinstance(channel_values, dict):
        raise ValueError("invalid checkpoint channels")
    messages = channel_values.get("messages", [])
    if not isinstance(messages, (list, tuple)):
        raise ValueError("invalid checkpoint messages")

    ids: set[str] = set()
    for message in messages:
        message_id = _message_id(message)
        if message_id is None or message_id in ids:
            raise ValueError("unstable checkpoint message identity")
        ids.add(message_id)
    return ids


def _unpack_stream_item(
    item: Any,
    lg_modes: list[str],
    stream_subgraphs: bool,
) -> tuple[tuple[str, ...], str | None, Any]:
    """Unpack a multi-mode or subgraph item into (namespace, mode, chunk).

    Returns ``((), None, None)`` if the item cannot be parsed.
    """
    if stream_subgraphs:
        if isinstance(item, tuple) and len(item) == 3:
            namespace, mode, chunk = item
            return _normalize_stream_namespace(namespace), str(mode), chunk
        if isinstance(item, tuple) and len(item) == 2:
            first, chunk = item
            if isinstance(first, (list, tuple)):
                mode = lg_modes[0] if len(lg_modes) == 1 else None
                return _normalize_stream_namespace(first), mode, chunk
            return (), str(first), chunk
        return (), None, None

    if isinstance(item, tuple) and len(item) == 2:
        mode, chunk = item
        return (), str(mode), chunk

    # Fallback: single-element output from first mode
    return (), lg_modes[0] if lg_modes else None, item


def _normalize_stream_namespace(namespace: Any) -> tuple[str, ...]:
    """Return the generated LangGraph namespace as protocol segments."""
    if isinstance(namespace, str):
        return tuple(segment for segment in namespace.split("|") if segment)
    if isinstance(namespace, (list, tuple)):
        return tuple(str(segment) for segment in namespace if str(segment))
    return ()
