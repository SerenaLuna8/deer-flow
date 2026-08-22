"""Task tool for delegating work to subagents."""

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated, Any, cast

from langchain.tools import InjectedToolCallId, tool
from langchain_core.callbacks import BaseCallbackManager
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.config import get_stream_writer
from langgraph.graph import END
from langgraph.types import Command
from pydantic import BaseModel

from deerflow.config import get_app_config
from deerflow.error_codes import SUBAGENT_EXECUTION_FAILED_ERROR_CODE
from deerflow.file_authority import (
    AuthorityManifest,
    AuthorityManifestEntry,
    RunFileAuthority,
)
from deerflow.guardrails.provider import (
    GUARDRAIL_ATTRIBUTION_CONTEXT_KEY,
    copy_guardrail_attribution,
)
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.host_execution_approval import (
    HostExecutionApprovalPort,
    HostExecutionApprovalResult,
    HostExecutionOutcome,
    HostExecutionPlan,
)
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.sandbox.security import (
    LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE,
    is_host_bash_available,
    requires_host_bash_approval,
)
from deerflow.subagents import SubagentExecutor, get_available_subagent_names, get_subagent_config
from deerflow.subagents.change_signal import wait_for_change
from deerflow.subagents.config import SubagentConfig, resolve_subagent_model_name
from deerflow.subagents.executor import (
    SubagentStatus,
    cleanup_background_task,
    get_background_task_result,
    request_cancel_background_task,
)
from deerflow.subagents.runtime_catalog import (
    RUNTIME_AGENT_CATALOG_CONTEXT_KEY,
    trusted_runtime_agent_catalog,
)
from deerflow.subagents.status_contract import (
    SubagentStatusValue,
    SubagentStopReasonValue,
    format_subagent_result_message,
    make_subagent_additional_kwargs,
)
from deerflow.tools.types import Runtime
from deerflow.trace_context import ACT_WEAVE_TRACE_METADATA_KEY, get_current_trace_id, normalize_trace_id

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig
    from deerflow.subagents.runtime_catalog import RuntimeAgentProfile

logger = logging.getLogger(__name__)

# Cache subagent token usage by tool_call_id so TokenUsageMiddleware can
# write it back to the triggering AIMessage's usage_metadata.
_subagent_usage_cache: dict[str, dict[str, int]] = {}


