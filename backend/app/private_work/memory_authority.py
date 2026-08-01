from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.private_work.authorization import PrivateRunAuthorizationService
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.run_repository import PrivateRunRepository
from app.projects.capabilities import Capability
from app.projects.context import (
    ProjectContext,
    resolve_project_context_in_transaction,
)
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.persistence.private_work.memory_repository import (
    PrivateMemoryRecord,
    PrivateMemoryRepository,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked

DEFAULT_PRIVATE_MEMORY_NAMESPACE = "default"


class PrivateRunMemoryAuthority:
    """Opaque, Worker-issued read authority for one Run's Memory snapshot."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        context: PrivateWorkContext,
        claim: JobClaim,
        thread_id: str,
        namespace: str,
    ) -> None:
        context = require_issued_private_work_context(context)
        if type(claim) is not JobClaim or claim.run_id is None or claim.scope.project_id != context.project_id or claim.scope.owner_user_id != str(context.user_id):
            raise ValueError("Memory authority claim is invalid")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(namespace, str) or not namespace or namespace.strip() != namespace or len(namespace) > 255:
            raise ValueError("Memory authority coordinates are invalid")
        self._session_factory = session_factory
        self._context = context
        self._claim = claim
        self._thread_id = thread_id
        self._namespace = namespace

    async def load_snapshot(self) -> PrivateMemoryRecord | None:
        """Load without creating a row after one transactional authority check."""

        try:
            async with self._session_factory() as session, session.begin():
                current = await resolve_project_context_in_transaction(
                    session,
                    self._context.user_id,
                    self._context.project_id,
                    self._context.request_id,
                    lock=True,
                )
                if type(current) is not ProjectContext or current.membership_id != self._context.membership_id or current.membership_version != self._context.membership_version:
                    raise AuthorizationRevoked
                current.require(Capability.PRIVATE_WORK_READ_OWN)

                active = await PrivateRunAuthorizationService.is_active(
                    session,
                    project_id=self._context.project_id,
                    owner_user_id=str(self._context.user_id),
                    run_id=self._claim.run_id,
                    lock=False,
                )
                if not active:
                    raise AuthorizationRevoked

                runs = PrivateRunRepository(session)
                cancel_requested = await runs.assert_execution_active(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id,
                    job_id=self._claim.job_id,
                    lease_token=self._claim.lease_token,
                )
                if cancel_requested:
                    raise AuthorizationRevoked
                run = await runs.get(
                    scope=self._context.resource_scope,
                    run_id=self._claim.run_id,
                    lock=False,
                )
                if run is None or run.thread_id != self._thread_id or run.job_id != self._claim.job_id:
                    raise AuthorizationRevoked

                return await PrivateMemoryRepository(session).load(
                    scope=self._context.resource_scope,
                    namespace=self._namespace,
                    lock=True,
                )
        except asyncio.CancelledError:
            raise
        except AuthorizationRevoked:
            raise
        except Exception:
            raise AuthorizationRevoked from None


__all__ = [
    "DEFAULT_PRIVATE_MEMORY_NAMESPACE",
    "PrivateRunMemoryAuthority",
]
