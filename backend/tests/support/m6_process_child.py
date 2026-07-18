"""Real process entrypoints used by the M6 release-gate tests.

This module deliberately lives under ``tests/support``.  It supplies only
coordination handlers; process construction, leasing, repository access and
shutdown remain the production Worker/Gateway/Scheduler paths.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from functools import partial
from pathlib import Path

from app.audit.service import AuditService, _bind_worker_audit_process
from app.audit.sinks import OperationalAuditSink
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.reliability.execution import (
    AgentExecutionResult,
    LeaseAuthorizedStreamBridge,
    PrivateRunExecutionBoundary,
    PrivateRunJobHandler,
    PrivateRunJobTerminalPort,
)
from app.reliability.owner_refs import AuditHmacKeyring
from app.worker.app import run_worker
from app.worker.service import JobSettlement
from deerflow.config import get_app_config
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence import get_session_factory
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.runtime.stream_bridge.postgres import PostgresStreamBridge


def _append_barrier(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class _CoordinatedExecutor:
    """Pause execution while all durable authority stays production-owned."""

    def __init__(self, factory) -> None:
        self._factory = factory

    async def execute(self, execution, authority) -> AgentExecutionResult:
        claim = authority.claim
        barrier = Path(os.environ["M6_PROCESS_BARRIER"])
        release = Path(os.environ["M6_PROCESS_RELEASE"])
        _append_barrier(
            barrier,
            {
                "event": "leased",
                "job_id": str(claim.job_id),
                "pid": os.getpid(),
                "project_id": str(claim.scope.project_id),
                "owner_user_id": claim.scope.owner_user_id,
            },
        )
        while not release.exists():
            await asyncio.sleep(0.05)

        boundary = PrivateRunExecutionBoundary(
            self._factory,
            context=execution.context,
            claim=claim,
        )
        bridge = LeaseAuthorizedStreamBridge(
            PostgresStreamBridge(self._factory),
            boundary,
            scope=execution.context.resource_scope,
            thread_id=execution.run.thread_id,
            terminal_status=lambda: "success",
        )
        await bridge.publish(
            execution.run.run_id,
            "updates",
            {"worker_pid": os.getpid()},
        )
        await bridge.publish_end(execution.run.run_id)
        return AgentExecutionResult.succeeded()


class _CoordinatedPrivateRunHandler:
    """Lazily wrap the production handler after ``run_worker`` initializes."""

    def __init__(self) -> None:
        self._handler: PrivateRunJobHandler | None = None

    def _production_handler(self) -> PrivateRunJobHandler:
        if self._handler is not None:
            return self._handler
        factory = get_session_factory()
        keyring = AuditHmacKeyring.from_environment()
        audit_service = AuditService(factory, keyring)
        audit = OperationalAuditSink(
            audit_service,
            process_context=_bind_worker_audit_process(audit_service),
        )
        config = get_app_config()
        quota = ProjectQuotaEnforcer(
            QuotaService(
                factory,
                getattr(config, "quotas", None) or QuotaConfig(),
                source_ref_hasher=keyring,
            )
        )
        terminal_port = PrivateRunJobTerminalPort(quota=quota, audit=audit)
        repository_builder = partial(
            JobRepository,
            owner_ref_hasher=keyring.job_owner_ref,
            terminal_port=terminal_port,
        )
        self._handler = PrivateRunJobHandler(
            factory,
            executor=_CoordinatedExecutor(factory),
            retry_initial_seconds=config.worker.retry_initial_seconds,
            retry_max_seconds=config.worker.retry_max_seconds,
            job_repository_builder=repository_builder,
            quota=quota,
            audit=audit,
        )
        return self._handler

    async def __call__(self, claim, authority) -> JobSettlement:
        settlement = await self._production_handler()(claim, authority)

        async def commit() -> None:
            await settlement.commit()
            _append_barrier(
                Path(os.environ["M6_PROCESS_BARRIER"]),
                {
                    "event": "settled",
                    "job_id": str(claim.job_id),
                    "pid": os.getpid(),
                },
            )

        return JobSettlement(settlement.outcome, commit)


async def _run_worker_child() -> None:
    await run_worker(handlers={"private_run": _CoordinatedPrivateRunHandler()})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("worker",))
    args = parser.parse_args()
    if args.role == "worker":
        asyncio.run(_run_worker_child())


if __name__ == "__main__":
    main()
