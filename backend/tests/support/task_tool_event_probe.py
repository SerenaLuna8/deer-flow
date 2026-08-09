"""Clean-process acceptance probe for the event-driven ``task`` tool.

The main pytest process installs a lightweight executor module before test
collection to break an unrelated import cycle.  This probe runs in a fresh
Python process so it exercises the production ``SubagentResult`` and the full
``task`` tool coroutine, including progress forwarding, terminal wake-up,
``ToolMessage`` construction, and registry cleanup.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.executor import (
    SubagentResult,
    SubagentStatus,
    _background_tasks,
    _background_tasks_lock,
    cleanup_background_task,
    get_background_task_result,
)


async def _run_probe() -> dict[str, Any]:
    task_module = importlib.import_module("deerflow.tools.builtins.task_tool")
    events: list[dict[str, Any]] = []

    class ProbeExecutor:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def execute_async(self, _prompt: str, task_id: str | None = None) -> str:
            assert task_id is not None
            result = SubagentResult(
                task_id=task_id,
                trace_id="probe-trace",
                status=SubagentStatus.RUNNING,
            )
            with _background_tasks_lock:
                _background_tasks[task_id] = result

            def finish() -> None:
                time.sleep(0.10)
                assert result.ai_messages is not None
                result.ai_messages.append({"role": "assistant", "content": "step one"})
                result.changes.notify()
                # This second progress notification is deliberately inside the
                # one-second debounce window.  The terminal notification must
                # still drain it exactly once instead of losing or duplicating it.
                time.sleep(0.05)
                result.ai_messages.append({"role": "assistant", "content": "step two"})
                result.changes.notify()
                time.sleep(0.05)
                result.try_set_terminal(
                    SubagentStatus.COMPLETED,
                    result="probe complete",
                    token_usage_records=[
                        {
                            "model": "probe-model",
                            "input_tokens": 2,
                            "output_tokens": 3,
                            "total_tokens": 5,
                        }
                    ],
                )

            threading.Thread(target=finish, daemon=True).start()
            return task_id

    async def no_tools(**_kwargs: Any) -> list[Any]:
        return []

    config = SubagentConfig(
        name="general-purpose",
        description="probe",
        model="probe-model",
        timeout_seconds=2,
    )
    task_module.SubagentExecutor = ProbeExecutor
    task_module.SubagentStatus = SubagentStatus
    task_module.get_background_task_result = get_background_task_result
    task_module.cleanup_background_task = cleanup_background_task
    task_module.get_available_subagent_names = lambda **_kwargs: ["general-purpose"]
    task_module.get_subagent_config = lambda *_args, **_kwargs: config
    task_module._assemble_subagent_tools = no_tools
    task_module._token_usage_cache_enabled = lambda _app_config: False
    task_module.get_stream_writer = lambda: events.append

    runtime = SimpleNamespace(
        state={},
        context={},
        config={"metadata": {"model_name": "probe-model"}},
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
    event_types = [event["type"] for event in events]
    assert event_types == [
        "task_started",
        "task_running",
        "task_running",
        "task_completed",
    ]
    assert [event["message_index"] for event in events if event["type"] == "task_running"] == [1, 2]
    assert [event["message"]["content"] for event in events if event["type"] == "task_running"] == [
        "step one",
        "step two",
    ]
    assert get_background_task_result("probe-task") is None
    return {
        "elapsed": elapsed,
        "event_types": event_types,
        "tool_status": message.additional_kwargs["subagent_status"],
    }


def main() -> int:
    result = asyncio.run(_run_probe())
    if result["elapsed"] >= 1.0:
        raise AssertionError(f"200ms subtask paid polling tail: {result['elapsed']:.3f}s")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
