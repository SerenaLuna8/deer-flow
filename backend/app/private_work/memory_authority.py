"""Worker-issued authority for one Run's frozen Memory document."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn

import sqlalchemy as sa

from app.personalization.repository import (
    AccountPersonalizationNotFound,
    AccountPersonalizationRepository,
)
from app.private_work.authorization import PrivateRunAuthorizationService
from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.run_repository import (
    PrivateRunExecutionLeaseLost,
    PrivateRunRepository,
)
from app.private_work.thread_repository import PrivateThreadRepository
from app.projects.capabilities import Capability
from app.projects.context import (
    ProjectContext,
    resolve_project_context_in_transaction,
)
from app.projects.errors import ProjectForbidden, ProjectNotFound
from deerflow.config.memory_config import MemoryConfig
from deerflow.error_codes import MemoryAuthorityUnavailable
from deerflow.memory_contract import (
    validate_memory_document,
    validate_memory_document_sections,
)
from deerflow.persistence.jobs.sql import JobClaim
from deerflow.persistence.private_work.memory_document_model import (
    RunMemoryContextSnapshotRow,
)
from deerflow.persistence.private_work.memory_document_repository import (
    EPISODE_SEARCH_TAGS,
    MAX_EPISODE_QUERY_CHARS,
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryEpisodeRecord,
    MemoryProposalOutcome,
    MemoryRememberProposal,
)
from deerflow.sandbox.sandbox import AuthorizationRevoked

DEFAULT_PRIVATE_MEMORY_NAMESPACE = "default"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
logger = logging.getLogger(__name__)

_AUTHORIZATION_FAILURES = (
    AccountPersonalizationNotFound,
    PrivateRunExecutionLeaseLost,
    ProjectForbidden,
    ProjectNotFound,
)
_AUTHORITY_OPERATIONS = frozenset({"load_snapshot", "propose_entry", "search_episodes"})


def _raise_authority_unavailable(
    operation: str,
    error: Exception,
) -> NoReturn:
    """Fail the Run with a content-free operational observation."""

    safe_operation = operation if operation in _AUTHORITY_OPERATIONS else "unknown"
    logger.error(
        "Memory authority operation failed: operation=%s disposition=fail_closed failure_type=%s",
        safe_operation,
        type(error).__name__,
    )
    # ``raise ... from None`` suppresses display of the active exception but
    # Python still stores it in ``__context__``.  Clear that hidden chain too,
    # so a later tracer cannot recover SQL parameters or private identifiers.
    try:
        raise MemoryAuthorityUnavailable from None
    except MemoryAuthorityUnavailable as signal:
        signal.__cause__ = None
        signal.__context__ = None
        raise


def _recall_result_bucket(count: int) -> str:
    """Bucket a recall result count into the closed audit vocabulary."""

    if count <= 0:
        return "0"
    if count <= 2:
        return "1-2"
    return "3+"


def _recall_query_len_bucket(query: str) -> str:
    """Bucket a normalized query's Unicode code-point length for quality audit."""

    if not isinstance(query, str) or not 1 <= len(query) <= MAX_EPISODE_QUERY_CHARS:
        raise ValueError("Episode query is out of contract")
    length = len(query)
    if length <= 4:
        return "1-4"
    if length <= 16:
        return "5-16"
    if length <= 64:
        return "17-64"
    return "65-200"


def _recall_matched_stage(query: str, episodes: tuple[MemoryEpisodeRecord, ...]) -> str:
    """Report which ranking stage produced the top hit, without content.

    Mirrors the repository ranking: an exact case-insensitive substring hit
    outranks trigram similarity, so the top record decides the stage.
    """

    if not episodes:
        return "none"
    top = episodes[0]
    if query.lower() in top.tagged_text.lower():
        return "exact"
    return "similarity"


