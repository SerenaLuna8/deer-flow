"""Tool exception handling and compatibility exports for assembly builders."""

import logging
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.agents.middlewares.skill_context import (
    SKILL_CONTEXT_ENTRY_KEY,
    _tool_call_path,
    build_skill_entry_metadata_from_read,
)
from deerflow.agents.middlewares.tool_result_meta import (
    normalize_tool_result,
    stamp_exception_meta,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.summarization_config import (
    DEFAULT_SKILL_FILE_READ_TOOL_NAMES,
)
from deerflow.constants import DEFAULT_SKILLS_CONTAINER_PATH
from deerflow.error_codes import TOOL_EXECUTION_FAILED_ERROR_CODE
from deerflow.sandbox.sandbox import (
    AuthorizationRevoked,
    check_authorization_boundary,
)
from deerflow.subagents.status_contract import (
    format_subagent_result_message,
    make_subagent_additional_kwargs,
)
from deerflow.tools.mcp_metadata import is_private_mcp_tool

logger = logging.getLogger(__name__)

_MISSING_TOOL_CALL_ID = "missing_tool_call_id"
_TASK_TOOL_NAME = "task"
_RECOVERY_HINT = "Continue with available context, or choose an alternative tool."
_TRUSTED_READ_ONLY_TOOL_MARKER = object()
_TRUSTED_IDEMPOTENT_TOOL_MARKER = object()


def mark_trusted_read_only_tool(tool: object) -> object:
    """Mark one code-created tool as safe for the read-only lease boundary.

    The marker lives on the registered Python callable and is compared by
    object identity.  It cannot be supplied through model tool arguments or a
    serialized tool definition.  Application-owned runtimes use this narrow
    hook for fixed read-only tools whose objects are created per Run, where a
    module-level identity check is therefore not possible.
    """

    for attribute in ("coroutine", "func"):
        implementation = getattr(tool, attribute, None)
        if callable(implementation):
            setattr(
                implementation,
                "__deerflow_trusted_read_only_tool__",
                _TRUSTED_READ_ONLY_TOOL_MARKER,
            )
            return tool
    raise TypeError("trusted read-only tools require a registered callable")


def mark_trusted_idempotent_tool(tool: object) -> object:
    """Mark an app-owned tool whose durable mutation is Run-idempotent."""

    for attribute in ("coroutine", "func"):
        implementation = getattr(tool, attribute, None)
        if callable(implementation):
            setattr(
                implementation,
                "__deerflow_trusted_idempotent_tool__",
                _TRUSTED_IDEMPOTENT_TOOL_MARKER,
            )
            return tool
    raise TypeError("trusted idempotent tools require a registered callable")


def _is_trusted_read_only_tool(request: ToolCallRequest) -> bool:
    """Recognize only canonical code-registered read-only tool objects."""

    from deerflow.tools.builtins.list_uploaded_files_tool import (
        list_uploaded_files_tool,
    )

    tool = getattr(request, "tool", None)
    if tool is list_uploaded_files_tool:
        return True
    return any(
        getattr(
            getattr(tool, attribute, None),
            "__deerflow_trusted_read_only_tool__",
            None,
        )
        is _TRUSTED_READ_ONLY_TOOL_MARKER
        for attribute in ("coroutine", "func")
    )


def _is_trusted_idempotent_tool(request: ToolCallRequest) -> bool:
    tool = getattr(request, "tool", None)
    return any(
        getattr(
            getattr(tool, attribute, None),
            "__deerflow_trusted_idempotent_tool__",
            None,
        )
        is _TRUSTED_IDEMPOTENT_TOOL_MARKER
        for attribute in ("coroutine", "func")
    )


def _stamp_task_exception_status(
    message: ToolMessage,
    *,
    tool_name: str,
    error: str,
) -> ToolMessage:
    """Stamp failed metadata on task exception wrappers produced here."""
    if tool_name != _TASK_TOOL_NAME:
        return message
    content, metadata_error = format_subagent_result_message("failed", error=error)
    if not content.endswith((".", "!", "?")):
        content += "."
    message.content = f"{content} {_RECOVERY_HINT}"
    existing = dict(message.additional_kwargs or {})
    existing.update(make_subagent_additional_kwargs("failed", error=metadata_error))
    message.additional_kwargs = existing
    return message


class ToolErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    """Convert tool exceptions into error ToolMessages so the run can continue."""

    def __init__(self, *, app_config: AppConfig | None = None) -> None:
        super().__init__()
        self._app_config = app_config
        if app_config is None:
            self._skill_read_tool_names = frozenset(DEFAULT_SKILL_FILE_READ_TOOL_NAMES)
            self._skills_root = DEFAULT_SKILLS_CONTAINER_PATH
        else:
            self._skill_read_tool_names = frozenset(app_config.summarization.skill_file_read_tool_names)
            self._skills_root = app_config.skills.container_path

    def _build_error_message(
        self,
        request: ToolCallRequest,
        exc: Exception,
    ) -> ToolMessage:
        del exc
        tool_name = str(request.tool_call.get("name") or "unknown_tool")
        tool_call_id = str(request.tool_call.get("id") or _MISSING_TOOL_CALL_ID)

        content = f"Error: Tool '{tool_name}' failed ({TOOL_EXECUTION_FAILED_ERROR_CODE}). {_RECOVERY_HINT}"
        message = ToolMessage(
            content=content,
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
            additional_kwargs={
                "error_code": TOOL_EXECUTION_FAILED_ERROR_CODE,
            },
        )
        # This middleware is the producer for exception wrappers, so task
        # failures raised before task_tool can build its own Command still
        # carry the same structured metadata.
        message = _stamp_task_exception_status(
            message,
            tool_name=tool_name,
            error=TOOL_EXECUTION_FAILED_ERROR_CODE,
        )
        return stamp_exception_meta(
            message,
            TOOL_EXECUTION_FAILED_ERROR_CODE,
        )

    def _stamp_skill_read_metadata(
        self,
        message: ToolMessage,
        request: ToolCallRequest,
        *,
        tool_name: str,
    ) -> ToolMessage:
        if tool_name not in self._skill_read_tool_names:
            return message
        if getattr(message, "status", "success") == "error":
            return message
        content = message.content if isinstance(message.content, str) else None
        if content is None:
            return message
        path = _tool_call_path(request.tool_call)
        if path is None:
            return message
        entry = build_skill_entry_metadata_from_read(
            path,
            content,
            skills_root=self._skills_root,
        )
        if entry is None:
            return message
        existing = dict(message.additional_kwargs or {})
        existing[SKILL_CONTEXT_ENTRY_KEY] = dict(entry)
        message.additional_kwargs = existing
        return message

    def _maybe_stamp(
        self,
        result: ToolMessage | Command,
        request: ToolCallRequest,
    ) -> ToolMessage | Command:
        """Apply producer-bound metadata for tool results that need it."""
        if not isinstance(result, ToolMessage):
            return result
        tool_name = str(request.tool_call.get("name") or "")
        return self._stamp_skill_read_metadata(
            result,
            request,
            tool_name=tool_name,
        )

    @staticmethod
    def _runtime_context(request: object) -> object | None:
        runtime = getattr(request, "runtime", None)
        return getattr(runtime, "context", None)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        # This middleware is inside the retry wrapper, so every retry reaches
        # the database-backed authorization check before invoking the model.
        await check_authorization_boundary(
            self._runtime_context(request),
            "before_model_call",
        )
        return await handler(request)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        runtime_context = self._runtime_context(request)
        if isinstance(runtime_context, dict) and "private_scope" in runtime_context:
            raise AuthorizationRevoked
        try:
            result = handler(request)
        except GraphBubbleUp:
            # Preserve LangGraph control-flow signals (interrupt/pause/resume).
            raise
        except Exception as exc:
            logger.error(
                "Tool execution failed (sync): error_code=%s name=%s id=%s",
                TOOL_EXECUTION_FAILED_ERROR_CODE,
                request.tool_call.get("name"),
                request.tool_call.get("id"),
            )
            return self._build_error_message(request, exc)
        return normalize_tool_result(self._maybe_stamp(result, request))

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest],
            Awaitable[ToolMessage | Command],
        ],
    ) -> ToolMessage | Command:
        try:
            runtime_context = self._runtime_context(request)
            if _is_trusted_read_only_tool(request):
                authorization_method = "before_read_only_tool_call"
            elif _is_trusted_idempotent_tool(request):
                authorization_method = "before_idempotent_tool_call"
            else:
                authorization_method = "before_tool_call"
            await check_authorization_boundary(
                runtime_context,
                authorization_method,
            )
            if is_private_mcp_tool(getattr(request, "tool", None)):
                await check_authorization_boundary(
                    runtime_context,
                    "before_mcp_call",
                )
            result = await handler(request)
        except GraphBubbleUp:
            # Preserve LangGraph control-flow signals (interrupt/pause/resume).
            raise
        except Exception as exc:
            logger.error(
                "Tool execution failed (async): error_code=%s name=%s id=%s",
                TOOL_EXECUTION_FAILED_ERROR_CODE,
                request.tool_call.get("name"),
                request.tool_call.get("id"),
            )
            return self._build_error_message(request, exc)
        return normalize_tool_result(self._maybe_stamp(result, request))


# Compatibility only: production assembly imports the canonical module below.
# Importing ``assembly`` directly remains safe because it imports this concrete
# class lazily from inside ``build_runtime_middlewares`` rather than at module load.
from deerflow.agents.middlewares.assembly import (  # noqa: E402
    assemble_agent_middlewares,
    build_lead_runtime_middlewares,
    build_runtime_middlewares,
    build_sandbox_infrastructure,
    build_subagent_runtime_middlewares,
)

__all__ = [
    "ToolErrorHandlingMiddleware",
    "assemble_agent_middlewares",
    "build_lead_runtime_middlewares",
    "build_runtime_middlewares",
    "build_sandbox_infrastructure",
    "build_subagent_runtime_middlewares",
    "mark_trusted_read_only_tool",
    "mark_trusted_idempotent_tool",
]
