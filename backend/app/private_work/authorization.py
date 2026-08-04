from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.errors import PrivateWorkError, PrivateWorkUnavailable
from app.projects.models import ProjectRole
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.run.model import RunRow
from deerflow.sandbox.sandbox import AuthorizationRevoked

AUTHORIZATION_REVOKED_REASON = "authorization_revoked"
_EXECUTABLE_ROLES = (
    ProjectRole.ADMIN.value,
    ProjectRole.EDITOR.value,
    ProjectRole.RUNNER.value,
    ProjectRole.CHANNEL_GUEST.value,
)


class PrivateRequestAuthorizationBoundary:
    """Adapt request-scoped revalidation to an app-agnostic model boundary.

    The checker owns one short database transaction and must return before the
    external model call begins.  Harness code understands only the internal
    ``AuthorizationRevoked`` control signal, so this adapter retains the
    private HTTP-facing error for the owning request service to restore.
    """

    def __init__(
        self,
        checker: Callable[[], Awaitable[None]],
        *,
        request_id: str,
    ) -> None:
        self._checker = checker
        self._request_id = request_id
        self._private_error: PrivateWorkError | None = None

    async def before_model_call(self) -> None:
        try:
            await self._checker()
        except asyncio.CancelledError:
            raise
        except PrivateWorkError as error:
            self._private_error = error
            raise AuthorizationRevoked from None
        except AuthorizationRevoked:
            if self._private_error is None:
                self._private_error = PrivateWorkUnavailable(self._request_id)
            raise
        except Exception:
            self._private_error = PrivateWorkUnavailable(self._request_id)
            raise AuthorizationRevoked from None

    def private_error(self) -> PrivateWorkError:
        """Return the stable private error recorded by the latest check."""

        return self._private_error or PrivateWorkUnavailable(self._request_id)


class PrivateRunAuthorizationService:
    """Trusted authorization marker and run-bound revalidation operations."""

    @staticmethod
    async def mark_revoked(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        reason: str = AUTHORIZATION_REVOKED_REASON,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Lock then mark live runs in the caller-owned governance transaction."""

        requested_at = now or datetime.now(UTC)
        run_ids = tuple(
            (
                await session.execute(
                    select(RunRow.run_id)
                    .where(
                        RunRow.project_id == project_id,
                        RunRow.owner_user_id == owner_user_id,
                        RunRow.status.in_(("pending", "running")),
                        RunRow.authorization_cancel_requested_at.is_(None),
                    )
                    .order_by(RunRow.created_at, RunRow.run_id)
                    .with_for_update(of=RunRow)
                )
            )
            .scalars()
            .all()
        )
        if not run_ids:
            return ()
        await session.execute(
            update(RunRow)
            .where(
                RunRow.project_id == project_id,
                RunRow.owner_user_id == owner_user_id,
                RunRow.run_id.in_(run_ids),
                RunRow.status.in_(("pending", "running")),
                RunRow.authorization_cancel_requested_at.is_(None),
            )
            .values(
                authorization_cancel_requested_at=requested_at,
                authorization_cancel_reason=reason,
                updated_at=requested_at,
            )
        )
        return run_ids

    @staticmethod
    async def is_active(
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
        lock: bool = False,
    ) -> bool:
        """Revalidate a running scope without pinning its admission-time version."""

        statement = (
            select(RunRow.run_id)
            .join(ProjectRow, ProjectRow.id == RunRow.project_id)
            .join(
                ProjectMembershipRow,
                (ProjectMembershipRow.project_id == RunRow.project_id) & (ProjectMembershipRow.user_id == RunRow.owner_user_id),
            )
            .where(
                RunRow.run_id == run_id,
                RunRow.project_id == project_id,
                RunRow.owner_user_id == owner_user_id,
                RunRow.status.in_(("pending", "running")),
                RunRow.authorization_cancel_requested_at.is_(None),
                RunRow.authorization_cancel_reason.is_(None),
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.role.in_(_EXECUTABLE_ROLES),
            )
            .limit(1)
        )
        if lock:
            statement = statement.with_for_update(of=RunRow)
        return (await session.execute(statement)).scalar_one_or_none() is not None


class PrivateRunAuthorizationBoundary:
    """Fail-closed boundary shared by every run-time side-effect surface."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
        abort_event: asyncio.Event | None = None,
        on_revoke: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._project_id = project_id
        self._owner_user_id = owner_user_id
        self._run_id = run_id
        self._abort_event = abort_event
        self._on_revoke = on_revoke

    def bind_abort_event(self, abort_event: asyncio.Event) -> None:
        """Attach the process-local abort only after the run is registered."""

        if self._abort_event is abort_event:
            return
        if self._abort_event is not None:
            raise RuntimeError("authorization boundary abort event is already bound")
        self._abort_event = abort_event

    async def _check(self) -> None:
        try:
            async with self._session_factory() as session:
                active = await PrivateRunAuthorizationService.is_active(
                    session,
                    project_id=self._project_id,
                    owner_user_id=self._owner_user_id,
                    run_id=self._run_id,
                )
        except AuthorizationRevoked:
            raise
        except Exception:
            await self._revoke_locally()
            raise AuthorizationRevoked from None
        if not active:
            await self._revoke_locally()
            raise AuthorizationRevoked from None

    async def _revoke_locally(self) -> None:
        if self._abort_event is not None:
            self._abort_event.set()
        if self._on_revoke is not None:
            result = self._on_revoke()
            if inspect.isawaitable(result):
                await result

    async def before_model_call(self) -> None:
        await self._check()

    async def before_tool_call(self) -> None:
        await self._check()

    async def before_mcp_call(self) -> None:
        await self._check()

    async def before_sandbox_write(self) -> None:
        await self._check()

    async def before_sandbox_exec(self) -> None:
        await self._check()

    async def before_checkpoint_read(self) -> None:
        await self._check()

    async def before_checkpoint_write(self) -> None:
        await self._check()

    async def before_file_finalization(self) -> None:
        """Reserved for Task 8 finalization; no Task 7 file authority lives here."""

        await self._check()
