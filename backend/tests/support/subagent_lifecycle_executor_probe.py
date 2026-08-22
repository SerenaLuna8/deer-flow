"""Clean-process probe for the production graph-runner lifecycle adapter."""

from __future__ import annotations

import asyncio
import json
from types import MethodType

from deerflow.subagents.executor import (
    _SubagentGraphResult,
    _SubagentGraphRunner,
    _SubagentGraphStatus,
)
from deerflow.subagents.lifecycle import (
    NO_INHERITED_OPERATIONS,
    SubagentCompleted,
    SubagentExecutionBinding,
    SubagentQuiescencePolicy,
    SubagentTaskCall,
    SubagentTaskLifecycle,
    _ProcessSubagentScheduler,
)


async def _run() -> dict[str, object]:
    runner = object.__new__(_SubagentGraphRunner)
    runner.trace_id = "production-adapter-trace"

    async def graph(
        self: _SubagentGraphRunner,
        prompt: str,
        holder: _SubagentGraphResult,
    ) -> _SubagentGraphResult:
        del self
        holder.try_set_terminal(
            _SubagentGraphStatus.COMPLETED,
            result=prompt,
            token_usage_records=[
                {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            ],
        )
        return holder

    runner._aexecute = MethodType(graph, runner)  # type: ignore[method-assign]
    lifecycle = SubagentTaskLifecycle(
        _scheduler=_ProcessSubagentScheduler(max_concurrency=1),
    )
    try:
        outcome = await lifecycle.run(
            SubagentTaskCall(
                task_id="external-correlation",
                prompt="production graph adapter",
                queue_timeout_seconds=1,
                execution_timeout_seconds=1,
            ),
            SubagentExecutionBinding(
                runner_factory=lambda: runner,
                quiescence_policy=SubagentQuiescencePolicy.REQUIRED_BEFORE_RETURN,
                inherited_operations_barrier=NO_INHERITED_OPERATIONS,
            ),
        )
        assert isinstance(outcome, SubagentCompleted)
        return {
            "outcome_type": type(outcome).__name__,
            "result": outcome.result,
            "quiescent": outcome.quiescent,
            "total_tokens": outcome.usage.total_tokens if outcome.usage else None,
            "execution_id_is_internal": str(outcome.execution_id) != outcome.task_id,
        }
    finally:
        await lifecycle.aclose()


print(json.dumps(asyncio.run(_run())))
