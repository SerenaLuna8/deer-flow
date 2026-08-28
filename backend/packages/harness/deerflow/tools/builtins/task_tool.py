"""Task tool for delegating work to subagents."""

import asyncio
import inspect
import logging
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

from deerflow.error_codes import SUBAGENT_EXECUTION_FAILED_ERROR_CODE
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.host_execution_approval import (
    HostExecutionApprovalArtifact,
)
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.sandbox.security import (
    LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE,
    is_host_bash_available,
    requires_host_bash_approval,
)
from deerflow.subagents import get_available_subagent_names, get_subagent_config
from deerflow.subagents.binding import (
    ConfiguredLeadParentExecutionProfile,
    EmbeddedParentExecutionProfile,
    ParentExecutionBinding,
    ParentExecutionBindingFactory,
    PrivateRunParentExecutionProfile,
    SdkParentExecutionProfile,
    invoke_parent_operation_on_owner_loop,
)
from deerflow.subagents.config import SubagentConfig, resolve_subagent_model_name
from deerflow.subagents.delegated_context import (
    project_delegated_runtime_context,
)
from deerflow.subagents.executor import _SubagentGraphRunner
from deerflow.subagents.lifecycle import (
    SubagentApprovalRequired,
    SubagentCancelled,
    SubagentCompleted,
    SubagentFailed,
    SubagentFailureCode,
    SubagentTaskCall,
    SubagentTaskEvent,
    SubagentTaskOutcome,
    SubagentTimedOut,
    SubagentTimeoutPhase,
    SubagentTokenUsage,
    SubagentUsageSettlement,
    subagent_task_lifecycle,
)
from deerflow.subagents.runtime_catalog import trusted_runtime_agent_catalog
from deerflow.subagents.status_contract import (
    SUBAGENT_TOKEN_USAGE_KEY,
    SUBAGENT_USAGE_COMPLETENESS_KEY,
    SUBAGENT_USAGE_RECEIPT_ID_KEY,
    SubagentStatusValue,
    SubagentStopReasonValue,
    SubagentUsageCompletenessValue,
    format_subagent_result_message,
    make_subagent_additional_kwargs,
)
from deerflow.tools.types import Runtime
from deerflow.trace_context import get_current_trace_id

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig
    from deerflow.subagents.runtime_catalog import RuntimeAgentProfile

logger = logging.getLogger(__name__)

_SUBAGENT_COORDINATION_GRACE_SECONDS = 60.0
_PRIVATE_SUBAGENT_BASH_DISABLED_MESSAGE = "Private Sub-Agent bash is unavailable because this Sandbox cannot provide a per-Task filesystem namespace"


