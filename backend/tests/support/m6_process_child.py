"""Real process entrypoints used by the M6 release-gate tests.

This module deliberately lives under ``tests/support``. It supplies only a
controlled graph runner; production constructs the executor, handler, leasing,
repository access, and shutdown path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from functools import partial
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from app.reliability.execution import RunAgentPrivateExecutor
from app.worker.app import run_worker
from deerflow.persistence import get_session_factory
from deerflow.persistence.jobs.model import JobRow
from deerflow.runtime.runs.schemas import RunStatus


def _append_barrier(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


_SETTLEMENT_TASKS: set[asyncio.Task[None]] = set()


async def _record_production_settlement(job_id: object) -> None:
    """Observe the real handler commit without replacing or wrapping it."""

    factory = get_session_factory()
    while True:
        async with factory() as session:
            status = await session.scalar(select(JobRow.status).where(JobRow.id == job_id))
        if status in {"succeeded", "failed", "cancelled", "dead"}:
            _append_barrier(
                Path(os.environ["M6_PROCESS_BARRIER"]),
                {
                    "event": "settled",
                    "role": "worker",
                    "job_id": str(job_id),
                    "pid": os.getpid(),
                },
            )
            return
        await asyncio.sleep(0.05)


async def _controlled_agent_runner(
    bridge,
    run_manager,
    record,
    *,
    ctx,
    **_kwargs,
) -> None:
    """Pause the graph body inside the real production execution boundary."""

    boundary = ctx.authorization_boundary
    job_id = boundary.execution_job_id
    barrier = Path(os.environ["M6_PROCESS_BARRIER"])
    payload = {
        "role": "worker",
        "job_id": str(job_id),
        "pid": os.getpid(),
        "project_id": str(record.scope.project_id),
        "owner_user_id": record.scope.owner_user_id,
        "run_id": record.run_id,
    }
    await run_manager.set_status(record.run_id, RunStatus.running)
    _append_barrier(barrier, {**payload, "event": "claim"})
    _append_barrier(barrier, {**payload, "event": "leased"})
    _append_barrier(barrier, {**payload, "event": "graph_execution"})

    release = Path(os.environ["M6_PROCESS_RELEASE"])
    while not release.exists():
        await asyncio.sleep(0.05)

    await bridge.publish(
        record.run_id,
        "updates",
        {"worker_pid": os.getpid()},
    )
    _append_barrier(barrier, {**payload, "event": "stream_append"})
    await run_manager.set_status(record.run_id, RunStatus.success)
    await bridge.publish_end(record.run_id)
    _append_barrier(barrier, {**payload, "event": "terminal_append"})

    task = asyncio.create_task(_record_production_settlement(job_id))
    _SETTLEMENT_TASKS.add(task)
    task.add_done_callback(_SETTLEMENT_TASKS.discard)


async def _run_worker_child() -> None:
    controlled_executor = partial(
        RunAgentPrivateExecutor,
        runner=_controlled_agent_runner,
    )
    with patch("app.worker.app.RunAgentPrivateExecutor", controlled_executor):
        await run_worker(handlers=None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("worker",))
    args = parser.parse_args()
    if args.role == "worker":
        asyncio.run(_run_worker_child())


if __name__ == "__main__":
    main()
