"""Direct, no-tool model-call boundary for first-batch Workflow LLM nodes.

This module intentionally does not construct an Agent, install middleware, read
ambient configuration, or know about Threads.  The Worker materializes the
exact Run model snapshot and injects that model into this narrow boundary.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage

_RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class WorkflowModelCallError(RuntimeError):
    """Base error for the isolated Workflow model-call boundary."""


class WorkflowModelCallCancelled(WorkflowModelCallError):
    """The owning Workflow execution lease is no longer current."""


class WorkflowModelProtocolViolation(WorkflowModelCallError):
    """The injected model violated the first-batch no-tool protocol."""


class _BoundNoToolModel(Protocol):
    async def ainvoke(
        self,
        messages: list[BaseMessage],
        *,
        config: dict[str, object],
    ) -> AIMessage: ...


class WorkflowSnapshotChatModel(Protocol):
    """A model already materialized from one exact Workflow Run snapshot."""

    def bind_tools(self, tools: list[Any]) -> _BoundNoToolModel: ...


def _require_current_lease(lease_is_current: Callable[[], bool]) -> None:
    current = lease_is_current()
    if type(current) is not bool:
        raise WorkflowModelProtocolViolation("lease probe must return a real boolean")
    if not current:
        raise WorkflowModelCallCancelled("Workflow model call lease is no longer current")


async def call_workflow_model_without_tools(
    model: WorkflowSnapshotChatModel,
    *,
    messages: Sequence[BaseMessage],
    run_name: str,
    lease_is_current: Callable[[], bool],
) -> AIMessage:
    """Invoke one exact snapshot model with an explicitly empty tool binding.

    The caller owns timeout, streaming batch persistence, and structured-output
    validation.  This boundary only proves the architectural invariant needed
    by the Phase-0 gate: direct model invocation, ``tools=[]``, lease fencing,
    and fail-closed handling of any tool-call-shaped response.
    """

    if not isinstance(run_name, str) or _RUN_NAME.fullmatch(run_name) is None:
        raise ValueError("run_name must be a bounded stable identifier")
    if not messages or any(not isinstance(message, BaseMessage) for message in messages):
        raise ValueError("messages must be a non-empty sequence of BaseMessage values")
    if not callable(lease_is_current):
        raise TypeError("lease_is_current must be callable")

    _require_current_lease(lease_is_current)
    bound = model.bind_tools([])
    response = await bound.ainvoke(list(messages), config={"run_name": run_name})
    _require_current_lease(lease_is_current)

    if not isinstance(response, AIMessage):
        raise WorkflowModelProtocolViolation("model response must be an AIMessage")
    if response.tool_calls or response.invalid_tool_calls:
        raise WorkflowModelProtocolViolation("Workflow LLM returned tool calls while tools were disabled")
    return response


__all__ = [
    "WorkflowModelCallCancelled",
    "WorkflowModelCallError",
    "WorkflowModelProtocolViolation",
    "WorkflowSnapshotChatModel",
    "call_workflow_model_without_tools",
]