def _trusted_private_mcp_tools(
    raw_tools: object,
) -> tuple[BaseTool, ...]:
    """Return only opaque Worker-installed private MCP proxy objects."""

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
    binding: ParentExecutionBinding,
) -> StructuredTool:
    """Keep delegated MCP calls on the parent loop that owns the exact runtime."""

    args_schema = admitted_tool.args_schema
    if not isinstance(args_schema, type) or not issubclass(args_schema, BaseModel):
        raise RuntimeError("Private MCP tool schema is unavailable")

    async def invoke(**arguments):
        return await invoke_parent_operation_on_owner_loop(
            binding,
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


def _merge_skill_allowlists(parent: list[str] | None, child: list[str] | None) -> list[str] | None:
    """Return the effective subagent skill allowlist under the parent policy."""
    if parent is None:
        return child
    if child is None:
        return list(parent)

    parent_set = set(parent)
    return [skill for skill in child if skill in parent_set]


def _profile_app_config(profile: object) -> "AppConfig | None":
    if type(profile) in {
        EmbeddedParentExecutionProfile,
        ConfiguredLeadParentExecutionProfile,
        PrivateRunParentExecutionProfile,
    }:
        return cast("AppConfig", profile.app_config)
    if type(profile) is SdkParentExecutionProfile:
        return None
    raise TypeError("unsupported parent execution profile")


def _profile_parent_model_name(profile: object) -> str | None:
    if type(profile) in {
        EmbeddedParentExecutionProfile,
        ConfiguredLeadParentExecutionProfile,
        PrivateRunParentExecutionProfile,
    }:
        value = profile.model_name
        return value if isinstance(value, str) and value else None
    if type(profile) is not SdkParentExecutionProfile:
        raise TypeError("unsupported parent execution profile")
    model = profile.graph.model
    for attribute in ("model_name", "model"):
        value = getattr(model, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _sdk_subagent_config(name: str) -> SubagentConfig | None:
    """Resolve only built-ins for a config-free SDK graph.

    The SDK caller did not select the process AppConfig, so loading custom
    subagents or overrides from that global object would cross profiles.
    """

    from deerflow.subagents.builtins import BUILTIN_SUBAGENTS

    config = BUILTIN_SUBAGENTS.get(name)
    return replace(config) if config is not None else None


def _sdk_available_subagent_names(profile: SdkParentExecutionProfile) -> list[str]:
    names = ["general-purpose"]
    if any(tool.name == "bash" for tool in profile.graph.tools):
        names.append("bash")
    return names


def _bind_parent_execution(runtime: Runtime) -> ParentExecutionBinding | None:
    context = runtime.context if runtime is not None else None
    context = context if isinstance(context, Mapping) else {}
    factory = context.get(RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY)
    if type(factory) is not ParentExecutionBindingFactory:
        logger.error("Sub-Agent Task rejected because its graph binding is unavailable")
        return None
    try:
        return factory.bind(runtime)
    except (PermissionError, RuntimeError, TypeError):
        logger.error("Sub-Agent Task rejected because its graph binding is invalid")
        return None


def _usage_payload(usage: SubagentTokenUsage | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }


def _plain_event_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_event_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_event_value(item) for item in value]
    if isinstance(value, frozenset):
        return [_plain_event_value(item) for item in value]
    return value


_TOOL_CONTROL_FAILURE_PRESENTATION: dict[SubagentFailureCode, str] = {
    SubagentFailureCode.TOOL_CALL_CONTROL_STATE_INVALID: ("TOOL_CALL_CONTROL_STATE_INVALID: The Sub-Agent Task stopped because its tool-control state could not be validated."),
    SubagentFailureCode.LOOP_FINALIZATION_FAILED: ("LOOP_FINALIZATION_FAILED: The Sub-Agent Task did not complete the required tool-free final response."),
}


def _failure_presentation(outcome: SubagentFailed) -> str:
    """Choose adapter-owned wording for one stable lifecycle failure."""

    return outcome.detail or _TOOL_CONTROL_FAILURE_PRESENTATION.get(
        outcome.failure_code,
        outcome.failure_code.value,
    )


def _cancellation_presentation(outcome: SubagentCancelled) -> str:
    """Preserve the v1 task wire without leaking lifecycle reason wording."""

    del outcome
    return "Cancelled by user"


class _TaskLifecycleEventAdapter:
    """Translate immutable lifecycle events to the existing SSE wire."""

    def __init__(
        self,
        *,
        writer: Any,
        task_id: str,
        description: str,
        model_name: str | None,
        execution_timeout_seconds: int,
    ) -> None:
        self._writer = writer
        self._task_id = task_id
        self._description = description
        self._model_name = model_name
        self._execution_timeout_seconds = execution_timeout_seconds
        self._started = False
        self._message_count = 0

    async def __call__(self, event: SubagentTaskEvent) -> None:
        execution_id = str(event.execution_id)
        if not self._started:
            self._writer(
                {
                    "type": "task_started",
                    "task_id": self._task_id,
                    "execution_id": execution_id,
                    "description": self._description,
                    "model_name": self._model_name,
                }
            )
            self._started = True

        usage = _usage_payload(event.usage)
        current_message_count = len(event.ai_messages)
        for index in range(self._message_count, current_message_count):
            self._writer(
                {
                    "type": "task_running",
                    "task_id": self._task_id,
                    "execution_id": execution_id,
                    "message": _plain_event_value(event.ai_messages[index]),
                    "message_index": index + 1,
                    "total_messages": current_message_count,
                    "usage": usage,
                    "usage_completeness": event.usage_completeness.value,
                    "model_name": self._model_name,
                }
            )
        self._message_count = current_message_count

        if isinstance(event, SubagentCompleted):
            self._writer(
                {
                    "type": "task_completed",
                    "task_id": self._task_id,
                    "execution_id": execution_id,
                    "result": event.result,
                    "usage": usage,
                    "usage_completeness": event.usage_completeness.value,
                    "model_name": self._model_name,
                    **({"stop_reason": event.stop_reason} if event.stop_reason is not None else {}),
                }
            )
        elif isinstance(event, SubagentFailed):
            self._writer(
                {
                    "type": "task_failed",
                    "task_id": self._task_id,
                    "execution_id": execution_id,
                    "error": _failure_presentation(event),
                    "usage": usage,
                    "usage_completeness": event.usage_completeness.value,
                    "model_name": self._model_name,
                    **({"stop_reason": event.stop_reason} if event.stop_reason is not None else {}),
                }
            )
        elif isinstance(event, SubagentCancelled):
            self._writer(
                {
                    "type": "task_cancelled",
                    "task_id": self._task_id,
                    "execution_id": execution_id,
                    "error": _cancellation_presentation(event),
                    "usage": usage,
                    "usage_completeness": event.usage_completeness.value,
                    "model_name": self._model_name,
                }
            )
        elif isinstance(event, SubagentTimedOut):
            payload = {
                "type": "task_timed_out",
                "task_id": self._task_id,
                "execution_id": execution_id,
                "usage": usage,
                "usage_completeness": event.usage_completeness.value,
                "model_name": self._model_name,
            }
            if event.quiescent and event.timeout_phase is SubagentTimeoutPhase.EXECUTION:
                payload["error"] = f"Execution timed out after {self._execution_timeout_seconds} seconds"
            self._writer(payload)


def _new_subagent_graph_runner(**kwargs: Any) -> _SubagentGraphRunner:
    """Narrow construction seam exercised only after lifecycle admission."""

    return _SubagentGraphRunner(**kwargs)


def _usage_settlement_hook(
    runtime: Runtime,
    parent_binding: ParentExecutionBinding,
):
    recorder = _find_usage_recorder(runtime)
    if recorder is None:
        return None

    async def settle(settlement: SubagentUsageSettlement) -> None:
        # SubagentTaskLifecycle invokes settlement from the Run owner loop,
        # after graph and inherited-operation quiescence.  The barrier is
        # sealed by then, so settlement must not try to open a child receipt.
        if asyncio.get_running_loop() is not parent_binding.owner_loop:
            raise RuntimeError("usage settlement left the parent owner loop")
        records = [dict(record) for record in settlement.records]
        if not records:
            return
        result = recorder.record_external_llm_usage_records(records)
        if inspect.isawaitable(result):
            await result

    return settle


def _outcome_command(
    outcome: SubagentTaskOutcome,
    *,
    tool_call_id: str,
    model_name: str | None,
    execution_timeout_seconds: int,
    delegated_output_promotions: object = (),
) -> Command:
    usage = _usage_payload(outcome.usage)
    receipt_id = str(outcome.execution_id)
    if isinstance(outcome, SubagentApprovalRequired):
        return _host_execution_approval_command(
            tool_call_id=tool_call_id,
            artifact=outcome.artifact,
            usage=usage,
            usage_receipt_id=receipt_id,
            usage_completeness=outcome.usage_completeness.value,
            delegated_output_promotions=delegated_output_promotions,
        )
    if isinstance(outcome, SubagentCompleted):
        return _task_result_command(
            tool_call_id=tool_call_id,
            status="completed",
            result=outcome.result,
            lead_result=_result_with_delegated_output_promotions(
                outcome.result,
                delegated_output_promotions,
            ),
            stop_reason=outcome.stop_reason,
            model_name=model_name,
            usage=usage,
            usage_receipt_id=receipt_id,
            usage_completeness=outcome.usage_completeness.value,
        )
    if isinstance(outcome, SubagentFailed):
        error = _failure_presentation(outcome)
        return _task_result_command(
            tool_call_id=tool_call_id,
            status="failed",
            error=error,
            lead_error=_result_with_delegated_output_promotions(
                error,
                delegated_output_promotions,
            ),
            stop_reason=outcome.stop_reason,
            model_name=model_name,
            usage=usage,
            usage_receipt_id=receipt_id,
            usage_completeness=outcome.usage_completeness.value,
        )
    if isinstance(outcome, SubagentCancelled):
        error = _cancellation_presentation(outcome)
        return _task_result_command(
            tool_call_id=tool_call_id,
            status="cancelled",
            error=error,
            lead_error=_result_with_delegated_output_promotions(
                error,
                delegated_output_promotions,
            ),
            model_name=model_name,
            usage=usage,
            usage_receipt_id=receipt_id,
            usage_completeness=outcome.usage_completeness.value,
        )
    if isinstance(outcome, SubagentTimedOut):
        if outcome.timeout_phase is SubagentTimeoutPhase.QUEUE or not outcome.quiescent:
            timeout_minutes = execution_timeout_seconds // 60
            status = "pending" if outcome.started_at is None else "running"
            error = f"Task polling timed out after {timeout_minutes} minutes. This may indicate the background task is stuck. Status: {status}"
            return _task_result_command(
                tool_call_id=tool_call_id,
                status="polling_timed_out",
                error=error,
                lead_error=_result_with_delegated_output_promotions(
                    error,
                    delegated_output_promotions,
                ),
                model_name=model_name,
                usage=usage,
                usage_receipt_id=receipt_id,
                usage_completeness=outcome.usage_completeness.value,
            )
        error = f"Execution timed out after {execution_timeout_seconds} seconds"
        return _task_result_command(
            tool_call_id=tool_call_id,
            status="timed_out",
            error=error,
            lead_error=_result_with_delegated_output_promotions(
                error,
                delegated_output_promotions,
            ),
            model_name=model_name,
            usage=usage,
            usage_receipt_id=receipt_id,
            usage_completeness=outcome.usage_completeness.value,
        )
    raise TypeError("unsupported Sub-Agent Task outcome")


def _result_with_delegated_output_promotions(
    result: str,
    raw_promotions: object,
) -> str:
    """Tell the Lead where delegated outputs were isolated for promotion."""

    if not isinstance(raw_promotions, tuple):
        raise RuntimeError("Delegated output isolation returned an invalid result")
    if not raw_promotions:
        return result
    mappings: list[tuple[str, str]] = []
    for promotion in raw_promotions:
        source_path = getattr(promotion, "source_path", None)
        scratch_path = getattr(promotion, "scratch_path", None)
        if (
            type(source_path) is not str
            or not source_path.startswith("/mnt/user-data/outputs/")
            or type(scratch_path) is not str
            or not scratch_path.startswith(
                "/mnt/user-data/workspace/.deerflow/subagents/",
            )
        ):
            raise RuntimeError(
                "Delegated output isolation returned an invalid result",
            )
        mappings.append((source_path, scratch_path))

    lines = [
        "Delegated output files were isolated as scratch files and are not final Run outputs:",
        *(f"- {source} -> {scratch}" for source, scratch in mappings),
        ("To deliver one, copy the selected scratch file to a new /mnt/user-data/outputs path, then call present_files."),
    ]
    return f"{result.rstrip()}\n\n" + "\n".join(lines)


def _task_result_command(
    *,
    tool_call_id: str,
    status: SubagentStatusValue,
    result: str | None = None,
    error: str | None = None,
    lead_result: str | None = None,
    lead_error: str | None = None,
    stop_reason: SubagentStopReasonValue | None = None,
    model_name: str | None = None,
    usage: dict[str, int] | None = None,
    usage_receipt_id: str | None = None,
    usage_completeness: SubagentUsageCompletenessValue | None = None,
) -> Command:
    """Build one Task result with separate Lead and user-card projections.

    ``ToolMessage.content`` remains model-visible and may include private
    delegated-output promotion mappings. Structured metadata feeds the public
    Sub-Agent card and therefore retains only the Sub-Agent's original result
    or error, without Lead-only scratch paths or publication instructions.
    """

    content, _ = format_subagent_result_message(
        status,
        result=lead_result if lead_result is not None else result,
        error=lead_error if lead_error is not None else error,
        stop_reason=stop_reason,
    )
    _, metadata_error = format_subagent_result_message(
        status,
        result=result,
        error=error,
        stop_reason=stop_reason,
    )
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
                        usage_receipt_id=usage_receipt_id,
                        usage_completeness=usage_completeness,
                    ),
                )
            ]
        }
    )