async def _invoke_on_owner_loop(
    owner_loop: asyncio.AbstractEventLoop,
    target,
    *args,
    **kwargs,
):
    """Run a loop-bound trusted callback on the parent Worker's event loop."""

    async def invoke():
        result = target(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    if asyncio.get_running_loop() is owner_loop:
        return await invoke()
    future = asyncio.run_coroutine_threadsafe(invoke(), owner_loop)
    return await asyncio.wrap_future(future)


class _OwnerLoopAuthorityProxy:
    """Marshal opaque authorization-boundary methods back to their owner loop."""

    def __init__(self, target: object, owner_loop: asyncio.AbstractEventLoop):
        self._target = target
        self._owner_loop = owner_loop

    def __getattr__(self, name: str):
        target = getattr(self._target, name)
        if not callable(target):
            return target

        async def invoke(*args, **kwargs):
            return await _invoke_on_owner_loop(
                self._owner_loop,
                target,
                *args,
                **kwargs,
            )

        return invoke


class _OwnerLoopFileAuthorityProxy:
    """Keep async private Run file operations on their owning Worker loop."""

    def __init__(
        self,
        target: RunFileAuthority,
        owner_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._target = target
        self._owner_loop = owner_loop

    @property
    def sandbox_id(self) -> str | None:
        return self._target.sandbox_id

    async def restore(self) -> AuthorityManifest:
        return await _invoke_on_owner_loop(
            self._owner_loop,
            self._target.restore,
        )

    def thread_data_paths(self) -> dict[str, str]:
        return self._target.thread_data_paths()

    def visible_uploads(self) -> tuple[dict[str, object], ...]:
        return self._target.visible_uploads()

    def record_current_upload_ids(self, file_ids: tuple[str, ...]) -> None:
        self._target.record_current_upload_ids(file_ids)

    def current_upload_ids(self) -> tuple[str, ...]:
        return self._target.current_upload_ids()

    def current_uploads(self) -> tuple[AuthorityManifestEntry, ...]:
        return self._target.current_uploads()

    def authorizes_run_read_only_mount_path(
        self,
        *,
        run_id: str,
        path: str,
    ) -> bool:
        return self._target.authorizes_run_read_only_mount_path(
            run_id=run_id,
            path=path,
        )

    async def write_output(
        self,
        relative_path: str,
        content: bytes,
    ) -> str:
        return await _invoke_on_owner_loop(
            self._owner_loop,
            self._target.write_output,
            relative_path,
            content,
        )

    async def write_internal(
        self,
        relative_path: str,
        content: bytes,
    ) -> str:
        return await _invoke_on_owner_loop(
            self._owner_loop,
            self._target.write_internal,
            relative_path,
            content,
        )

    async def record_presented_paths(
        self,
        presented_paths: tuple[str, ...],
        *,
        tool_call_id: str,
    ) -> None:
        await _invoke_on_owner_loop(
            self._owner_loop,
            self._target.record_presented_paths,
            presented_paths,
            tool_call_id=tool_call_id,
        )

    async def output_delivery_status(self) -> str:
        return await _invoke_on_owner_loop(
            self._owner_loop,
            self._target.output_delivery_status,
        )

    async def finalize(self) -> object:
        return await _invoke_on_owner_loop(
            self._owner_loop,
            self._target.finalize,
        )

    async def mark_failed(self) -> None:
        await _invoke_on_owner_loop(
            self._owner_loop,
            self._target.mark_failed,
        )

    async def release(self) -> None:
        await _invoke_on_owner_loop(
            self._owner_loop,
            self._target.release,
        )


class _OwnerLoopHostExecutionApprovalProxy:
    """Preserve the typed approval port across the subagent thread boundary."""

    def __init__(
        self,
        target: HostExecutionApprovalPort,
        owner_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._target = target
        self._owner_loop = owner_loop

    async def request_host_execution(
        self,
        plan: HostExecutionPlan,
    ) -> HostExecutionApprovalResult:
        return await _invoke_on_owner_loop(
            self._owner_loop,
            self._target.request_host_execution,
            plan,
        )

    async def complete_host_execution(
        self,
        approval_id: str,
        outcome: HostExecutionOutcome,
    ) -> None:
        await _invoke_on_owner_loop(
            self._owner_loop,
            self._target.complete_host_execution,
            approval_id,
            outcome,
        )


class _OwnerLoopCheckerProxy:
    """Marshal a trusted callable authorization fallback to its owner loop."""

    def __init__(self, target, owner_loop: asyncio.AbstractEventLoop):
        self._target = target
        self._owner_loop = owner_loop

    async def __call__(self):
        return await _invoke_on_owner_loop(self._owner_loop, self._target)


class _OwnerLoopSkillSecretProviderProxy:
    """Marshal private Skill secret refresh back to the owner Worker loop."""

    def __init__(self, target, owner_loop: asyncio.AbstractEventLoop):
        self._target = target
        self._owner_loop = owner_loop

    async def __call__(self, *args, **kwargs):
        return await _invoke_on_owner_loop(
            self._owner_loop,
            self._target,
            *args,
            **kwargs,
        )


def _trusted_private_mcp_tools(
    parent_context: dict[str, Any],
) -> tuple[BaseTool, ...]:
    """Return only opaque Worker-installed private MCP proxy objects."""

    raw_tools = parent_context.get("__runtime_mcp_tools")
    if not isinstance(raw_tools, tuple):
        return ()
    from deerflow.tools.mcp_metadata import is_private_mcp_tool

    trusted: list[BaseTool] = []
    for candidate in raw_tools:
        if not isinstance(candidate, BaseTool) or not is_private_mcp_tool(candidate):
            return ()
        trusted.append(candidate)
    return tuple(trusted)


def _wrap_private_mcp_tool_for_owner_loop(
    admitted_tool: BaseTool,
    owner_loop: asyncio.AbstractEventLoop,
) -> StructuredTool:
    """Keep delegated MCP calls on the parent loop that owns the exact runtime."""

    args_schema = admitted_tool.args_schema
    if not isinstance(args_schema, type) or not issubclass(args_schema, BaseModel):
        raise RuntimeError("Private MCP tool schema is unavailable")

    async def invoke(**arguments):
        return await _invoke_on_owner_loop(
            owner_loop,
            admitted_tool.ainvoke,
            dict(arguments),
        )

    return StructuredTool.from_function(
        coroutine=invoke,
        name=admitted_tool.name,
        description=admitted_tool.description,
        args_schema=args_schema,
        return_direct=admitted_tool.return_direct,
        response_format=admitted_tool.response_format,
        metadata=dict(admitted_tool.metadata or {}),
    )


def _token_usage_cache_enabled(app_config: "AppConfig | None") -> bool:
    if app_config is None:
        try:
            app_config = get_app_config()
        except FileNotFoundError:
            return False
    return bool(getattr(getattr(app_config, "token_usage", None), "enabled", False))


def _cache_subagent_usage(tool_call_id: str, usage: dict | None, *, enabled: bool = True) -> None:
    if enabled and usage:
        _subagent_usage_cache[tool_call_id] = usage


def pop_cached_subagent_usage(tool_call_id: str) -> dict | None:
    return _subagent_usage_cache.pop(tool_call_id, None)


def _is_subagent_terminal(result: Any) -> bool:
    """Return whether a background subagent result is safe to clean up."""
    return result.status in {SubagentStatus.COMPLETED, SubagentStatus.FAILED, SubagentStatus.CANCELLED, SubagentStatus.TIMED_OUT} or getattr(result, "completed_at", None) is not None


def _trusted_agent_prompt_bundle(parent_context: dict[str, Any]) -> object | None:
    """Accept only the Worker-installed opaque immutable bundle shape.

    JSON request bodies cannot manufacture an object with these attributes, so
    client context dictionaries/strings fail closed without importing the lead
    prompt module here (which would reintroduce the task/subagent import cycle).
    """

    bundle = parent_context.get("__agent_prompt_bundle")
    required = (
        "payload_schema_version",
        "agents_instructions",
        "soul",
        "identity",
        "user_context",
    )
    if bundle is None or not all(hasattr(bundle, name) for name in required):
        return None
    return bundle


def _trusted_skill_scoped_secrets(
    parent_context: dict[str, Any],
) -> dict[str, dict[str, str]] | None:
    """Copy only the Worker-installed private Skill secret carrier.

    Private request preparation strips every double-underscore and secret-like
    client key. Requiring the opaque private scope here keeps a non-private
    caller from manufacturing this internal inheritance channel.
    """

    if "private_scope" not in parent_context:
        return None
    raw = parent_context.get("__skill_scoped_secrets")
    if not isinstance(raw, Mapping):
        return None
    copied: dict[str, dict[str, str]] = {}
    for path, values in raw.items():
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(
                values,
                Mapping,
            )
        ):
            return None
        env: dict[str, str] = {}
        for name, value in values.items():
            if not isinstance(name, str) or not isinstance(value, str):
                return None
            env[name] = value
        copied[path] = env
    return copied


# Heartbeat upper bound for event-driven subagent waiting (U8). Terminal and
# progress transitions wake waiters immediately through SubagentChangeSignal;
# the heartbeat only bounds staleness when a notification was debounced away
# or the writer died without a terminal transition.
_SUBAGENT_WAIT_HEARTBEAT_SECONDS = 5.0


def _subscribe_subagent_changes(result: Any):
    """Return ``(signal, event)`` for a result carrying a change signal.

    Duck-typed so tests (and any legacy result object without the signal)
    degrade to pure heartbeat polling instead of failing.
    """

    signal = getattr(result, "changes", None)
    if signal is None or not callable(getattr(signal, "subscribe", None)):
        return None, None
    return signal, signal.subscribe()


def _execution_wait_deadline(
    *,
    status: Any,
    now: float,
    wait_budget_seconds: float,
    current_deadline: float | None,
) -> float | None:
    """Start the tool-side execution safety budget exactly once at RUNNING."""

    if current_deadline is None and status == SubagentStatus.RUNNING:
        return now + wait_budget_seconds
    return current_deadline


async def _await_subagent_terminal(task_id: str, wait_budget_seconds: float) -> Any | None:
    """Wait until the background subagent reaches a terminal status or the deadline passes."""
    deadline = time.monotonic() + wait_budget_seconds
    signal = None
    change_event = None
    try:
        while True:
            if change_event is not None:
                change_event.clear()
            result = get_background_task_result(task_id)
            if result is None:
                return None
            if _is_subagent_terminal(result):
                return result
            if change_event is None:
                signal, change_event = _subscribe_subagent_changes(result)
                if change_event is not None:
                    # Re-read state: the terminal latch covers transitions
                    # that raced between the read above and the subscribe.
                    continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            slice_seconds = min(_SUBAGENT_WAIT_HEARTBEAT_SECONDS, remaining)
            if change_event is not None:
                await wait_for_change(change_event, heartbeat_seconds=slice_seconds)
            else:
                await asyncio.sleep(slice_seconds)
    finally:
        if signal is not None and change_event is not None:
            signal.unsubscribe(change_event)


async def _deferred_cleanup_subagent_task(task_id: str, trace_id: str, wait_budget_seconds: float) -> None:
    """Keep watching a cancelled subagent until it can be safely removed."""
    terminal = await _await_subagent_terminal(task_id, wait_budget_seconds)
    if terminal is not None:
        cleanup_background_task(task_id)
        return
    if get_background_task_result(task_id) is not None:
        logger.warning(f"[trace={trace_id}] Deferred cleanup for task {task_id} timed out after {wait_budget_seconds:.0f}s")


def _log_cleanup_failure(cleanup_task: asyncio.Task[None], *, trace_id: str, task_id: str) -> None:
    if cleanup_task.cancelled():
        return

    if cleanup_task.exception() is not None:
        logger.error(
            "[trace=%s] Deferred cleanup failed for task %s: error_code=%s",
            trace_id,
            task_id,
            SUBAGENT_EXECUTION_FAILED_ERROR_CODE,
        )


def _schedule_deferred_subagent_cleanup(task_id: str, trace_id: str, wait_budget_seconds: float) -> None:
    logger.debug(f"[trace={trace_id}] Scheduling deferred cleanup for cancelled task {task_id}")
    cleanup_task = asyncio.create_task(_deferred_cleanup_subagent_task(task_id, trace_id, wait_budget_seconds))
    cleanup_task.add_done_callback(lambda task: _log_cleanup_failure(task, trace_id=trace_id, task_id=task_id))


def _find_usage_recorder(runtime: Any) -> Any | None:
    """Find a callback handler with ``record_external_llm_usage_records`` in the runtime config.

    LangChain may pass ``config["callbacks"]`` in three different shapes:

    - ``None`` (no callbacks registered): no recorder.
    - A plain ``list[BaseCallbackHandler]``: iterate it directly.
    - A ``BaseCallbackManager`` instance (e.g. ``AsyncCallbackManager`` on async
      tool runs): managers are not iterable, so we unwrap ``.handlers`` first.

    Any other shape (e.g. a single handler object accidentally passed without a
    list wrapper) cannot be iterated safely; treat it as "no recorder" rather
    than raise.
    """
    if runtime is None:
        return None
    config = getattr(runtime, "config", None)
    if not isinstance(config, dict):
        return None
    callbacks = config.get("callbacks")
    if isinstance(callbacks, BaseCallbackManager):
        callbacks = callbacks.handlers
    if not callbacks:
        return None
    if not isinstance(callbacks, list):
        return None
    for cb in callbacks:
        if hasattr(cb, "record_external_llm_usage_records"):
            return cb
    return None


def _summarize_usage(records: list[dict] | None) -> dict | None:
    """Summarize token usage records into a compact dict for SSE events."""
    if not records:
        return None
    return {
        "input_tokens": sum(r.get("input_tokens", 0) or 0 for r in records),
        "output_tokens": sum(r.get("output_tokens", 0) or 0 for r in records),
        "total_tokens": sum(r.get("total_tokens", 0) or 0 for r in records),
    }


def _report_subagent_usage(runtime: Any, result: Any) -> None:
    """Report subagent token usage to the parent RunJournal, if available.

    Each subagent task must be reported only once (guarded by usage_reported).
    """
    if getattr(result, "usage_reported", True):
        return
    records = getattr(result, "token_usage_records", None) or []
    if not records:
        return
    journal = _find_usage_recorder(runtime)
    if journal is None:
        logger.debug("No usage recorder found in runtime callbacks — subagent token usage not recorded")
        return
    try:
        journal.record_external_llm_usage_records(records)
        result.usage_reported = True
    except Exception:
        logger.warning(
            "Failed to report subagent token usage: error_code=%s",
            SUBAGENT_EXECUTION_FAILED_ERROR_CODE,
        )


def _get_runtime_app_config(runtime: Any) -> "AppConfig | None":
    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        app_config = context.get("app_config")
        if app_config is not None:
            return cast("AppConfig", app_config)
    return None


def _merge_skill_allowlists(parent: list[str] | None, child: list[str] | None) -> list[str] | None:
    """Return the effective subagent skill allowlist under the parent policy."""
    if parent is None:
        return child
    if child is None:
        return list(parent)

    parent_set = set(parent)
    return [skill for skill in child if skill in parent_set]


def _task_result_command(
    *,
    tool_call_id: str,
    status: SubagentStatusValue,
    result: str | None = None,
    error: str | None = None,
    stop_reason: SubagentStopReasonValue | None = None,
    model_name: str | None = None,
    usage: dict[str, int] | None = None,
) -> Command:
    content, metadata_error = format_subagent_result_message(status, result=result, error=error, stop_reason=stop_reason)
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=content,
                    tool_call_id=tool_call_id,
                    name="task",
                    additional_kwargs=make_subagent_additional_kwargs(
                        status,
                        result=result,
                        error=metadata_error,
                        stop_reason=stop_reason,
                        model_name=model_name,
                        token_usage=usage,
                    ),
                )
            ]
        }
    )


