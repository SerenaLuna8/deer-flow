"""Lease and authorization state owned by one private Run attempt."""

from __future__ import annotations

import asyncio
import uuid
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.authorization import PrivateRunAuthorizationBoundary
from app.private_work.context import PrivateWorkContext
from app.private_work.run_repository import PrivateRunRepository
from app.projects.models import ProjectRole
from app.reliability.run_execution.ports import (
    NoopPrivateRunAgentQuota,
    PrivateRunAgentQuotaPort,
)
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.runtime.events.models import StreamLeaseProof
from deerflow.sandbox.sandbox import AuthorizationRevoked


class PrivateRunExecutionBoundary:
    """Combine member authorization with the current job/run lease proof."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        context: PrivateWorkContext,
        claim: JobClaim,
        quota: PrivateRunAgentQuotaPort | None = None,
        runtime_kind: Literal["chat", "skill_builder"] = "chat",
    ) -> None:
        if claim.run_id is None:
            raise ValueError("private execution claim requires a Run")
        self._factory = session_factory
        self._context = context
        self._claim = claim
        self._quota = quota or NoopPrivateRunAgentQuota()
        self._runtime_kind = runtime_kind
        executable_roles = (
            (ProjectRole.ADMIN.value, ProjectRole.EDITOR.value)
            if runtime_kind == "skill_builder"
            else (
                ProjectRole.ADMIN.value,
                ProjectRole.EDITOR.value,
                ProjectRole.RUNNER.value,
                ProjectRole.CHANNEL_GUEST.value,
            )
        )
        self._authorization = PrivateRunAuthorizationBoundary(
            session_factory,
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            run_id=claim.run_id,
            executable_roles=executable_roles,
        )
        self._abort_event: asyncio.Event | None = None
        self._lease_lost = False
        self._authorization_revoked = False
        self._cancel_requested = False
        self._ambiguous_side_effect = False

    @property
    def execution_job_id(self) -> uuid.UUID:
        return self._claim.job_id

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost

    @property
    def authorization_revoked(self) -> bool:
        return self._authorization_revoked

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    @property
    def ambiguous_side_effect(self) -> bool:
        return self._ambiguous_side_effect

    def bind_abort_event(self, abort_event: asyncio.Event) -> None:
        if self._abort_event is not None and self._abort_event is not abort_event:
            raise RuntimeError("execution boundary abort event is already bound")
        self._abort_event = abort_event
        self._authorization.bind_abort_event(abort_event)

    def request_local_cancel(self) -> None:
        self._cancel_requested = True
        if self._abort_event is not None:
            self._abort_event.set()

    def stream_lease_proof(self) -> StreamLeaseProof:
        return StreamLeaseProof(
            job_id=self._claim.job_id,
            lease_token=self._claim.lease_token,
        )

    def record_stream_lease_lost(self) -> None:
        self._lease_lost = True
        if self._abort_event is not None:
            self._abort_event.set()

    def record_stream_authorization_revoked(self) -> None:
        self._authorization_revoked = True
        if self._abort_event is not None:
            self._abort_event.set()

    async def _check(
        self,
        authorization_method: str,
        *,
        ambiguous_side_effect: bool = False,
        allow_cancel: bool = False,
    ) -> None:
        try:
            await getattr(
                self._authorization,
                authorization_method,
            )()
        except AuthorizationRevoked:
            self._authorization_revoked = True
            if self._abort_event is not None:
                self._abort_event.set()
            raise
        try:
            async with self._factory() as session, session.begin():
                repository = PrivateRunRepository(session)
                if ambiguous_side_effect:
                    cancel_requested = await repository.mark_execution_side_effect_unknown(
                        scope=self._context.resource_scope,
                        run_id=self._claim.run_id or "",
                        job_id=self._claim.job_id,
                        lease_token=self._claim.lease_token,
                    )
                else:
                    cancel_requested = await repository.assert_execution_active(
                        scope=self._context.resource_scope,
                        run_id=self._claim.run_id or "",
                        job_id=self._claim.job_id,
                        lease_token=self._claim.lease_token,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lease_lost = True
            if self._abort_event is not None:
                self._abort_event.set()
            raise AuthorizationRevoked from None
        if cancel_requested:
            self.request_local_cancel()
            if not allow_cancel:
                raise AuthorizationRevoked
        if ambiguous_side_effect:
            self._ambiguous_side_effect = True

    async def before_model_call(self) -> None:
        await self._check("before_model_call")

    async def before_tool_call(self) -> None:
        await self._check(
            "before_tool_call",
            ambiguous_side_effect=True,
        )

    async def before_read_only_tool_call(self) -> None:
        await self._check("before_tool_call")

    async def before_idempotent_tool_call(self) -> None:
        await self._check("before_tool_call")

    async def before_deferred_dispatch_tool_call(self) -> None:
        # The exact external-call authority owns the durable ambiguity fence.
        await self._check("before_tool_call")

    async def before_mcp_call(self) -> None:
        # Discovery/materialization is read-only. The exact remote dispatch
        # hook owns both quota consumption and the retry-safety fence.
        await self._check("before_mcp_call")

    async def before_mcp_tool_dispatch(self) -> None:
        await self._check("before_mcp_call")
        await self._quota.consume_mcp_dispatch(
            self._context,
            dispatch_id=uuid.uuid4(),
        )
        # Quota rejection happens before this durable unknown-side-effect
        # marker, so it remains a stable retry-safe public failure.
        await self._check(
            "before_mcp_call",
            ambiguous_side_effect=True,
        )

    async def before_vision_dispatch(self) -> None:
        """Fence one non-idempotent third-party model request."""

        await self._check(
            "before_model_call",
            ambiguous_side_effect=True,
        )

    async def after_vision_dispatch(self) -> None:
        """Require current authority before exposing provider evidence."""

        await self._check("before_model_call")

    async def before_sandbox_write(self) -> None:
        await self._check(
            "before_sandbox_write",
            ambiguous_side_effect=True,
        )

    async def before_sandbox_exec(self) -> None:
        await self._check(
            "before_sandbox_exec",
            ambiguous_side_effect=True,
        )

    async def before_sandbox_restore(self) -> None:
        # Restoring a deterministic snapshot into an ephemeral sandbox is
        # retry-safe but still requires current durable execution authority.
        await self._check("before_sandbox_write")

    async def before_checkpoint_read(self) -> None:
        await self._check("before_checkpoint_read")

    async def before_checkpoint_write(self) -> None:
        await self._check("before_checkpoint_write")

    async def before_stream_publish(self) -> None:
        await self._check("before_checkpoint_write")

    async def before_stream_terminal(self) -> None:
        await self._check(
            "before_checkpoint_write",
            allow_cancel=True,
        )

    async def stream_cleanup_allowed(self) -> bool:
        try:
            async with self._factory() as session, session.begin():
                return await PrivateRunRepository(session).stream_cleanup_allowed(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id or "",
                    job_id=self._claim.job_id,
                )
        except Exception:
            return False

    async def before_file_finalization(self) -> None:
        await self._check(
            "before_file_finalization",
            ambiguous_side_effect=True,
        )

    async def before_file_finalization_in_session(
        self,
        session: AsyncSession,
    ) -> None:
        """Validate file/Run mutation authority in its owning transaction."""

        try:
            cancel_requested = await PrivateRunRepository(
                session,
            ).mark_execution_side_effect_unknown(
                scope=self._context.resource_scope,
                run_id=self._claim.run_id or "",
                job_id=self._claim.job_id,
                lease_token=self._claim.lease_token,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lease_lost = True
            if self._abort_event is not None:
                self._abort_event.set()
            raise AuthorizationRevoked from None
        if cancel_requested:
            self.request_local_cancel()
            raise AuthorizationRevoked
        self._ambiguous_side_effect = True


__all__ = ["PrivateRunExecutionBoundary"]