@dataclass(frozen=True, slots=True)
class PrivateRunMemorySnapshot:
    """The complete, immutable Memory payload frozen by Run admission."""

    document_version: int
    content: str
    content_digest: str
    sections: tuple[str, ...]

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
        object.__setattr__(
            self,
            "sections",
            validate_memory_document_sections(self.sections),
        )


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
        thread_repository_builder=PrivateThreadRepository,
        audit=None,
    ) -> None:
        context = require_issued_private_work_context(context)
        if type(claim) is not JobClaim or claim.job_type not in {"private_run", "automation_run"} or claim.run_id is None or claim.scope.project_id != context.project_id or claim.scope.owner_user_id != str(context.user_id):
            raise ValueError("Memory authority claim is invalid")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(namespace, str) or not namespace or namespace.strip() != namespace or len(namespace) > 255:
            raise ValueError("Memory authority coordinates are invalid")
        if not isinstance(memory_config, MemoryConfig):
            raise ValueError("Memory authority configuration is invalid")
        if not callable(personalization_repository_builder) or not callable(run_repository_builder) or not callable(thread_repository_builder):
            raise ValueError("Memory authority repositories are invalid")
        if audit is not None and (not callable(getattr(audit, "memory_remembered", None)) or not callable(getattr(audit, "memory_recall_executed", None))):
            raise ValueError("Memory authority audit port is invalid")
        self._session_factory = session_factory
        self._audit = audit
        self._context = context
        self._claim = claim
        self._thread_id = thread_id
        self._namespace = namespace
        self._memory_config = memory_config
        self._personalization_repository_builder = personalization_repository_builder
        self._run_repository_builder = run_repository_builder
        self._thread_repository_builder = thread_repository_builder

    async def _require_live_run(self, session) -> None:
        """Revalidate membership, capability, Run, Job, lease, and Thread."""

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

        # The global Memory lock order requires the exact live Thread after the
        # Project/Membership locks and before the Run Job/lease rows.  The
        # repository's active-scope predicate excludes frozen and deleted
        # threads; the explicit checks keep an injected repository fail-closed.
        thread = await self._thread_repository_builder(session).get(
            scope=self._context.resource_scope,
            thread_id=self._thread_id,
            lock=True,
        )
        if (
            thread is None
            or thread.thread_id != self._thread_id
            or str(thread.project_id) != self._context.resource_scope.project_id
            or thread.owner_user_id != self._context.resource_scope.owner_user_id
            or thread.frozen_at is not None
            or thread.deleted_at is not None
        ):
            raise AuthorizationRevoked

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

    async def _memory_readable(self, session) -> bool:
        """Live platform switch plus the live account preference."""

        if not self._memory_config.enabled:
            return False
        preference = await self._personalization_repository_builder(session).read_memory(str(self._context.user_id))
        return bool(preference.memory_enabled)

    async def load_snapshot(self) -> PrivateRunMemorySnapshot | None:
        """Return the admitted row, or no data when disabled/reset."""

        try:
            async with self._session_factory() as session, session.begin():
                await self._require_live_run(session)
                if not await self._memory_readable(session):
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
                    sections=row.sections,
                )
                return PrivateRunMemorySnapshot(
                    document_version=int(row.document_version),
                    content=row.content,
                    content_digest=row.content_digest,
                    sections=tuple(row.sections),
                )
        except asyncio.CancelledError:
            raise
        except AuthorizationRevoked:
            raise
        except _AUTHORIZATION_FAILURES:
            raise AuthorizationRevoked from None
        except MemoryAuthorityUnavailable:
            raise
        except Exception as error:
            _raise_authority_unavailable("load_snapshot", error)

    async def search_episodes(
        self,
        *,
        query: str,
        tags: tuple[str, ...] = (),
        limit: int = 5,
    ) -> tuple[MemoryEpisodeRecord, ...] | None:
        """Ranked recall over the archived episodes of this exact scope.

        Coordinates always come from the authority itself; the model only ever
        supplies ``query``/``tags``/``limit``. ``None`` reports Memory disabled,
        mirroring :meth:`load_snapshot`.
        """

        if not isinstance(query, str):
            raise ValueError("Episode query is out of contract")
        normalized_query = query.strip()
        if not normalized_query or len(normalized_query) > MAX_EPISODE_QUERY_CHARS:
            raise ValueError("Episode query is out of contract")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("Episode limit is out of contract")
        normalized_tags = tuple(tags)
        if any(tag not in EPISODE_SEARCH_TAGS for tag in normalized_tags):
            raise ValueError("Episode tag is out of contract")

        try:
            async with self._session_factory() as session, session.begin():
                await self._require_live_run(session)
                if not await self._memory_readable(session):
                    return None
                repository = MemoryDocumentRepository(session)
                episodes = await repository.search_episodes(
                    MemoryDocumentScope(
                        project_id=self._context.project_id,
                        owner_user_id=str(self._context.user_id),
                        namespace=self._namespace,
                    ),
                    query=normalized_query,
                    tags=normalized_tags,
                    limit=limit,
                    retention_days=self._memory_config.episode_retention_days,
                    now=datetime.now(UTC),
                )
                # The content-free quality event shares the search transaction
                # so an aborted Run never leaves an orphan recall record.
                if self._audit is not None:
                    await self._audit.memory_recall_executed(
                        session,
                        self._context.resource_scope,
                        run_id=self._claim.run_id,
                        job_id=self._claim.job_id,
                        request_id=self._context.request_id,
                        result_bucket=_recall_result_bucket(len(episodes)),
                        matched_stage=_recall_matched_stage(
                            normalized_query,
                            episodes,
                        ),
                        tags_filtered=bool(normalized_tags),
                        query_len_bucket=_recall_query_len_bucket(normalized_query),
                    )
                return episodes
        except asyncio.CancelledError:
            raise
        except AuthorizationRevoked:
            raise
        except _AUTHORIZATION_FAILURES:
            raise AuthorizationRevoked from None
        except MemoryAuthorityUnavailable:
            raise
        except Exception as error:
            _raise_authority_unavailable("search_episodes", error)

    async def propose_entry(
        self,
        *,
        kind: str,
        content: str,
        tool_call_id: str,
    ) -> MemoryProposalOutcome:
        """Record one pending memory line for the next Dream, idempotently.

        The proposal is validated before any database work so hostile
        arguments surface as ``ValueError`` rather than a revoked Run. The
        write, both caps, and the ``memory.remember`` audit event share one
        transaction: an aborted Run never leaves a half-recorded proposal.
        """

        proposal = MemoryRememberProposal(
            scope=MemoryDocumentScope(
                project_id=self._context.project_id,
                owner_user_id=str(self._context.user_id),
                namespace=self._namespace,
            ),
            thread_id=self._thread_id,
            run_id=self._claim.run_id,
            tool_call_id=tool_call_id,
            kind=kind,
            content=content,
        )
        if not self._memory_config.enabled:
            return MemoryProposalOutcome(
                disposition="memory_disabled",
                entry_id=None,
                tagged_text=None,
            )

        try:
            async with self._session_factory() as session, session.begin():
                await self._require_live_run(session)
                outcome = await MemoryDocumentRepository(session).propose_entry(proposal)
                if outcome.disposition == "recorded" and self._audit is not None:
                    await self._audit.memory_remembered(
                        session,
                        self._context.resource_scope,
                        run_id=self._claim.run_id,
                        job_id=self._claim.job_id,
                        request_id=self._context.request_id,
                        kind=proposal.kind,
                    )
                return outcome
        except asyncio.CancelledError:
            raise
        except AuthorizationRevoked:
            raise
        except _AUTHORIZATION_FAILURES:
            raise AuthorizationRevoked from None
        except MemoryAuthorityUnavailable:
            raise
        except Exception as error:
            _raise_authority_unavailable("propose_entry", error)


__all__ = [
    "DEFAULT_PRIVATE_MEMORY_NAMESPACE",
    "PrivateRunMemoryAuthority",
    "PrivateRunMemorySnapshot",
]