def _host_execution_approval_command(
    *,
    tool_call_id: str,
    artifact: Mapping[str, object],
) -> Command:
    """Bubble a delegated approval anchor into the parent Agent checkpoint."""

    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=("Delegated host command execution requires approval."),
                    tool_call_id=tool_call_id,
                    name="task",
                    artifact={
                        "host_execution_approval": dict(artifact),
                    },
                ),
            ],
        },
        goto=END,
    )


async def _assemble_subagent_tools(
    *,
    parent_context: dict[str, Any],
    runtime_agent_profile: "RuntimeAgentProfile | None",
    effective_model: str,
    effective_tool_groups: list[str] | tuple[str, ...] | None,
    app_config: "AppConfig | None",
) -> list[BaseTool]:
    """Assemble the exact tool set used by a delegated Agent.

    This is deliberately independent of the lead Agent's runtime context.
    In particular, a parent ``__memory_authority`` never turns into the
    lead-only ``recall_memory`` or ``remember`` tools here.
    """

    # Lazy import avoids the tools/__init__ -> task_tool import cycle.
    from deerflow.tools import get_available_tools

    private_run = "private_scope" in parent_context
    available_tools_kwargs: dict[str, Any] = {
        "model_name": effective_model,
        "groups": effective_tool_groups,
        "subagent_enabled": False,
    }
    if private_run:
        # Private Runs may use only the exact admitted proxies installed by the
        # Worker below. Never fall back to process-global MCP or ACP discovery.
        available_tools_kwargs["include_mcp"] = False
        available_tools_kwargs["include_acp"] = False
    if app_config is not None:
        available_tools_kwargs["app_config"] = app_config

    from deerflow.assets.catalog import trusted_asset_context

    raw_asset_context = parent_context.get("project_context") or parent_context.get("asset_context")
    asset_context = trusted_asset_context(raw_asset_context)
    if asset_context is not None:
        available_tools_kwargs["asset_context"] = asset_context

    tools = await asyncio.to_thread(get_available_tools, **available_tools_kwargs)
    if parent_context.get(RuntimeContextKeys.NON_INTERACTIVE) is True and app_config is not None and requires_host_bash_approval(app_config):
        tools = [tool for tool in tools if tool.name != "bash"]
    if private_run:
        owner_loop = asyncio.get_running_loop()
        admitted_mcp_tools = runtime_agent_profile.mcp_tools if runtime_agent_profile is not None else _trusted_private_mcp_tools(parent_context)
        existing_names = {tool.name for tool in tools}
        for admitted_tool in admitted_mcp_tools:
            if admitted_tool.name in existing_names:
                raise RuntimeError("Private MCP tool name conflicts with another tool")
            tools.append(
                _wrap_private_mcp_tool_for_owner_loop(
                    admitted_tool,
                    owner_loop,
                )
            )
            existing_names.add(admitted_tool.name)
    return tools


