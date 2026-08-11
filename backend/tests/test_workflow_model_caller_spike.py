from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from deerflow.workflows.model_call import (
    WorkflowModelCallCancelled,
    WorkflowModelProtocolViolation,
    call_workflow_model_without_tools,
)


@dataclass
class _BoundFakeModel:
    response: AIMessage
    calls: list[tuple[str, object]]

    async def ainvoke(self, messages: list[object], *, config: dict[str, object]) -> AIMessage:
        self.calls.append(("ainvoke", (tuple(messages), dict(config))))
        return self.response


@dataclass
class _FakeModel:
    response: AIMessage
    calls: list[tuple[str, object]] = field(default_factory=list)

    def bind_tools(self, tools: list[object]) -> _BoundFakeModel:
        self.calls.append(("bind_tools", list(tools)))
        return _BoundFakeModel(response=self.response, calls=self.calls)


@pytest.mark.asyncio
async def test_workflow_model_caller_binds_exactly_zero_tools_and_invokes_directly() -> None:
    model = _FakeModel(
        AIMessage(
            content="done",
            usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        )
    )
    lease_checks = iter((True, True))

    result = await call_workflow_model_without_tools(
        model,
        messages=(SystemMessage(content="system"), HumanMessage(content="input")),
        run_name="workflow_llm_node",
        lease_is_current=lambda: next(lease_checks),
    )

    assert result is model.response
    assert model.calls[0] == ("bind_tools", [])
    assert model.calls[1][0] == "ainvoke"
    invoked_messages, config = model.calls[1][1]
    assert invoked_messages == (
        SystemMessage(content="system"),
        HumanMessage(content="input"),
    )
    assert config == {"run_name": "workflow_llm_node"}


@pytest.mark.asyncio
async def test_workflow_model_caller_rejects_tool_or_invalid_tool_output() -> None:
    for response in (
        AIMessage(
            content="",
            tool_calls=[{"name": "forbidden", "args": {}, "id": "call-1"}],
        ),
        AIMessage(
            content="",
            invalid_tool_calls=[{"name": "forbidden", "args": "{}", "id": "call-2", "error": "bad"}],
        ),
    ):
        with pytest.raises(WorkflowModelProtocolViolation, match="tool calls"):
            await call_workflow_model_without_tools(
                _FakeModel(response),
                messages=(HumanMessage(content="input"),),
                run_name="workflow_llm_node",
                lease_is_current=lambda: True,
            )


@pytest.mark.asyncio
async def test_workflow_model_caller_checks_lease_before_and_after_call() -> None:
    before = _FakeModel(AIMessage(content="unused"))
    with pytest.raises(WorkflowModelCallCancelled):
        await call_workflow_model_without_tools(
            before,
            messages=(HumanMessage(content="input"),),
            run_name="workflow_llm_node",
            lease_is_current=lambda: False,
        )
    assert before.calls == []

    after = _FakeModel(AIMessage(content="discarded"))
    checks = iter((True, False))
    with pytest.raises(WorkflowModelCallCancelled):
        await call_workflow_model_without_tools(
            after,
            messages=(HumanMessage(content="input"),),
            run_name="workflow_llm_node",
            lease_is_current=lambda: next(checks),
        )
    assert after.calls[0] == ("bind_tools", [])
