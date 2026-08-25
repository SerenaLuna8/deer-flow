"""Lease and authorization state owned by one private Run attempt."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.authorization import PrivateRunAuthorizationBoundary
from app.private_work.context import PrivateWorkContext
from app.private_work.revalidation import PrivateWorkRevalidator
from app.private_work.run_repository import (
    PrivateRunExecutionLeaseLost,
    PrivateRunRepository,
    PrivateRunVisionDispatchRateLimited,
)
from app.private_work.run_skill_tree_materializer import (
    MaterializationAttemptIdentity,
    RuntimeOwnedMaterializedRunSkillTree,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.run_execution.ports import (
    NoopPrivateRunAgentQuota,
    PrivateRunAgentQuotaPort,
)
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.runtime.events.models import StreamLeaseProof
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.sandbox.sandbox import (
    AuthorizationBoundaryFenceUncertain,
    AuthorizationRevoked,
)
from deerflow.vision.dispatch import VisionDispatchDenied


class PrivateRunExecutionBoundary:
    """Combine member authorization with the current job/run lease proof."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        context: PrivateWorkContext,
        claim: JobClaim,
        expected_worker_id: uuid.UUID | None = None,
        quota: PrivateRunAgentQuotaPort | None = None,
        runtime_kind: Literal["chat", "skill_builder"] = "chat",
    ) -> None:
        if claim.run_id is None:
            raise ValueError("private execution claim requires a Run")
        if expected_worker_id is not None and type(expected_worker_id) is not uuid.UUID:
            raise TypeError("expected_worker_id must be a UUID")
        self._factory = session_factory
        self._context = context
        self._claim = claim
        self._expected_worker_id = expected_worker_id
        self._quota = quota or NoopPrivateRunAgentQuota()
        self._runtime_kind = runtime_kind
        self._revalidator = PrivateWorkRevalidator()
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
        self._side_effect_fence_lock = asyncio.Lock()
        self._side_effect_fence_generation = 0
        self._unresolved_side_effect_fences: set[int] = set()

    @property
    def execution_job_id(self) -> uuid.UUID:
        return self._claim.job_id

    @property
    def expected_worker_id(self) -> uuid.UUID | None:
        return self._expected_worker_id

    @property
    def attempt_id(self) -> uuid.UUID:
        return self._claim.attempt_id

    async def lock_and_assert_materialization_active_in_session(
        self,
        session: AsyncSession,
        locked_context: ProjectContext,
    ) -> MaterializationAttemptIdentity:
        """Lock only Job, Run, and exact Attempt after a governance prefix."""

        expected_worker_id = self._expected_worker_id
        try:
            if (
                type(locked_context) is not ProjectContext
                or locked_context.user_id != self._context.user_id
                or locked_context.project_id != self._context.project_id
                or locked_context.membership_id != self._context.membership_id
                or locked_context.membership_version != self._context.membership_version
                or expected_worker_id is None
            ):
                raise PrivateRunExecutionLeaseLost
            identity = MaterializationAttemptIdentity(
                job_id=uuid.UUID(str(self._claim.job_id)),
                attempt_id=uuid.UUID(str(self._claim.attempt_id)),
                worker_id=uuid.UUID(str(expected_worker_id)),
            )
            cancel_requested = await PrivateRunRepository(
                session,
            ).assert_materialization_attempt_active(
                scope=PrivateResourceScope(
                    project_id=str(locked_context.project_id),
                    owner_user_id=str(locked_context.user_id),
                    membership_version=locked_context.membership_version,
                ),
                run_id=self._claim.run_id or "",
                job_id=identity.job_id,
                attempt_id=identity.attempt_id,
                expected_worker_id=identity.worker_id,
                lease_token=self._claim.lease_token,
            )
            if cancel_requested:
                self.request_local_cancel()
                raise AuthorizationRevoked
            return identity
        except asyncio.CancelledError:
            raise
        except AuthorizationRevoked:
            raise
        except PrivateRunExecutionLeaseLost:
            self._lease_lost = True
            if self._abort_event is not None:
                self._abort_event.set()
            raise AuthorizationRevoked from None
        except Exception:
            self._lease_lost = True
            if self._abort_event is not None:
                self._abort_event.set()
            raise AuthorizationRevoked from None

    async def before_run_readonly_mount_acquire(
        self,
        tree: RuntimeOwnedMaterializedRunSkillTree,
    ) -> None:
        """Transaction A: exact authority plus durable acquiring metadata."""

        if type(tree) is not RuntimeOwnedMaterializedRunSkillTree:
            raise TypeError("Runtime-owned materialized Skill tree is required")
        await self._materialization_fence(tree.persist_mount_acquiring)

    async def after_run_readonly_mount_acquire(
        self,
        tree: RuntimeOwnedMaterializedRunSkillTree,
        lease,
    ) -> None:
        """Transaction B: exact authority plus durable mounted lease identity."""

        if type(tree) is not RuntimeOwnedMaterializedRunSkillTree:
            raise TypeError("Runtime-owned materialized Skill tree is required")

        async def persist() -> None:
            await tree.persist_mount_mounted(lease)

        await self._materialization_fence(persist)

    async def _materialization_fence(
        self,
        persist=None,
    ) -> None:
        """Hold governance and execution locks across one metadata fsync."""

        capabilities = (
            (
                Capability.SHARED_ASSETS_READ,
                Capability.SHARED_ASSETS_EDIT,
            )
            if self._runtime_kind == "skill_builder"
            else (
                Capability.PRIVATE_WORK_CREATE,
                Capability.SHARED_ASSETS_EXECUTE,
            )
        )
        try:
            async with self._factory() as session, session.begin():
                locked_context = await self._revalidator.require(
                    session,
                    self._context,
                    *capabilities,
                    lock_mode="share",
                )
                await self.lock_and_assert_materialization_active_in_session(
                    session,
                    locked_context,
                )
                if persist is not None:
                    await persist()
        except asyncio.CancelledError:
            raise
        except AuthorizationRevoked:
            raise
        except PrivateRunExecutionLeaseLost:
            self._lease_lost = True
            if self._abort_event is not None:
                self._abort_event.set()
            raise AuthorizationRevoked from None
        except Exception:
            self._lease_lost = True
            if self._abort_event is not None:
                self._abort_event.set()
            raise AuthorizationRevoked from None

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
        return bool(self._unresolved_side_effect_fences)

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
        vision_dispatch_resources: tuple[int, int] | None = None,
    ) -> int | None:
        if ambiguous_side_effect:
            async with self._side_effect_fence_lock:
                return await self._check_once(
                    authorization_method,
                    ambiguous_side_effect=True,
                    allow_cancel=allow_cancel,
                    vision_dispatch_resources=vision_dispatch_resources,
                )
        return await self._check_once(
            authorization_method,
            ambiguous_side_effect=False,
            allow_cancel=allow_cancel,
            vision_dispatch_resources=vision_dispatch_resources,
        )

    async def _check_once(
        self,
        authorization_method: str,
        *,
        ambiguous_side_effect: bool,
        allow_cancel: bool,
        vision_dispatch_resources: tuple[int, int] | None,
    ) -> int | None:
        fence: int | None = None
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
        if ambiguous_side_effect:
            # Register the local fence before the transaction commit await. If
            # cancellation or a DB transport failure loses the commit ACK, the
            # Worker must remain fail-closed even though no token can be
            # returned to the caller. Domain failures that prove rollback
            # remove this tentative fence below.
            self._side_effect_fence_generation += 1
            fence = self._side_effect_fence_generation
            self._unresolved_side_effect_fences.add(fence)
        try:
            async with self._factory() as session, session.begin():
                repository = PrivateRunRepository(session)
                if vision_dispatch_resources is not None:
                    cancel_requested = await repository.reserve_vision_dispatch_attempt(
                        scope=self._context.resource_scope,
                        run_id=self._claim.run_id or "",
                        job_id=self._claim.job_id,
                        lease_token=self._claim.lease_token,
                        normalized_bytes=vision_dispatch_resources[0],
                        normalized_pixels=vision_dispatch_resources[1],
                    )
                elif ambiguous_side_effect:
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
        except PrivateRunVisionDispatchRateLimited:
            if fence is not None:
                self._unresolved_side_effect_fences.remove(fence)
            raise
        except PrivateRunExecutionLeaseLost:
            if fence is not None:
                self._unresolved_side_effect_fences.remove(fence)
            self._lease_lost = True
            if self._abort_event is not None:
                self._abort_event.set()
            raise AuthorizationRevoked from None
        except Exception:
            self._lease_lost = True
            if self._abort_event is not None:
                self._abort_event.set()
            if fence is not None:
                raise AuthorizationBoundaryFenceUncertain(fence) from None
            raise AuthorizationRevoked from None
        if cancel_requested:
            self.request_local_cancel()
            if not allow_cancel:
                raise AuthorizationRevoked
        return fence

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

    async def before_vision_dispatch(
        self,
        *,
        normalized_bytes: int,
        normalized_pixels: int,
    ) -> None:
        """Fence one non-idempotent third-party model request."""

        try:
            await self._check(
                "before_model_call",
                ambiguous_side_effect=True,
                vision_dispatch_resources=(
                    normalized_bytes,
                    normalized_pixels,
                ),
            )
        except PrivateRunVisionDispatchRateLimited:
            # A hard resource limit is a normal pre-dispatch denial. It does
            # not imply lease loss, cancellation, or an ambiguous side effect.
            raise VisionDispatchDenied("VISION_RATE_LIMITED") from None

    async def after_vision_dispatch(self) -> None:
        """Require current authority before exposing provider evidence."""

        await self._check("before_model_call")

    async def before_sandbox_write(self) -> None:
        await self._check(
            "before_sandbox_write",
            ambiguous_side_effect=True,
        )

    async def before_sandbox_exec(self) -> int:
        fence = await self._check(
            "before_sandbox_exec",
            ambiguous_side_effect=True,
        )
        if fence is None:  # pragma: no cover - guarded by the call above
            raise RuntimeError("sandbox execution fence was not created")
        return fence

    def resolve_sandbox_exec_fence(self, fence: object) -> None:
        """Resolve only the exact host-spawn ambiguity proven by a receipt.

        Other tool, MCP, write, or execution fences remain in the set.  The
        opaque value is returned by :meth:`before_sandbox_exec` directly to
        trusted Worker code and is never accepted from model or browser data.
        """

        if type(fence) is not int or fence not in self._unresolved_side_effect_fences:
            raise RuntimeError("sandbox execution fence is unavailable")
        self._unresolved_side_effect_fences.remove(fence)

    @asynccontextmanager
    async def resolve_sandbox_exec_fence_transaction(
        self,
        fence: object,
    ) -> AsyncIterator[bool]:
        """Serialize receipt commit with every ambiguous boundary mutation.

        The yielded boolean is true only when the host-spawn fence is the sole
        unresolved local fence.  The app port uses it inside the receipt
        transaction to decide whether the durable Job may return to ``safe``.
        The exact fence is removed only after that transaction returns.
        """

        async with self._side_effect_fence_lock:
            if type(fence) is not int or fence not in self._unresolved_side_effect_fences:
                raise RuntimeError("sandbox execution fence is unavailable")
            retry_safe = self._unresolved_side_effect_fences == {fence}
            try:
                yield retry_safe
            except BaseException:
                raise
            else:
                self._unresolved_side_effect_fences.remove(fence)

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
        # File finalization is a Run-idempotent PostgreSQL workflow: staging
        # rows use server IDs, quota reservations key by those IDs, promotion
        # is transactional, and active Artifacts are reused by Run+file+path.
        await self._check("before_file_finalization")

    async def before_file_finalization_in_session(
        self,
        session: AsyncSession,
    ) -> None:
        """Validate file/Run mutation authority in its owning transaction."""

        try:
            cancel_requested = await PrivateRunRepository(
                session,
            ).assert_execution_active(
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


__all__ = ["PrivateRunExecutionBoundary"]