@tool("task", parse_docstring=True)
async def task_tool(
    runtime: Runtime,
    description: str,
    prompt: str,
    subagent_type: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str | Command:
    """Delegate a task to a specialized subagent that runs in its own context.

    Subagents help you:
    - Preserve context by keeping exploration and implementation separate
    - Handle complex multi-step tasks autonomously
    - Execute commands or operations in isolated contexts

    Built-in subagent types:
    - **general-purpose**: A capable agent for complex, multi-step tasks that require
      both exploration and action. Use when the task requires complex reasoning,
      multiple dependent steps, or would benefit from isolated context.
    - **bash**: Command execution specialist for running bash commands. This is only
      available when host bash is explicitly allowed or when using an isolated shell
      sandbox such as `AioSandboxProvider`.

    Additional custom subagent types may be defined in config.yaml under
    `subagents.custom_agents`. Each custom type can have its own system prompt,
    tools, skills, model, and timeout configuration. If an unknown subagent_type
    is provided, the error message will list all available types.

    When to use this tool:
    - Complex tasks requiring multiple steps or tools
    - Tasks that produce verbose output
    - When you want to isolate context from the main conversation
    - Parallel research or exploration tasks

    When NOT to use this tool:
    - Simple, single-step operations (use tools directly)
    - Tasks requiring user interaction or clarification

    Args:
        description: A short (3-5 word) description of the task for logging/display. ALWAYS PROVIDE THIS PARAMETER FIRST.
        prompt: The task description for the subagent. Be specific and clear about what needs to be done. ALWAYS PROVIDE THIS PARAMETER SECOND.
        subagent_type: The type of subagent to use. ALWAYS PROVIDE THIS PARAMETER THIRD.
    """
    runtime_app_config = _get_runtime_app_config(runtime)
    cache_token_usage = _token_usage_cache_enabled(runtime_app_config)
    parent_context = runtime.context if runtime is not None else None
    parent_context = parent_context if isinstance(parent_context, dict) else {}
    private_run = "private_scope" in parent_context
    runtime_agent_catalog = trusted_runtime_agent_catalog(parent_context.get(RUNTIME_AGENT_CATALOG_CONTEXT_KEY)) if private_run else None
    runtime_agent_profile = runtime_agent_catalog.get(subagent_type) if runtime_agent_catalog is not None else None
    static_subagent_names = get_available_subagent_names(app_config=runtime_app_config) if runtime_app_config is not None else get_available_subagent_names()
    available_subagent_names = list(
        dict.fromkeys(
            [
                *static_subagent_names,
                *(runtime_agent_catalog.names if runtime_agent_catalog else ()),
            ]
        )
    )

    # Get subagent configuration
    if runtime_agent_profile is not None:
        config = SubagentConfig(
            name=runtime_agent_profile.key,
            description=runtime_agent_profile.description,
            system_prompt=None,
            tools=None,
            disallowed_tools=["task"],
            # The executor receives only this profile's exact runtime Skill
            # tuple below; ``None`` loads that tuple, while ``[]`` would
            # incorrectly suppress even the explicitly referenced Skills.
            skills=None,
            model=runtime_agent_profile.model_name,
        )
    else:
        config = get_subagent_config(subagent_type, app_config=runtime_app_config) if runtime_app_config is not None else get_subagent_config(subagent_type)
    if config is None:
        available = ", ".join(available_subagent_names)
        error = f"Unknown subagent type '{subagent_type}'. Available: {available}"
        return _task_result_command(
            tool_call_id=tool_call_id,
            status="failed",
            error=error,
        )
    if subagent_type == "bash":
        host_bash_allowed = is_host_bash_available(runtime_app_config) if runtime_app_config is not None else is_host_bash_available()
        approval_unavailable = parent_context.get(RuntimeContextKeys.NON_INTERACTIVE) is True and runtime_app_config is not None and requires_host_bash_approval(runtime_app_config)
        if not host_bash_allowed or approval_unavailable:
            return _task_result_command(
                tool_call_id=tool_call_id,
                status="failed",
                error=LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE,
            )

    # Build config overrides
    overrides: dict = {}

    # Skills are loaded by SubagentExecutor per-session (aligned with Codex's pattern:
    # each subagent loads its own skills based on config, injected as conversation items).
    # No longer appended to system_prompt here.

    # Extract parent context from runtime
    sandbox_state = None
    thread_data = None
    thread_id = None
    parent_model = None
    trace_id = None
    user_id = None
    deerflow_trace_id = None
    metadata: dict = {}

    if runtime is not None:
        sandbox_state = runtime.state.get("sandbox")
        thread_data = runtime.state.get("thread_data")
        thread_id = runtime.context.get("thread_id") if runtime.context else None
        if thread_id is None:
            thread_id = runtime.config.get("configurable", {}).get("thread_id")

        # Try to get parent model from configurable
        metadata = runtime.config.get("metadata", {})
        parent_model = metadata.get("model_name")

        # Get or generate trace_id for distributed tracing
        trace_id = metadata.get("trace_id") or str(uuid.uuid4())[:8]

    # Get user_id for tracing (uses standard resolution order)
    user_id = resolve_runtime_user_id(runtime)

    # Propagate the authenticated runtime context so delegated tool calls are
    # evaluated by GuardrailMiddleware with the same identity/attribution as
    # the lead agent. Sourced from the server-side context written by
    # inject_authenticated_user_context (and run_id by the run worker); stays
    # None when absent (e.g. internal-auth runs) so guardrail behavior is
    # unchanged. Without this, role-aware policy silently mis-attributes any
    # tool call delegated to a subagent (user_role=None).
    guardrail_attribution = copy_guardrail_attribution(parent_context.get(GUARDRAIL_ATTRIBUTION_CONTEXT_KEY)) if private_run else None
    user_role = parent_context.get("user_role")
    oauth_provider = parent_context.get("oauth_provider")
    oauth_id = parent_context.get("oauth_id")
    run_id = parent_context.get("run_id")
    if guardrail_attribution is not None:
        user_id = guardrail_attribution.get("user_id")
        user_role = guardrail_attribution.get("user_role")
        oauth_provider = guardrail_attribution.get("oauth_provider")
        oauth_id = guardrail_attribution.get("oauth_id")
        run_id = guardrail_attribution.get("run_id")
    # IM-channel sender identity: group chats share one thread across senders,
    # so delegated bash commands need the dispatching turn's channel_user_id.
    channel_user_id = parent_context.get("channel_user_id")
    channel_identity_present = "channel_user_id" in parent_context
    deerflow_trace_id = normalize_trace_id(parent_context.get(ACT_WEAVE_TRACE_METADATA_KEY)) or normalize_trace_id(metadata.get(ACT_WEAVE_TRACE_METADATA_KEY)) or get_current_trace_id()

    parent_available_skills = metadata.get("available_skills")
    if runtime_agent_profile is None and parent_available_skills is not None:
        overrides["skills"] = _merge_skill_allowlists(list(parent_available_skills), config.skills)

    if overrides:
        config = replace(config, **overrides)

    # Inherit parent agent's tool_groups so subagents respect the same restrictions
    parent_tool_groups = metadata.get("tool_groups")
    effective_tool_groups = runtime_agent_profile.tool_groups if runtime_agent_profile is not None else parent_tool_groups
    resolved_app_config = runtime_app_config
    if config.model == "inherit" and parent_model is None and resolved_app_config is None:
        resolved_app_config = get_app_config()
    effective_model = resolve_subagent_model_name(config, parent_model, app_config=resolved_app_config)
    tools = await _assemble_subagent_tools(
        parent_context=parent_context,
        runtime_agent_profile=runtime_agent_profile,
        effective_model=effective_model,
        effective_tool_groups=effective_tool_groups,
        app_config=resolved_app_config,
    )

    # Create executor
    executor_kwargs = {
        "config": config,
        "tools": tools,
        "parent_model": parent_model,
        "sandbox_state": sandbox_state,
        "thread_data": thread_data,
        "thread_id": thread_id,
        "trace_id": trace_id,
        "user_id": user_id,
        "user_role": user_role,
        "oauth_provider": oauth_provider,
        "oauth_id": oauth_id,
        "run_id": run_id,
        "channel_user_id": channel_user_id,
        "channel_identity_present": channel_identity_present,
        "deerflow_trace_id": deerflow_trace_id,
    }
    if guardrail_attribution is not None:
        executor_kwargs["guardrail_attribution"] = guardrail_attribution
    # Preserve the server-issued private Run boundary across delegation.
    # Passing only sandbox/thread_data state is insufficient: the subagent's
    # own ThreadDataMiddleware and SandboxMiddleware need the opaque authority
    # objects to keep the parent's projected workspace and must not release the
    # parent's private sandbox when delegated execution finishes.
    if "private_scope" in parent_context:
        executor_kwargs["private_scope"] = parent_context["private_scope"]
        owner_loop = asyncio.get_running_loop()
        file_authority = parent_context.get("__file_authority")
        executor_kwargs["file_authority"] = (
            _OwnerLoopFileAuthorityProxy(
                cast(RunFileAuthority, file_authority),
                owner_loop,
            )
            if file_authority is not None
            else None
        )
        authorization_boundary = parent_context.get("__authorization_boundary")
        if authorization_boundary is not None:
            executor_kwargs["authorization_boundary"] = _OwnerLoopAuthorityProxy(
                authorization_boundary,
                owner_loop,
            )
        authorization_checker = parent_context.get("__authorization_checker")
        if callable(authorization_checker):
            executor_kwargs["authorization_checker"] = _OwnerLoopCheckerProxy(
                authorization_checker,
                owner_loop,
            )
        skill_secret_provider = parent_context.get(
            "__skill_secret_provider",
        )
        if callable(skill_secret_provider):
            executor_kwargs["skill_secret_provider"] = _OwnerLoopSkillSecretProviderProxy(
                skill_secret_provider,
                owner_loop,
            )
    approval_port = parent_context.get(
        RuntimeContextKeys.HOST_EXECUTION_APPROVAL_PORT,
    )
    if isinstance(approval_port, HostExecutionApprovalPort):
        owner_loop = asyncio.get_running_loop()
        executor_kwargs["host_execution_approval_port"] = _OwnerLoopHostExecutionApprovalProxy(
            approval_port,
            owner_loop,
        )
        raw_parent_path = parent_context.get(
            RuntimeContextKeys.HOST_EXECUTION_AGENT_PATH,
        )
        parent_path = raw_parent_path if isinstance(raw_parent_path, tuple) and raw_parent_path and all(isinstance(part, str) and part for part in raw_parent_path) else ("lead",)
        executor_kwargs["host_execution_agent_path"] = (
            *parent_path,
            f"subagent:{config.name}",
        )
    run_read_only_mounts = parent_context.get("__run_read_only_mounts")
    if isinstance(run_read_only_mounts, tuple):
        executor_kwargs["run_read_only_mounts"] = run_read_only_mounts
    agent_prompt_bundle = runtime_agent_profile.prompt_bundle if runtime_agent_profile is not None else _trusted_agent_prompt_bundle(parent_context)
    if agent_prompt_bundle is not None:
        executor_kwargs["agent_prompt_bundle"] = agent_prompt_bundle
    runtime_skills = runtime_agent_profile.runtime_skills if runtime_agent_profile is not None else parent_context.get("__runtime_skills")
    if isinstance(runtime_skills, tuple):
        executor_kwargs["runtime_skills"] = runtime_skills
    if runtime_agent_profile is not None:
        executor_kwargs["agent_model_settings"] = runtime_agent_profile.model_settings
    if "skill_secret_provider" not in executor_kwargs:
        skill_scoped_secrets = _trusted_skill_scoped_secrets(parent_context)
        if skill_scoped_secrets is not None:
            executor_kwargs["skill_scoped_secrets"] = skill_scoped_secrets
    if resolved_app_config is not None:
        executor_kwargs["app_config"] = resolved_app_config
    executor = SubagentExecutor(**executor_kwargs)

    # Start background execution (always async to prevent blocking)
    # Use tool_call_id as task_id for better traceability
    task_id = executor.execute_async(prompt, task_id=tool_call_id)

    # Wait for task completion in backend (removes need for LLM to poll).
    # Event-driven (U8): the subagent wakes this waiter on progress/terminal
    # transitions; the heartbeat only bounds staleness as a safety net.
    last_status = None
    last_message_count = 0  # Track how many AI messages we've already sent
    # The scheduler leaves queued tasks PENDING and starts the configured
    # execution timeout only after capacity is acquired. Mirror that boundary
    # here: queueing and execution each receive an independent safety window,
    # so backpressure never consumes the subagent's execution wait budget.
    wait_budget_seconds = float(config.timeout_seconds + 60)
    wait_started = time.monotonic()
    queue_deadline = wait_started + wait_budget_seconds
    execution_deadline: float | None = None

    logger.info(f"[trace={trace_id}] Started background task {task_id} (subagent={subagent_type}, timeout={config.timeout_seconds}s, wait_budget={wait_budget_seconds:.0f}s)")

    writer = get_stream_writer()
    # Send Task Started message'
    writer(
        {
            "type": "task_started",
            "task_id": task_id,
            "description": description,
            "model_name": effective_model,
        }
    )

    change_signal = None
    change_event = None
    try:
        while True:
            if change_event is not None:
                # Clear before reading state so a notify that lands after the
                # read is never lost — it re-sets the event for the next wait.
                change_event.clear()
            result = get_background_task_result(task_id)

            if result is None:
                logger.error(f"[trace={trace_id}] Task {task_id} not found in background tasks")
                writer(
                    {
                        "type": "task_failed",
                        "task_id": task_id,
                        "error": "Task disappeared from background tasks",
                        "model_name": effective_model,
                        "usage": None,
                    }
                )
                cleanup_background_task(task_id)
                error = f"Task {task_id} disappeared from background tasks"
                return _task_result_command(
                    tool_call_id=tool_call_id,
                    status="failed",
                    error=error,
                    model_name=effective_model,
                )

            # Log status changes for debugging
            if result.status != last_status:
                logger.info(f"[trace={trace_id}] Task {task_id} status: {result.status.value}")
                last_status = result.status

            execution_deadline = _execution_wait_deadline(
                status=result.status,
                now=time.monotonic(),
                wait_budget_seconds=wait_budget_seconds,
                current_deadline=execution_deadline,
            )

            # Token records are cumulative. Reuse one snapshot for progress and
            # terminal events so consumers replace rather than add totals.
            usage = _summarize_usage(getattr(result, "token_usage_records", None))

            # Check for new AI messages and send task_running events
            ai_messages = result.ai_messages or []
            current_message_count = len(ai_messages)
            if current_message_count > last_message_count:
                # Send task_running event for each new message
                for i in range(last_message_count, current_message_count):
                    message = ai_messages[i]
                    writer(
                        {
                            "type": "task_running",
                            "task_id": task_id,
                            "message": message,
                            "message_index": i + 1,  # 1-based index for display
                            "total_messages": current_message_count,
                            "usage": usage,
                            "model_name": effective_model,
                        }
                    )
                    logger.info(f"[trace={trace_id}] Task {task_id} sent message #{i + 1}/{current_message_count}")
                last_message_count = current_message_count

            # Check if task completed, failed, or timed out
            if result.status == SubagentStatus.COMPLETED:
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                _report_subagent_usage(runtime, result)
                approval_artifact = result.host_execution_approval_artifact
                if approval_artifact is not None:
                    cleanup_background_task(task_id)
                    return _host_execution_approval_command(
                        tool_call_id=tool_call_id,
                        artifact=approval_artifact,
                    )
                writer(
                    {
                        "type": "task_completed",
                        "task_id": task_id,
                        "result": result.result,
                        "usage": usage,
                        "model_name": effective_model,
                    }
                )
                logger.info(f"[trace={trace_id}] Task {task_id} completed after {time.monotonic() - wait_started:.1f}s")
                cleanup_background_task(task_id)
                # stop_reason carries a guardrail cap (token_capped / turn_capped)
                # when the run was ended early but still produced a final answer
                # — the work survives on result_brief like a clean success.
                return _task_result_command(
                    tool_call_id=tool_call_id,
                    status="completed",
                    result=result.result,
                    stop_reason=result.stop_reason,
                    model_name=effective_model,
                    usage=usage,
                )
            elif result.status == SubagentStatus.FAILED:
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                _report_subagent_usage(runtime, result)
                writer(
                    {
                        "type": "task_failed",
                        "task_id": task_id,
                        "error": result.error,
                        "usage": usage,
                        "model_name": effective_model,
                    }
                )
                logger.error("[trace=%s] Task %s failed", trace_id, task_id)
                cleanup_background_task(task_id)
                # A turn-capped run with no usable output surfaces as failed +
                # stop_reason=turn_capped; the cap note lets the lead tell "out
                # of budget" from "broken subagent".
                return _task_result_command(
                    tool_call_id=tool_call_id,
                    status="failed",
                    error=result.error,
                    stop_reason=result.stop_reason,
                    model_name=effective_model,
                    usage=usage,
                )
            elif result.status == SubagentStatus.CANCELLED:
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                _report_subagent_usage(runtime, result)
                writer(
                    {
                        "type": "task_cancelled",
                        "task_id": task_id,
                        "error": result.error,
                        "usage": usage,
                        "model_name": effective_model,
                    }
                )
                logger.info("[trace=%s] Task %s cancelled", trace_id, task_id)
                cleanup_background_task(task_id)
                return _task_result_command(
                    tool_call_id=tool_call_id,
                    status="cancelled",
                    error=result.error,
                    model_name=effective_model,
                    usage=usage,
                )
            elif result.status == SubagentStatus.TIMED_OUT:
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                _report_subagent_usage(runtime, result)
                writer(
                    {
                        "type": "task_timed_out",
                        "task_id": task_id,
                        "error": result.error,
                        "usage": usage,
                        "model_name": effective_model,
                    }
                )
                logger.warning("[trace=%s] Task %s timed out", trace_id, task_id)
                cleanup_background_task(task_id)
                return _task_result_command(
                    tool_call_id=tool_call_id,
                    status="timed_out",
                    error=result.error,
                    model_name=effective_model,
                    usage=usage,
                )

            # Still running. Subscribe once, then wait for a change event
            # (with a heartbeat upper bound) instead of fixed-interval polling.
            if change_event is None:
                change_signal, change_event = _subscribe_subagent_changes(result)
                if change_event is not None:
                    # Re-read state: a transition may have raced the subscribe
                    # (the terminal latch in subscribe() covers most of this,
                    # but a fresh read also picks up new progress messages).
                    continue

            active_deadline = execution_deadline or queue_deadline
            remaining = active_deadline - time.monotonic()
            # This is a tool-side safety net for a wedged queue or execution.
            # The isolated-loop coroutine enforces the configured execution
            # timeout independently and normally reaches TIMED_OUT first.
            if remaining <= 0:
                timeout_minutes = config.timeout_seconds // 60
                wait_phase = "execution" if execution_deadline is not None else "queue"
                logger.error(
                    "[trace=%s] Task %s %s wait budget exhausted after %.0fs",
                    trace_id,
                    task_id,
                    wait_phase,
                    time.monotonic() - wait_started,
                )
                _report_subagent_usage(runtime, result)
                usage = _summarize_usage(getattr(result, "token_usage_records", None))
                _cache_subagent_usage(tool_call_id, usage, enabled=cache_token_usage)
                writer(
                    {
                        "type": "task_timed_out",
                        "task_id": task_id,
                        "usage": usage,
                        "model_name": effective_model,
                    }
                )
                # The task may still be queued or running. Cancel its isolated-loop
                # future and schedule deferred cleanup until it reaches terminal state.
                request_cancel_background_task(task_id)
                _schedule_deferred_subagent_cleanup(task_id, trace_id, wait_budget_seconds)
                message = f"Task polling timed out after {timeout_minutes} minutes. This may indicate the background task is stuck. Status: {result.status.value}"
                return _task_result_command(
                    tool_call_id=tool_call_id,
                    status="polling_timed_out",
                    error=message,
                    model_name=effective_model,
                    usage=usage,
                )

            slice_seconds = min(_SUBAGENT_WAIT_HEARTBEAT_SECONDS, remaining)
            if change_event is not None:
                await wait_for_change(change_event, heartbeat_seconds=slice_seconds)
            else:
                await asyncio.sleep(slice_seconds)
    except asyncio.CancelledError:
        # Cancel the isolated-loop execution and retain the cooperative signal.
        request_cancel_background_task(task_id)

        # Wait (shielded) for the subagent to reach a terminal state so the
        # final token usage snapshot is reported to the parent RunJournal
        # before the parent worker persists get_completion_data().
        terminal_result = None
        try:
            terminal_result = await asyncio.shield(_await_subagent_terminal(task_id, wait_budget_seconds))
        except asyncio.CancelledError:
            pass

        # Report whatever the subagent collected (even if we timed out).
        final_result = terminal_result or get_background_task_result(task_id)
        if final_result is not None:
            _report_subagent_usage(runtime, final_result)
        if final_result is not None and _is_subagent_terminal(final_result):
            cleanup_background_task(task_id)
        else:
            _schedule_deferred_subagent_cleanup(task_id, trace_id, wait_budget_seconds)
        _subagent_usage_cache.pop(tool_call_id, None)
        raise
    except Exception:
        _subagent_usage_cache.pop(tool_call_id, None)
        raise
    finally:
        if change_signal is not None and change_event is not None:
            change_signal.unsubscribe(change_event)