def _host_execution_approval_command(
    *,
    tool_call_id: str,
    artifact: HostExecutionApprovalArtifact,
    usage: dict[str, int] | None,
    usage_receipt_id: str,
    usage_completeness: SubagentUsageCompletenessValue,
    delegated_output_promotions: object = (),
) -> Command:
    """Bubble a delegated approval anchor into the parent Agent checkpoint."""

    if not isinstance(delegated_output_promotions, tuple):
        raise RuntimeError("Delegated output isolation returned an invalid result")
    content = "Delegated host command execution requires approval."
    if delegated_output_promotions:
        content += " Scratch files created before this approval pause are discarded; regenerate them after approval if needed."

    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=content,
                    tool_call_id=tool_call_id,
                    name="task",
                    artifact={
                        "host_execution_approval": artifact.to_payload(),
                    },
                    additional_kwargs={
                        **({SUBAGENT_TOKEN_USAGE_KEY: usage} if usage is not None else {}),
                        SUBAGENT_USAGE_RECEIPT_ID_KEY: usage_receipt_id,
                        SUBAGENT_USAGE_COMPLETENESS_KEY: usage_completeness,
                    },
                ),
            ],
        },
        goto=END,
    )


async def _assemble_subagent_tools(
    *,
    parent_binding: ParentExecutionBinding,
    parent_context: dict[str, Any],
    runtime_agent_profile: "RuntimeAgentProfile | None",
    effective_model: str | None,
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

    profile = parent_binding.profile
    private_run = type(profile) is PrivateRunParentExecutionProfile
    if type(profile) is SdkParentExecutionProfile:
        # SDK graphs own their concrete tool capability set.  Reusing that
        # captured set avoids silently loading a process-global application
        # profile that the SDK caller never selected.
        return list(profile.graph.tools)

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

    asset_context = getattr(profile, "asset_context", None)
    if asset_context is not None:
        available_tools_kwargs["asset_context"] = asset_context

    tools = await asyncio.to_thread(get_available_tools, **available_tools_kwargs)
    if private_run:
        # A shared private sandbox cannot provide a per-process mount namespace
        # for arbitrary shell paths. File tools are routed through the exact
        # delegated output view below; unrestricted bash could still reach the
        # Lead's canonical outputs through relative paths, variables, or scripts.
        # Keep the isolation boundary strong by failing that capability closed.
        tools = [tool for tool in tools if tool.name != "bash"]
    if runtime_agent_profile is None:
        parent_tool_names = {tool.name for tool in profile.graph.tools}
        tools = [tool for tool in tools if tool.name in parent_tool_names]
    if parent_context.get(RuntimeContextKeys.NON_INTERACTIVE) is True and app_config is not None and requires_host_bash_approval(app_config):
        tools = [tool for tool in tools if tool.name != "bash"]
    if private_run:
        admitted_mcp_tools = (
            runtime_agent_profile.mcp_tools
            if runtime_agent_profile is not None
            else _trusted_private_mcp_tools(
                getattr(profile.private_runtime, "mcp_tools", ()),
            )
        )
        existing_names = {tool.name for tool in tools}
        for admitted_tool in admitted_mcp_tools:
            if admitted_tool.name in existing_names:
                raise RuntimeError("Private MCP tool name conflicts with another tool")
            tools.append(
                _wrap_private_mcp_tool_for_owner_loop(
                    admitted_tool,
                    parent_binding,
                )
            )
            existing_names.add(admitted_tool.name)
    return tools


async def _run_task_through_lifecycle(
    *,
    runtime: Runtime,
    description: str,
    prompt: str,
    subagent_type: str,
    tool_call_id: str,
) -> Command:
    parent_binding = _bind_parent_execution(runtime)
    if parent_binding is None:
        return _task_result_command(
            tool_call_id=tool_call_id,
            status="failed",
            error=SUBAGENT_EXECUTION_FAILED_ERROR_CODE,
        )

    profile = parent_binding.profile
    parent_context = dict(parent_binding.context)
    private_run = type(profile) is PrivateRunParentExecutionProfile
    file_authority = parent_context.get(RuntimeContextKeys.FILE_AUTHORITY)
    delegated_output_scope = getattr(file_authority, "delegated_output_scope", None) if private_run else None
    if private_run and not callable(delegated_output_scope):
        return _task_result_command(
            tool_call_id=tool_call_id,
            status="failed",
            error=SUBAGENT_EXECUTION_FAILED_ERROR_CODE,
        )
    app_config = _profile_app_config(profile)
    runtime_agent_catalog = trusted_runtime_agent_catalog(profile.runtime_agent_catalog) if private_run else None
    runtime_agent_profile = runtime_agent_catalog.get(subagent_type) if runtime_agent_catalog is not None else None

    if type(profile) is SdkParentExecutionProfile:
        static_subagent_names = _sdk_available_subagent_names(profile)
    else:
        static_subagent_names = get_available_subagent_names(
            app_config=app_config,
        )
    available_subagent_names = list(
        dict.fromkeys(
            [
                *static_subagent_names,
                *(runtime_agent_catalog.names if runtime_agent_catalog else ()),
            ]
        )
    )

    # PREFLIGHT is intentionally cheap.  Tool/Skill materialization and graph
    # construction stay inside the async runner factory after scheduler entry.
    if runtime_agent_profile is not None:
        config = SubagentConfig(
            name=runtime_agent_profile.key,
            description=runtime_agent_profile.description,
            system_prompt=None,
            tools=None,
            disallowed_tools=["task"],
            skills=None,
            model=runtime_agent_profile.model_name,
        )
    elif type(profile) is SdkParentExecutionProfile:
        config = _sdk_subagent_config(subagent_type)
    else:
        config = get_subagent_config(subagent_type, app_config=app_config)
    if config is None:
        available = ", ".join(available_subagent_names)
        return _task_result_command(
            tool_call_id=tool_call_id,
            status="failed",
            error=(f"Unknown subagent type '{subagent_type}'. Available: {available}"),
        )

    if subagent_type == "bash":
        if private_run:
            return _task_result_command(
                tool_call_id=tool_call_id,
                status="failed",
                error=_PRIVATE_SUBAGENT_BASH_DISABLED_MESSAGE,
            )
        if type(profile) is SdkParentExecutionProfile:
            host_bash_allowed = "bash" in static_subagent_names
            approval_unavailable = False
        else:
            host_bash_allowed = is_host_bash_available(app_config)
            approval_unavailable = parent_context.get(RuntimeContextKeys.NON_INTERACTIVE) is True and requires_host_bash_approval(app_config)
        if not host_bash_allowed or approval_unavailable:
            return _task_result_command(
                tool_call_id=tool_call_id,
                status="failed",
                error=LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE,
            )

    state = parent_binding.state
    runnable_config = parent_binding.config
    raw_metadata = runnable_config.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    sandbox_state = state.get("sandbox")
    thread_data = state.get("thread_data")
    trace_id_value = metadata.get("trace_id")
    trace_id = trace_id_value if isinstance(trace_id_value, str) and trace_id_value else str(uuid.uuid4())[:8]
    parent_model = _profile_parent_model_name(profile)

    overrides: dict[str, object] = {}
    if runtime_agent_profile is None:
        if type(profile) in {
            EmbeddedParentExecutionProfile,
            ConfiguredLeadParentExecutionProfile,
        }:
            parent_available_skills = profile.available_skills
        else:
            parent_available_skills = None
        if parent_available_skills is not None:
            overrides["skills"] = _merge_skill_allowlists(
                list(parent_available_skills),
                config.skills,
            )
    if overrides:
        config = replace(config, **overrides)

    if config.model == "inherit":
        effective_model = parent_model
        if effective_model is None and type(profile) is not SdkParentExecutionProfile:
            effective_model = resolve_subagent_model_name(
                config,
                parent_model,
                app_config=app_config,
            )
    else:
        effective_model = config.model

    effective_tool_groups = runtime_agent_profile.tool_groups if runtime_agent_profile is not None else profile.tool_groups if private_run else None

    if runtime_agent_profile is not None:
        agent_prompt_bundle = runtime_agent_profile.prompt_bundle
    elif private_run:
        agent_prompt_bundle = getattr(profile.private_runtime, "prompt_bundle", None)
    else:
        agent_prompt_bundle = _trusted_agent_prompt_bundle(parent_context)
    # Runtime Agent profiles own their exact immutable Skill closure. Private
    # static delegates inherit the Run snapshot; other profiles materialize
    # Skills from their graph/profile inputs and never from raw invocation keys.
    runtime_skills = runtime_agent_profile.runtime_skills if runtime_agent_profile is not None else profile.runtime_skills if private_run else ()
    delegated_output_root: str | None = None

    async def materialize_graph_runner() -> _SubagentGraphRunner:
        delegated_context = project_delegated_runtime_context(
            parent_binding,
            subagent_name=config.name,
            fallback_user_id=resolve_runtime_user_id(runtime),
            fallback_trace_id=get_current_trace_id(),
            agent_prompt_bundle=agent_prompt_bundle,
            runtime_skills=tuple(runtime_skills),
            delegated_output_root=delegated_output_root,
        )
        tools = await _assemble_subagent_tools(
            parent_binding=parent_binding,
            parent_context=parent_context,
            runtime_agent_profile=runtime_agent_profile,
            effective_model=effective_model,
            effective_tool_groups=effective_tool_groups,
            app_config=app_config,
        )
        executor_kwargs: dict[str, Any] = {
            "config": config,
            "tools": tools,
            "parent_model": parent_model,
            "sandbox_state": sandbox_state,
            "thread_data": thread_data,
            "trace_id": trace_id,
            "delegated_context": delegated_context,
            "tool_call_control_topology": parent_binding.tool_call_control_topology,
            "tool_call_control_observer": parent_binding.tool_call_control_observer,
        }
        if parent_binding.context_evidence_observer_factory is not None:
            executor_kwargs["context_evidence_observer_factory"] = parent_binding.create_subagent_context_evidence_observer
        if runtime_agent_profile is not None:
            executor_kwargs["agent_model_settings"] = runtime_agent_profile.model_settings
        if type(profile) is SdkParentExecutionProfile:
            executor_kwargs.update(
                {
                    "model_override": profile.graph.model,
                    "sdk_feature_snapshot": profile.features,
                    "middleware_override": (profile.graph.middleware if profile.full_middleware_takeover else None),
                    "tool_search_enabled": False,
                }
            )
        return _new_subagent_graph_runner(**executor_kwargs)

    lifecycle_binding = parent_binding.to_lifecycle_binding(
        materialize_graph_runner,
        settle_usage=_usage_settlement_hook(runtime, parent_binding),
    )
    wait_budget_seconds = float(config.timeout_seconds + 60)
    event_adapter = _TaskLifecycleEventAdapter(
        writer=get_stream_writer(),
        task_id=tool_call_id,
        description=description,
        model_name=effective_model,
        execution_timeout_seconds=config.timeout_seconds,
    )
    lifecycle_call = SubagentTaskCall(
        task_id=tool_call_id,
        prompt=prompt,
        queue_timeout_seconds=wait_budget_seconds,
        execution_timeout_seconds=float(config.timeout_seconds),
        quiescence_timeout_seconds=_SUBAGENT_COORDINATION_GRACE_SECONDS,
    )
    delegated_output_capture: object | None = None
    if callable(delegated_output_scope):
        async with delegated_output_scope(tool_call_id) as delegated_output_capture:
            raw_output_root = getattr(
                delegated_output_capture,
                "output_root",
                None,
            )
            if type(raw_output_root) is not str or not raw_output_root:
                raise RuntimeError(
                    "Delegated output isolation returned an invalid root",
                )
            delegated_output_root = raw_output_root
            outcome = await subagent_task_lifecycle.run(
                lifecycle_call,
                lifecycle_binding,
                observers=(event_adapter,),
            )
    else:
        outcome = await subagent_task_lifecycle.run(
            lifecycle_call,
            lifecycle_binding,
            observers=(event_adapter,),
        )
    return _outcome_command(
        outcome,
        tool_call_id=tool_call_id,
        model_name=effective_model,
        execution_timeout_seconds=config.timeout_seconds,
        delegated_output_promotions=getattr(
            delegated_output_capture,
            "promotions",
            (),
        ),
    )


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
    return await _run_task_through_lifecycle(
        runtime=runtime,
        description=description,
        prompt=prompt,
        subagent_type=subagent_type,
        tool_call_id=tool_call_id,
    )
