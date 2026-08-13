"""Compatibility facade for owner-private Memory persistence."""

# Contract imports below are intentional compatibility re-exports.
# ruff: noqa: F401

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.memory_contract import (
    BUDGET_REWRITE_HISTORY_DIGEST,
    DEFAULT_EPISODE_RETENTION_DAYS,
    DEFAULT_MEMORY_NAMESPACE,
    EPISODE_SEARCH_TAGS,
    EPISODE_SIMILARITY_FLOOR,
    MAX_EPISODE_QUERY_CHARS,
    MAX_MEMORY_UNIFIED_DIFF_CHARS,
    MAX_REMEMBER_CONTENT_CHARS,
    MEMORY_REVIEW_DELETION_RATIO,
    MEMORY_REVIEW_MIN_LINES,
    REMEMBER_BACKLOG_LIMIT,
    REMEMBER_PROMPT_VERSION,
    REMEMBER_RUN_LIMIT,
    MemoryBudgetRewriteScanCursor,
    MemoryBudgetRewriteScopePage,
    MemoryDocumentConflict,
    MemoryDocumentNotFound,
    MemoryDocumentRecord,
    MemoryDocumentScope,
    MemoryDocumentState,
    MemoryDocumentVersionRecord,
    MemoryDreamAdmissionDisposition,
    MemoryDreamAdmissionKind,
    MemoryDreamAdmissionRecord,
    MemoryDreamFrozenRuntime,
    MemoryDreamHistoryRecord,
    MemoryDreamLeaseConflict,
    MemoryDreamReleaseDisposition,
    MemoryDreamReleaseResult,
    MemoryDreamSettlementInvariant,
    MemoryDreamStaleConflict,
    MemoryDreamTrigger,
    MemoryDreamWork,
    MemoryEpisodeCursorInvalid,
    MemoryEpisodePage,
    MemoryEpisodeRecord,
    MemoryHistoryActivation,
    MemoryHistoryActivationResult,
    MemoryHistoryActivationStatus,
    MemoryPendingEntryRecord,
    MemoryProposalDisposition,
    MemoryProposalOutcome,
    MemoryRememberProposal,
    MemoryResetCounts,
    MemoryResetSettledDream,
    compute_dream_history_digest,
    compute_remember_source_digest,
    decode_memory_episode_cursor,
    encode_memory_episode_cursor,
    escape_like_pattern,
    memory_document_deletion_ratio,
    memory_document_diff_preview,
    memory_document_digest,
    memory_document_needs_review,
    memory_document_unified_diff,
    scope_predicates,
    validate_episode_retention_days,
    validated_episode_tags,
)
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import JobRepository, JobScope
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    MemoryDocumentVersionRow,
    MemoryDreamPrepareRunRow,
    MemoryDreamRunRow,
    MemoryEpisodeRow,
    MemoryHistoryEntryRow,
    RunMemoryContextSnapshotRow,
)
from deerflow.persistence.private_work.memory_dream_store import (
    DREAM_HISTORY_BATCH_SIZE,
    TOOL_ENTRY_DUE_MINUTES,
    MemoryDreamStore,
)
from deerflow.persistence.private_work.memory_repository_parts import (
    MemoryDocumentStore,
    MemoryEpisodeReader,
    MemoryHistoryRepository,
    document_record,
    frozen_document_sections,
    version_record,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow

_escape_like_pattern = escape_like_pattern
_validated_episode_tags = validated_episode_tags


class MemoryDocumentRepository:
    """Legacy facade sharing one caller-owned session across focused stores."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        jobs: JobRepository | None = None,
    ) -> None:
        self.session = session
        self.jobs = jobs or JobRepository(session)
        self.documents = MemoryDocumentStore(session)
        self.episodes = MemoryEpisodeReader(session)
        self.history = MemoryHistoryRepository(session)
        self.dreams = MemoryDreamStore(
            session,
            jobs=self.jobs,
            documents=self.documents,
        )

    @staticmethod
    def _scope_predicates(
        row_type,
        scope: MemoryDocumentScope,
    ) -> tuple[sa.ColumnElement[bool], ...]:
        return scope_predicates(row_type, scope)

    @staticmethod
    def _frozen_document_sections(
        row: MemoryDocumentRow,
    ) -> tuple[tuple[str, ...], uuid.UUID]:
        return frozen_document_sections(row)

    @staticmethod
    def _document_record(row: MemoryDocumentRow | None) -> MemoryDocumentRecord:
        return document_record(row)

    async def activate_history(
        self,
        activation: MemoryHistoryActivation,
    ) -> MemoryHistoryActivationResult:
        return await self.history.activate(activation)

    async def propose_entry(
        self,
        proposal: MemoryRememberProposal,
    ) -> MemoryProposalOutcome:
        return await self.history.propose(proposal)

    async def read_state(
        self,
        scope: MemoryDocumentScope,
        *,
        for_update: bool = False,
    ) -> MemoryDocumentState:
        return await self.documents.read_state(scope, for_update=for_update)

    async def list_pending_entries(
        self,
        scope: MemoryDocumentScope,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MemoryPendingEntryRecord, ...]:
        return await self.documents.list_pending_entries(
            scope,
            limit=limit,
            offset=offset,
        )

    async def list_versions(
        self,
        scope: MemoryDocumentScope,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MemoryDocumentVersionRecord, ...]:
        return await self.documents.list_versions(
            scope,
            limit=limit,
            offset=offset,
        )

    async def read_version(
        self,
        scope: MemoryDocumentScope,
        version: int,
    ) -> MemoryDocumentVersionRecord:
        return await self.documents.read_version(scope, version)

    async def restore_version(
        self,
        scope: MemoryDocumentScope,
        *,
        target_version: int,
        expected_current_version: int,
        expected_sections: tuple[str, ...],
        max_tokens: int,
        now: datetime,
    ) -> MemoryDocumentVersionRecord:
        return await self.documents.restore_version(
            scope,
            target_version=target_version,
            expected_current_version=expected_current_version,
            expected_sections=expected_sections,
            max_tokens=max_tokens,
            now=now,
        )

    async def list_due_scopes(
        self,
        *,
        now: datetime,
        interval_minutes: int,
        limit: int = 100,
    ) -> tuple[MemoryDocumentScope, ...]:
        return await self.dreams.list_due_scopes(
            now=now,
            interval_minutes=interval_minutes,
            limit=limit,
        )

    async def list_budget_rewrite_scope_page(
        self,
        *,
        budget_tokens: int,
        admissible_roles: tuple[str, ...],
        cursor: MemoryBudgetRewriteScanCursor | None = None,
        limit: int = 100,
    ) -> MemoryBudgetRewriteScopePage:
        return await self.dreams.list_budget_rewrite_scope_page(
            budget_tokens=budget_tokens,
            admissible_roles=admissible_roles,
            cursor=cursor,
            limit=limit,
        )

    async def is_scope_due(
        self,
        scope: MemoryDocumentScope,
        *,
        now: datetime,
        interval_minutes: int,
    ) -> bool:
        return await self.dreams.is_scope_due(
            scope,
            now=now,
            interval_minutes=interval_minutes,
        )

    async def admit_dream(
        self,
        scope: MemoryDocumentScope,
        *,
        trigger: MemoryDreamTrigger,
        frozen: MemoryDreamFrozenRuntime,
        initial_content: str | None,
        initial_sections: tuple[str, ...] | None,
        sections_policy_version_id: uuid.UUID | None,
        now: datetime,
        max_attempts: int = 3,
    ) -> MemoryDreamAdmissionRecord:
        return await self.dreams.admit_dream(
            scope,
            trigger=trigger,
            frozen=frozen,
            initial_content=initial_content,
            initial_sections=initial_sections,
            sections_policy_version_id=sections_policy_version_id,
            now=now,
            max_attempts=max_attempts,
        )

    async def load_dream_work(
        self,
        scope: MemoryDocumentScope,
        job_id: uuid.UUID,
    ) -> MemoryDreamWork | None:
        return await self.dreams.load_dream_work(scope, job_id)

    async def finalize_dream(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        expected_history_digest: str,
        expected_base_version: int,
        expected_base_digest: str,
        expected_sections: tuple[str, ...],
        content: str,
        now: datetime,
        episode_retention_days: int = DEFAULT_EPISODE_RETENTION_DAYS,
    ) -> MemoryDocumentVersionRecord:
        return await self.dreams.finalize_dream(
            scope,
            job_id=job_id,
            lease_token=lease_token,
            expected_history_digest=expected_history_digest,
            expected_base_version=expected_base_version,
            expected_base_digest=expected_base_digest,
            expected_sections=expected_sections,
            content=content,
            now=now,
            episode_retention_days=episode_retention_days,
        )

    async def release_dream(
        self,
        scope: MemoryDocumentScope,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        now: datetime,
        cancelled: bool,
        public_error_code: str = "MEMORY_DREAM_FAILED",
        retryable: bool = True,
        retry_initial_seconds: int = 5,
        retry_max_seconds: int = 300,
    ) -> MemoryDreamReleaseResult:
        return await self.dreams.release_dream(
            scope,
            job_id=job_id,
            lease_token=lease_token,
            now=now,
            cancelled=cancelled,
            public_error_code=public_error_code,
            retryable=retryable,
            retry_initial_seconds=retry_initial_seconds,
            retry_max_seconds=retry_max_seconds,
        )

    @staticmethod
    def _version_record(
        row: MemoryDocumentVersionRow,
    ) -> MemoryDocumentVersionRecord:
        return version_record(row)

    async def lock_owner_projects(
        self,
        owner_user_id: str,
    ) -> tuple[uuid.UUID, ...]:
        """Lock every project authority that can admit owner-private Memory."""

        try:
            owner_user_id = str(uuid.UUID(str(owner_user_id)))
        except (TypeError, ValueError):
            raise ValueError("Memory reset requires an owner UUID") from None
        authority_projects = sa.union(
            sa.select(ProjectMembershipRow.project_id.label("project_id")).where(ProjectMembershipRow.user_id == owner_user_id),
            sa.select(JobRow.project_id.label("project_id")).where(
                JobRow.owner_user_id == owner_user_id,
                JobRow.job_type.in_(("memory_dream", "memory_dream_prepare", "memory_seal")),
                JobRow.status.in_(("queued", "leased", "running", "retry_wait")),
            ),
        ).subquery("memory_reset_authority_projects")
        project_ids = tuple(
            sorted(
                {uuid.UUID(str(value)) for value in (await self.session.execute(sa.select(authority_projects.c.project_id).order_by(authority_projects.c.project_id))).scalars()},
                key=str,
            )
        )
        if not project_ids:
            return ()
        await self.session.execute(sa.select(ProjectRow.id).where(ProjectRow.id.in_(project_ids)).order_by(ProjectRow.id).with_for_update(of=ProjectRow))
        await self.session.execute(
            sa.select(ProjectMembershipRow.id)
            .where(
                ProjectMembershipRow.project_id.in_(project_ids),
                ProjectMembershipRow.user_id == owner_user_id,
            )
            .order_by(ProjectMembershipRow.project_id)
            .with_for_update(of=ProjectMembershipRow)
        )
        return project_ids

    async def reset_owner(
        self,
        owner_user_id: str,
        *,
        now: datetime,
        authority_project_ids: tuple[uuid.UUID, ...] | None = None,
    ) -> MemoryResetCounts:
        try:
            owner_user_id = str(uuid.UUID(str(owner_user_id)))
        except (TypeError, ValueError):
            raise ValueError("Memory reset requires an owner UUID") from None
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Memory reset time must be timezone-aware")

        if authority_project_ids is None:
            authority_project_ids = await self.lock_owner_projects(owner_user_id)
        elif type(authority_project_ids) is not tuple or any(type(value) is not uuid.UUID for value in authority_project_ids) or tuple(sorted(set(authority_project_ids), key=str)) != authority_project_ids:
            raise ValueError("Memory reset project authority is invalid")

        scoped_sources = (
            MemoryDocumentRow,
            MemoryHistoryEntryRow,
            MemoryDocumentVersionRow,
            MemoryDreamRunRow,
            MemoryDreamPrepareRunRow,
            RunMemoryContextSnapshotRow,
            MemoryEpisodeRow,
        )
        scope_query = sa.union(
            *(
                sa.select(
                    row_type.project_id.label("project_id"),
                    row_type.namespace.label("namespace"),
                ).where(row_type.owner_user_id == owner_user_id)
                for row_type in scoped_sources
            ),
            sa.select(
                JobRow.project_id.label("project_id"),
                JobRow.namespace.label("namespace"),
            ).where(
                JobRow.owner_user_id == owner_user_id,
                JobRow.job_type.in_(("memory_dream", "memory_dream_prepare", "memory_seal")),
                JobRow.status.in_(("queued", "leased", "running", "retry_wait")),
                JobRow.namespace.is_not(None),
            ),
        )
        scope_rows = set((await self.session.execute(scope_query)).all())
        affected_project_ids = tuple(
            sorted(
                {uuid.UUID(str(row.project_id)) for row in scope_rows},
                key=str,
            )
        )
        if not set(affected_project_ids).issubset(authority_project_ids):
            raise MemoryDocumentConflict("Memory reset authority changed")

        # Project and Membership are caller-owned locks. Preserve the frozen
        # Thread -> Prepare -> Document -> Dream -> History -> Job suffix.
        from deerflow.persistence.thread_meta.model import ThreadMetaRow

        await self.session.execute(
            sa.select(ThreadMetaRow)
            .join(
                MemoryDreamPrepareRunRow,
                sa.and_(
                    MemoryDreamPrepareRunRow.project_id == ThreadMetaRow.project_id,
                    MemoryDreamPrepareRunRow.owner_user_id == ThreadMetaRow.owner_user_id,
                    MemoryDreamPrepareRunRow.thread_id == ThreadMetaRow.thread_id,
                ),
            )
            .where(MemoryDreamPrepareRunRow.owner_user_id == owner_user_id)
            .order_by(ThreadMetaRow.project_id, ThreadMetaRow.thread_id)
            .with_for_update(of=ThreadMetaRow)
        )
        await self.session.execute(
            sa.select(MemoryDreamPrepareRunRow)
            .where(MemoryDreamPrepareRunRow.owner_user_id == owner_user_id)
            .order_by(
                MemoryDreamPrepareRunRow.project_id,
                MemoryDreamPrepareRunRow.thread_id,
                MemoryDreamPrepareRunRow.job_id,
            )
            .with_for_update(of=MemoryDreamPrepareRunRow)
        )
        await self.session.execute(sa.select(MemoryDocumentRow).where(MemoryDocumentRow.owner_user_id == owner_user_id).order_by(MemoryDocumentRow.project_id, MemoryDocumentRow.namespace).with_for_update(of=MemoryDocumentRow))
        await self.session.execute(
            sa.select(MemoryDreamRunRow)
            .where(MemoryDreamRunRow.owner_user_id == owner_user_id)
            .order_by(
                MemoryDreamRunRow.project_id,
                MemoryDreamRunRow.namespace,
                MemoryDreamRunRow.job_id,
            )
            .with_for_update(of=MemoryDreamRunRow)
        )
        await self.session.execute(
            sa.select(MemoryHistoryEntryRow)
            .where(MemoryHistoryEntryRow.owner_user_id == owner_user_id)
            .order_by(
                MemoryHistoryEntryRow.project_id,
                MemoryHistoryEntryRow.namespace,
                MemoryHistoryEntryRow.sequence,
            )
            .with_for_update(of=MemoryHistoryEntryRow)
        )

        counts = {
            "history_entries": await self._count(
                MemoryHistoryEntryRow,
                owner_user_id,
            ),
            "documents": await self._count(MemoryDocumentRow, owner_user_id),
            "versions": await self._count(
                MemoryDocumentVersionRow,
                owner_user_id,
            ),
            "dream_runs": await self._count(MemoryDreamRunRow, owner_user_id),
            "prepare_runs": await self._count(
                MemoryDreamPrepareRunRow,
                owner_user_id,
            ),
            "snapshots": await self._count(
                RunMemoryContextSnapshotRow,
                owner_user_id,
            ),
            "episodes": await self._count(MemoryEpisodeRow, owner_user_id),
        }

        active_jobs = tuple(
            (
                await self.session.execute(
                    sa.select(
                        JobRow.id,
                        JobRow.project_id,
                        JobRow.owner_user_id,
                        JobRow.job_type,
                    )
                    .where(
                        JobRow.owner_user_id == owner_user_id,
                        JobRow.job_type.in_(
                            (
                                "memory_dream",
                                "memory_dream_prepare",
                                "memory_seal",
                            )
                        ),
                        JobRow.status.in_(("queued", "leased", "running", "retry_wait")),
                    )
                    .order_by(JobRow.project_id, JobRow.id)
                    .with_for_update(of=JobRow)
                )
            ).all()
        )
        jobs_cancelled = 0
        settled_dreams: list[MemoryResetSettledDream] = []
        for job_id, project_id, job_owner_user_id, job_type in active_jobs:
            scope = JobScope(project_id, job_owner_user_id)
            requested = await self.jobs.request_cancel(
                scope,
                job_id,
                reason="memory_reset",
                now=now,
            )
            if requested:
                jobs_cancelled += 1
            settled = await self.jobs.settle_requested_cancel(
                scope,
                job_id,
                now=now,
            )
            if settled and job_type == "memory_dream_prepare":
                await self.session.execute(
                    sa.update(MemoryDreamPrepareRunRow)
                    .where(
                        MemoryDreamPrepareRunRow.job_id == job_id,
                        MemoryDreamPrepareRunRow.completed_at.is_(None),
                    )
                    .values(
                        phase="cancelled",
                        result_disposition="cancelled",
                        completed_at=now,
                        updated_at=now,
                    )
                )
            if settled and job_type == "memory_dream":
                settled_dreams.append(
                    MemoryResetSettledDream(
                        project_id=uuid.UUID(str(project_id)),
                        job_id=uuid.UUID(str(job_id)),
                    )
                )

        await self.session.execute(sa.delete(MemoryDreamPrepareRunRow).where(MemoryDreamPrepareRunRow.owner_user_id == owner_user_id))
        await self.session.execute(sa.delete(RunMemoryContextSnapshotRow).where(RunMemoryContextSnapshotRow.owner_user_id == owner_user_id))
        await self.session.execute(sa.delete(MemoryHistoryEntryRow).where(MemoryHistoryEntryRow.owner_user_id == owner_user_id))
        await self.session.execute(sa.delete(MemoryEpisodeRow).where(MemoryEpisodeRow.owner_user_id == owner_user_id))
        await self.session.execute(sa.delete(MemoryDocumentRow).where(MemoryDocumentRow.owner_user_id == owner_user_id))
        await self.session.flush()
        return MemoryResetCounts(
            scopes_reset=len(scope_rows),
            history_entries=counts["history_entries"],
            documents=counts["documents"],
            versions=counts["versions"],
            dream_runs=counts["dream_runs"],
            prepare_runs=counts["prepare_runs"],
            snapshots=counts["snapshots"],
            episodes=counts["episodes"],
            jobs_cancelled=jobs_cancelled,
            affected_project_ids=affected_project_ids,
            settled_dreams=tuple(settled_dreams),
        )

    async def _count(self, row_type, owner_user_id: str) -> int:
        return int(await self.session.scalar(sa.select(sa.func.count()).select_from(row_type).where(row_type.owner_user_id == owner_user_id)) or 0)

    async def search_episodes(
        self,
        scope: MemoryDocumentScope,
        *,
        query: str,
        tags: tuple[str, ...] = (),
        limit: int = 5,
        retention_days: int = DEFAULT_EPISODE_RETENTION_DAYS,
        now: datetime,
    ) -> tuple[MemoryEpisodeRecord, ...]:
        return await self.episodes.search(
            scope,
            query=query,
            tags=tags,
            limit=limit,
            retention_days=retention_days,
            now=now,
        )

    async def list_episodes(
        self,
        scope: MemoryDocumentScope,
        *,
        tags: tuple[str, ...] = (),
        cursor: str | None = None,
        before: datetime | None = None,
        limit: int = 20,
        retention_days: int = DEFAULT_EPISODE_RETENTION_DAYS,
        now: datetime,
    ) -> MemoryEpisodePage:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Episode limit is out of contract")
        if cursor is not None and before is not None:
            raise MemoryEpisodeCursorInvalid("Episode cursors are mutually exclusive")
        if before is not None and (not isinstance(before, datetime) or before.tzinfo is None):
            raise MemoryEpisodeCursorInvalid("Episode time cursor is invalid")
        boundary = decode_memory_episode_cursor(cursor) if cursor is not None else ((before, uuid.UUID(int=0)) if before is not None else None)
        rows = await self.episodes.list(
            scope,
            tags=tags,
            cursor=boundary,
            limit=limit + 1,
            retention_days=retention_days,
            now=now,
        )
        items = rows[:limit]
        return MemoryEpisodePage(
            items=items,
            next_cursor=(encode_memory_episode_cursor(items[-1]) if len(rows) > limit and items else None),
        )


__all__ = [
    "BUDGET_REWRITE_HISTORY_DIGEST",
    "DEFAULT_MEMORY_NAMESPACE",
    "DREAM_HISTORY_BATCH_SIZE",
    "MEMORY_REVIEW_DELETION_RATIO",
    "MEMORY_REVIEW_MIN_LINES",
    "MemoryBudgetRewriteScanCursor",
    "MemoryBudgetRewriteScopePage",
    "MemoryDocumentConflict",
    "MemoryDocumentNotFound",
    "MemoryDocumentRecord",
    "MemoryDocumentRepository",
    "MemoryDocumentScope",
    "MemoryDocumentState",
    "MemoryDocumentVersionRecord",
    "MemoryEpisodePage",
    "MemoryEpisodeCursorInvalid",
    "MemoryEpisodeRecord",
    "MemoryDreamAdmissionDisposition",
    "MemoryDreamAdmissionKind",
    "MemoryDreamAdmissionRecord",
    "MemoryDreamFrozenRuntime",
    "MemoryDreamHistoryRecord",
    "MemoryDreamLeaseConflict",
    "MemoryDreamReleaseDisposition",
    "MemoryDreamReleaseResult",
    "MemoryDreamSettlementInvariant",
    "MemoryDreamStaleConflict",
    "MemoryDreamTrigger",
    "MemoryDreamWork",
    "MemoryHistoryActivation",
    "MemoryHistoryActivationResult",
    "MemoryHistoryActivationStatus",
    "MAX_MEMORY_UNIFIED_DIFF_CHARS",
    "MemoryResetCounts",
    "MemoryResetSettledDream",
    "TOOL_ENTRY_DUE_MINUTES",
    "compute_dream_history_digest",
    "decode_memory_episode_cursor",
    "encode_memory_episode_cursor",
    "memory_document_deletion_ratio",
    "memory_document_diff_preview",
    "memory_document_digest",
    "memory_document_needs_review",
    "memory_document_unified_diff",
]
