"""Serialize model tool batches that can cross Local host approval.

LangGraph starts sibling ToolNode handlers concurrently.  A Bash call can stop
the graph for user approval, while a sibling write/delegation has already
started.  In Local approval mode this pre-ToolNode ``after_model`` barrier keeps
only the original first call whenever the batch contains ``bash`` or ``task``.
The next model step replans from that one real tool result.
"""

from __future__ import annotations

from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.tool_call_metadata import (
    clone_ai_message_with_tool_calls,
)

_HOST_CAPABLE_TOOL_NAMES = frozenset({"bash", "task"})


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


__all__ = ["HostExecutionBatchBarrierMiddleware"]
