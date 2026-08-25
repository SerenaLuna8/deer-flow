"""Test-only Worker entry point with the deterministic replay adapter."""

from __future__ import annotations

from functools import wraps
from typing import Any

from _replay_fixture import (
    install_replay_model_adapter,
    replay_fault_barriers_from_environment,
)


def install_replay_worker_fault_controls() -> None:
    """Wrap production seams only inside this standalone test process."""

    barriers = replay_fault_barriers_from_environment()
    if barriers is None:
        return

    from app.private_work.run_repository import PrivateRunRepository
    from app.worker.service import WorkerService

    original_fill_capacity = WorkerService._fill_capacity
    original_begin_execution = PrivateRunRepository.begin_execution

    @wraps(original_fill_capacity)
    async def fill_capacity_with_claim_barrier(
        service: WorkerService,
        stop_event=None,
    ) -> bool:
        released = await barriers.wait_async(
            "claim",
            stop_event=stop_event,
        )
        if not released:
            return False
        return await original_fill_capacity(service, stop_event)

    @wraps(original_begin_execution)
    async def begin_execution_with_barrier(
        repository: PrivateRunRepository,
        *args: Any,
        **kwargs: Any,
    ):
        await barriers.wait_async("begin_execution")
        return await original_begin_execution(
            repository,
            *args,
            **kwargs,
        )

    WorkerService._fill_capacity = fill_capacity_with_claim_barrier
    PrivateRunRepository.begin_execution = begin_execution_with_barrier


def main() -> None:
    install_replay_model_adapter()
    install_replay_worker_fault_controls()

    from app.worker.app import main as worker_main

    worker_main()


if __name__ == "__main__":
    main()
