"""Content-free transactional audit port for Local host execution approval."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import PrivateWorkContext

HostExecutionApprovalTerminalStatus = Literal[
    "finished",
    "launch_failed",
    "unknown",
    "cancelled",
    "expired",
]


class HostExecutionApprovalAuditPort(Protocol):
    """Append lifecycle events in the caller's authoritative transaction.

    The source Run is the only private coordinate accepted by this port. Audit
    persistence irreversibly pseudonymizes it through the existing typed target
    HMAC; approval, Thread, tool-call, command, and filesystem coordinates are
    deliberately absent from the contract.
    """

    async def host_execution_approval_requested(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        source_run_id: str,
        request_id: str | None,
        occurred_at: datetime,
    ) -> None: ...

    async def host_execution_approval_available(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        source_run_id: str,
        request_id: str | None,
        occurred_at: datetime,
    ) -> None: ...

    async def host_execution_approval_decided(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        source_run_id: str,
        decision: Literal["allow_once", "deny"],
        occurred_at: datetime,
    ) -> None: ...

    async def host_execution_approval_claimed(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        source_run_id: str,
        request_id: str | None,
        occurred_at: datetime,
    ) -> None: ...

    async def host_execution_approval_terminal(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        source_run_id: str,
        status: HostExecutionApprovalTerminalStatus,
        request_id: str | None,
        occurred_at: datetime,
    ) -> None: ...


class NoopHostExecutionApprovalAudit:
    async def host_execution_approval_requested(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        source_run_id: str,
        request_id: str | None,
        occurred_at: datetime,
    ) -> None:
        del session, project_id, source_run_id, request_id, occurred_at

    async def host_execution_approval_available(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        source_run_id: str,
        request_id: str | None,
        occurred_at: datetime,
    ) -> None:
        del session, project_id, source_run_id, request_id, occurred_at

    async def host_execution_approval_decided(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        *,
        source_run_id: str,
        decision: Literal["allow_once", "deny"],
        occurred_at: datetime,
    ) -> None:
        del session, context, source_run_id, decision, occurred_at

    async def host_execution_approval_claimed(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        source_run_id: str,
        request_id: str | None,
        occurred_at: datetime,
    ) -> None:
        del session, project_id, source_run_id, request_id, occurred_at

    async def host_execution_approval_terminal(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        source_run_id: str,
        status: HostExecutionApprovalTerminalStatus,
        request_id: str | None,
        occurred_at: datetime,
    ) -> None:
        del session, project_id, source_run_id, status, request_id, occurred_at


__all__ = [
    "HostExecutionApprovalAuditPort",
    "HostExecutionApprovalTerminalStatus",
    "NoopHostExecutionApprovalAudit",
]
