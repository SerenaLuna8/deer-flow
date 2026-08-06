"""Worker-issued authority for one Run's frozen Memory document."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from app.personalization.repository import AccountPersonalizationRepository
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
from deerflow.agents.memory.dream import validate_memory_document
from deerflow.config.memory_config import MemoryConfig
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.persistence.private_work.memory_document_model import (
    RunMemoryContextSnapshotRow,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked

DEFAULT_PRIVATE_MEMORY_NAMESPACE = "default"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class PrivateRunMemorySnapshot:
    """The complete, immutable Memory payload frozen by Run admission."""

    document_version: int
    content: str
    content_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.document_version) is not int
            or self.document_version < 1
            or not isinstance(self.content, str)
            or not self.content
            or len(self.content) > 16_000
            or not isinstance(self.content_digest, str)
            or _SHA256_HEX.fullmatch(self.content_digest) is None
            or hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.content_digest
        ):
            raise ValueError("Run Memory snapshot is invalid")


class PrivateRunMemoryAuthority:
    """Read one frozen snapshot after revalidating live Run authority.

    The object is created only by Worker composition and is passed through the
    opaque runtime context. It never reads the current Memory document: Dream
    revisions that happen after admission therefore cannot change a Run retry.
    The account preference is checked at each model boundary so disable/reset
    takes effect without weakening the frozen snapshot contract.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        context: PrivateWorkContext,
        claim: JobClaim,
        thread_id: str,
        namespace: str,
        memory_config: MemoryConfig,
        personalization_repository_builder=AccountPersonalizationRepository,
        run_repository_builder=PrivateRunRepository,
    ) -> None:
        context = require_issued_private_work_context(context)
        if type(claim) is not JobClaim or claim.job_type not in {"private_run", "automation_run"} or claim.run_id is None or claim.scope.project_id != context.project_id or claim.scope.owner_user_id != str(context.user_id):
            raise ValueError("Memory authority claim is invalid")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(namespace, str) or not namespace or namespace.strip() != namespace or len(namespace) > 255:
            raise ValueError("Memory authority coordinates are invalid")
        if not isinstance(memory_config, MemoryConfig):
            raise ValueError("Memory authority configuration is invalid")
        if not callable(personalization_repository_builder) or not callable(run_repository_builder):
            raise ValueError("Memory authority repositories are invalid")
        self._session_factory = session_factory
        self._context = context
        self._claim = claim
        self._thread_id = thread_id
        self._namespace = namespace
        self._memory_config = memory_config
        self._personalization_repository_builder = personalization_repository_builder
        self._run_repository_builder = run_repository_builder

    async def load_snapshot(self) -> PrivateRunMemorySnapshot | None:
        """Return the admitted row, or no data when disabled/reset."""

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

                runs = self._run_repository_builder(session)
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

                if not self._memory_config.enabled:
                    return None
                preference = await self._personalization_repository_builder(session).read_memory(str(self._context.user_id))
                if not preference.memory_enabled:
                    return None

                row = (
                    await session.execute(
                        sa.select(RunMemoryContextSnapshotRow).where(
                            RunMemoryContextSnapshotRow.project_id == self._context.project_id,
                            RunMemoryContextSnapshotRow.owner_user_id == str(self._context.user_id),
                            RunMemoryContextSnapshotRow.run_id == self._claim.run_id,
                            RunMemoryContextSnapshotRow.namespace == self._namespace,
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                validate_memory_document(
                    row.content,
                    self._memory_config.max_injection_tokens,
                )
                return PrivateRunMemorySnapshot(
                    document_version=int(row.document_version),
                    content=row.content,
                    content_digest=row.content_digest,
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
    "PrivateRunMemorySnapshot",
]
