"""Dependency ports for private Run execution orchestration."""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import PrivateWorkContext
from app.reliability.run_execution.contracts import (
    AgentExecutionResult,
    PrivateRunExecution,
)
from app.system_runtime_settings.models import MaterializedAgentRuntimePolicy
from app.worker.service import JobLeaseAuthority
from deerflow.config.model_config import ModelConfig
from deerflow.persistence.jobs.sql import JobTerminalEvent
from deerflow.runtime.private_scope import PrivateResourceScope


class SystemModelMaterializationPort(Protocol):
    """Materialize the exact secret-bearing model frozen for one Run."""

    async def materialize_snapshot(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
        purpose: str,
    ) -> ModelConfig: ...


class SystemRuntimePolicyMaterializationPort(Protocol):
    """Materialize the exact global runtime policy frozen for one Run."""

    async def materialize_run_snapshot_envelope(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
    ) -> MaterializedAgentRuntimePolicy: ...


class PrivateRunExecutor(Protocol):
    async def execute(
        self,
        execution: PrivateRunExecution,
        authority: JobLeaseAuthority,
    ) -> AgentExecutionResult: ...


class PrivateRunExecutionQuotaPort(Protocol):
    async def release_concurrent_run(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        request_id: str,
    ) -> None: ...


class PrivateRunExecutionAuditPort(Protocol):
    async def run_terminal(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        job_type: str,
        status: str,
        public_error_code: str | None,
        request_id: str,
    ) -> None: ...

    async def job_terminalized(
        self,
        session: AsyncSession,
        event: JobTerminalEvent,
    ) -> None: ...


class NoopPrivateRunExecutionQuota:
    async def release_concurrent_run(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        request_id: str,
    ) -> None:
        del session, scope, run_id, request_id


class NoopPrivateRunExecutionAudit:
    async def run_terminal(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        run_id: str,
        job_id: uuid.UUID,
        job_type: str,
        status: str,
        public_error_code: str | None,
        request_id: str,
    ) -> None:
        del (
            session,
            scope,
            run_id,
            job_id,
            job_type,
            status,
            public_error_code,
            request_id,
        )

    async def job_terminalized(
        self,
        session: AsyncSession,
        event: JobTerminalEvent,
    ) -> None:
        del session, event


class PrivateRunAgentQuotaPort(Protocol):
    async def consume_mcp_dispatch(
        self,
        context: PrivateWorkContext,
        *,
        dispatch_id: uuid.UUID,
    ) -> None: ...

    async def reserve_file(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        file_id: uuid.UUID,
        size: int,
    ) -> None: ...

    async def release_file(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        file_id: uuid.UUID,
        size: int,
        request_id: str,
    ) -> None: ...


class NoopPrivateRunAgentQuota:
    async def consume_mcp_dispatch(
        self,
        context: PrivateWorkContext,
        *,
        dispatch_id: uuid.UUID,
    ) -> None:
        del context, dispatch_id

    async def reserve_file(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        file_id: uuid.UUID,
        size: int,
    ) -> None:
        del session, context, file_id, size

    async def release_file(
        self,
        session: AsyncSession,
        scope: PrivateResourceScope,
        *,
        file_id: uuid.UUID,
        size: int,
        request_id: str,
    ) -> None:
        del session, scope, file_id, size, request_id


__all__ = [
    "NoopPrivateRunAgentQuota",
    "NoopPrivateRunExecutionAudit",
    "NoopPrivateRunExecutionQuota",
    "PrivateRunAgentQuotaPort",
    "PrivateRunExecutionAuditPort",
    "PrivateRunExecutionQuotaPort",
    "PrivateRunExecutor",
    "SystemModelMaterializationPort",
    "SystemRuntimePolicyMaterializationPort",
]
