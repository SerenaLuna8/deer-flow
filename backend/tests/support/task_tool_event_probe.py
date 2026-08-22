"""Clean-process acceptance probe for the lifecycle-owned task tool."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import time
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.subagents.binding import (
    AgentGraphExecutionInputs,
    ConfiguredLeadParentExecutionProfile,
    ParentExecutionBindingFactory,
)
from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.executor import _SubagentGraphResult, _SubagentGraphStatus
from deerflow.subagents.lifecycle import subagent_task_lifecycle


async def _run_probe() -> dict[str, Any]:
    task_module = importlib.import_module("deerflow.tools.builtins.task_tool")
    events: list[dict[str, Any]] = []

    class ProbeExecutor:
        trace_id = "probe-trace"

        def _create_lifecycle_result_holder(
            self,
            *,
            execution_id,
            changes,
        ) -> _SubagentGraphResult:
            return _SubagentGraphResult(
                execution_id=execution_id,
                trace_id=self.trace_id,
                status=_SubagentGraphStatus.PENDING,
                changes=changes,
            )

        async def _run_lifecycle_graph(
            self,
            _prompt: str,
            result: _SubagentGraphResult,
        ) -> _SubagentGraphResult:
            await asyncio.sleep(0.10)
            assert result.ai_messages is not None
            with result._state_lock:
                result.ai_messages.append({"role": "assistant", "content": "step one"})
            result.changes.notify()
            await asyncio.sleep(0.05)
            with result._state_lock:
                result.ai_messages.append({"role": "assistant", "content": "step two"})
            result.changes.notify()
            await asyncio.sleep(0.05)
            result.try_set_terminal(
                _SubagentGraphStatus.COMPLETED,
                result="probe complete",
                token_usage_records=[
                    {
                        "source_run_id": "probe-source-1",
                        "caller": "subagent:general-purpose",
                        "model_name": "probe-model",
                        "input_tokens": 2,
                        "output_tokens": 3,
                        "total_tokens": 5,
                    }
                ],
            )
            return result

    async def no_tools(**_kwargs: Any) -> list[Any]:
        return []

    config = SubagentConfig(
        name="general-purpose",
        description="probe",
        model="inherit",
        timeout_seconds=2,
    )
    app_config = SimpleNamespace()
    factory = ParentExecutionBindingFactory(
        ConfiguredLeadParentExecutionProfile(
            graph=AgentGraphExecutionInputs(
                model=object(),
                tools=(),
                middleware=(),
                system_prompt=None,
                state_schema=dict,
            ),
            app_config=app_config,
            asset_context=None,
            agent_config=None,
            model_name="probe-model",
            thinking_enabled=False,
            reasoning_effort=None,
            plan_mode=False,
            subagent_enabled=True,
            agent_name="lead",
            available_skills=None,
        )
    )
    task_module.get_available_subagent_names = lambda **_kwargs: ["general-purpose"]
    task_module.get_subagent_config = lambda *_args, **_kwargs: config
    task_module._assemble_subagent_tools = no_tools
    task_module._new_subagent_graph_runner = lambda **_kwargs: ProbeExecutor()
    task_module.get_stream_writer = lambda: events.append

    runtime = SimpleNamespace(
        state={},
        context={
            RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY: factory,
        },
        config={
            "metadata": {},
            "configurable": {},
            "callbacks": [],
        },
        store=None,
    )
    started = time.monotonic()
    command = await task_module.task_tool.coroutine(
        runtime=runtime,
        description="event probe",
        prompt="finish quickly",
        subagent_type="general-purpose",
        tool_call_id="probe-task",
    )
    elapsed = time.monotonic() - started

    assert isinstance(command, Command)
    messages = command.update["messages"]
    assert len(messages) == 1 and isinstance(messages[0], ToolMessage)
    message = messages[0]
    assert message.name == "task"
    assert message.tool_call_id == "probe-task"
    assert message.content == "Task Succeeded. Result: probe complete"
    assert message.additional_kwargs["subagent_status"] == "completed"
    assert message.additional_kwargs["subagent_result_brief"] == "probe complete"
    assert message.additional_kwargs["subagent_result_sha256"] == hashlib.sha256(b"probe complete").hexdigest()
    assert message.additional_kwargs["subagent_model_name"] == "probe-model"
    assert message.additional_kwargs["subagent_token_usage"] == {
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": 5,
    }
    receipt_id = message.additional_kwargs["subagent_usage_receipt_id"]
    assert receipt_id != message.tool_call_id

    event_types = [event["type"] for event in events]
    assert event_types == [
        "task_started",
        "task_running",
        "task_running",
        "task_completed",
    ]
    assert [event["message_index"] for event in events if event["type"] == "task_running"] == [1, 2]
    assert [event["message"]["content"] for event in events if event["type"] == "task_running"] == ["step one", "step two"]
    await subagent_task_lifecycle.aclose()
    return {
        "elapsed": elapsed,
        "event_types": event_types,
        "tool_status": message.additional_kwargs["subagent_status"],
        "usage_receipt_is_internal": receipt_id != message.tool_call_id,
    }


def main() -> int:
    result = asyncio.run(_run_probe())
    if result["elapsed"] >= 1.0:
        raise AssertionError(f"200ms subtask paid polling tail: {result['elapsed']:.3f}s")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
