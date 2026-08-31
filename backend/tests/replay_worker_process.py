"""Test-only Worker entry point with the deterministic replay adapter."""

from __future__ import annotations

import os
from functools import wraps
from typing import Any

from _replay_fixture import (
    install_replay_model_adapter,
    replay_fault_barriers_from_environment,
)


def install_replay_knowledge_fast_retry() -> None:
    """Shrink Knowledge task retry delays inside this test process only.

    The production defaults (30s × attempt) are correct for operators but
    would make the browser embedding-failure scenario wait ~90s to exhaust
    the three attempts. The replay Gateway sets the env flag only when it
    enabled the Knowledge module.
    """

    if os.environ.get("ACT_WEAVE_REPLAY_KNOWLEDGE_FAST_RETRY") != "1":
        return

    from actweave_knowledge.tasks.worker import KnowledgeTaskWorker

    original_init = KnowledgeTaskWorker.__init__

    @wraps(original_init)
    def fast_retry_init(self: KnowledgeTaskWorker, **kwargs: Any) -> None:
        kwargs["retry_delay_seconds"] = 1
        kwargs["poll_interval_seconds"] = 0.2
        original_init(self, **kwargs)

    KnowledgeTaskWorker.__init__ = fast_retry_init  # type: ignore[method-assign]


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
    install_replay_knowledge_fast_retry()

    from app.worker.app import main as worker_main

    worker_main()


if __name__ == "__main__":
    main()
