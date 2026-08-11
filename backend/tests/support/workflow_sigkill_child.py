"""Controlled child process for the real G02 pre-commit SIGKILL probe."""

from __future__ import annotations

import asyncio
import os
import sys

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from deerflow.workflows.compiler import StructuredLoopPlan, WorkflowCompilerCache
from deerflow.workflows.runtime import WorkflowLoopRunner, WorkflowLoopRuntimeContext

LOOP_ID = "00000000-0000-4000-8000-000000000102"
BODY_ID = "00000000-0000-4000-8000-000000000103"


async def _hang_before_commit(value: int, _identity: object) -> int:
    print("BODY_ENTERED", flush=True)
    await asyncio.Event().wait()
    return value + 1


async def _main(run_id: str) -> None:
    database_url = os.environ["WORKFLOW_TEST_DATABASE_URL"]
    plan = StructuredLoopPlan(
        graph_schema_version=1,
        compiler_contract_version=1,
        semantic_checksum="1" * 64,
        loop_node_id=LOOP_ID,
        body_node_id=BODY_ID,
        max_iterations=5,
    )
    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        await saver.setup()
        runner = WorkflowLoopRunner.compile(
            plan,
            checkpointer=saver,
            checkpoint_mode="full",
            cache=WorkflowCompilerCache(),
        )
        await runner.run(
            run_id=run_id,
            initial_value=0,
            context=WorkflowLoopRuntimeContext(
                body_step=_hang_before_commit,
                until=lambda value, _iteration: value >= 3,
            ),
        )


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1]))
