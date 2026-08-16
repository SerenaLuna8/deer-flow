"""Keep Local host-approval tool calls on checkpoint-safe graph boundaries.

LangGraph starts sibling ToolNode handlers concurrently.  A Bash call can stop
the graph for user approval, while a sibling write/delegation has already
started.  In Local approval mode this pre-ToolNode ``after_model`` barrier keeps
only the original first call whenever the batch contains ``bash`` or ``task``.
The next model step replans from that one real tool result.  If that result is
an approval anchor, the ``before_model`` pause gate exits instead, after the
ToolNode checkpoint and before another model invocation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.tool_call_metadata import (
    clone_ai_message_with_tool_calls,
)

_HOST_CAPABLE_TOOL_NAMES = frozenset({"bash", "task"})


def _latest_message_stages_current_run_approval(
    state: AgentState,
    runtime: Runtime,
) -> bool:
    messages = state.get("messages", [])
    if not messages or not isinstance(messages[-1], ToolMessage):
        return False

    context = getattr(runtime, "context", None)
    run_id = context.get("run_id") if isinstance(context, Mapping) else None
    if not isinstance(run_id, str) or not run_id:
        return False

    artifact = messages[-1].artifact
    approval = artifact.get("host_execution_approval") if isinstance(artifact, Mapping) else None
    return bool(
        isinstance(approval, Mapping)
        and approval.get("schema_version") == 1
        and approval.get("kind") == "local_shell"
        and approval.get("source_run_id") == run_id
        and isinstance(approval.get("approval_id"), str)
        and approval.get("approval_id")
        and isinstance(approval.get("source_tool_call_id"), str)
        and approval.get("source_tool_call_id")
    )


class HostExecutionApprovalPauseMiddleware(AgentMiddleware):
    """End the Agent loop after its approval ToolMessage is checkpointed.

    A tool-level ``Command(goto=END)`` does not suppress create_agent's static
    tools-to-model edge.  This before-model gate runs on the following graph
    tick, after the ToolNode tick and its durable checkpoint have completed,
    and uses LangChain's supported ``jump_to`` route to avoid another model
    call.
    """

    @override
    @hook_config(can_jump_to=["end"])
    def before_model(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict | None:
        if _latest_message_stages_current_run_approval(state, runtime):
            return {"jump_to": "end"}
        return None

    @override
    @hook_config(can_jump_to=["end"])
    async def abefore_model(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict | None:
        if _latest_message_stages_current_run_approval(state, runtime):
            return {"jump_to": "end"}
        return None


class HostExecutionBatchBarrierMiddleware(AgentMiddleware):
    """Admit at most the first sibling when a batch may reach host Bash."""

    @staticmethod
    def _serialize_batch(state: AgentState) -> dict | None:
        messages = state.get("messages", [])
        if not messages:
            return None
        message = messages[-1]
        if not isinstance(message, AIMessage):
            return None
        tool_calls = message.tool_calls or []
        if len(tool_calls) <= 1 or not any(call.get("name") in _HOST_CAPABLE_TOOL_NAMES for call in tool_calls):
            return None

        updated = clone_ai_message_with_tool_calls(
            message,
            [tool_calls[0]],
        )
        return {"messages": [updated]}

    @override
    def after_model(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict | None:
        del runtime
        return self._serialize_batch(state)

    @override
    async def aafter_model(
        self,
        state: AgentState,
        runtime: Runtime,
    ) -> dict | None:
        del runtime
        return self._serialize_batch(state)


__all__ = [
    "HostExecutionApprovalPauseMiddleware",
    "HostExecutionBatchBarrierMiddleware",
]
