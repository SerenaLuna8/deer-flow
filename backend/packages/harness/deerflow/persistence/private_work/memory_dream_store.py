"""Session-bound Dream persistence state machine."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.memory_contract import (
    BUDGET_REWRITE_HISTORY_DIGEST,
    DEFAULT_EPISODE_RETENTION_DAYS,
    MAX_MEMORY_DOCUMENT_CHARS,
    MemoryBudgetRewriteScanCursor,
    MemoryBudgetRewriteScopePage,
    MemoryDocumentConflict,
    MemoryDocumentScope,
    MemoryDocumentVersionRecord,
    MemoryDreamAdmissionRecord,
    MemoryDreamFrozenRuntime,
    MemoryDreamHistoryRecord,
    MemoryDreamLeaseConflict,
    MemoryDreamReleaseResult,
    MemoryDreamSettlementInvariant,
    MemoryDreamStaleConflict,
    MemoryDreamTrigger,
    MemoryDreamWork,
    compute_dream_history_digest,
    estimate_memory_tokens,
    memory_document_digest,
    memory_document_needs_review,
    memory_document_unified_diff,
    render_empty_memory_document,
    scope_predicates,
    validate_episode_retention_days,
    validate_memory_document,
    validate_memory_document_sections,
)
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import EnqueueJob, JobRepository, JobScope
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    MemoryDocumentVersionRow,
    MemoryDreamRunRow,
    MemoryEpisodeRow,
    MemoryHistoryEntryRow,
)
from deerflow.persistence.private_work.memory_repository_parts import (
    MemoryDocumentStore,
    frozen_document_sections,
    version_record,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.system_settings import SystemModelConfigVersionRow
from deerflow.persistence.user.model import UserRow

# Persistence-only Dream orchestration limits.
_EPISODE_PRUNE_BATCH_LIMIT = 500
DREAM_HISTORY_BATCH_SIZE = 20
_BUDGET_REWRITE_SCAN_BATCH_SIZE = 100
TOOL_ENTRY_DUE_MINUTES = 10


class MemoryDreamStore:
    def __init__(
        self,
        session: AsyncSession,
        *,
        jobs: JobRepository | None = None,
        documents: MemoryDocumentStore | None = None,
    ) -> None:
        self.session = session
        self.jobs = jobs or JobRepository(session)
        self.documents = documents or MemoryDocumentStore(session)

    @staticmethod
    def _scope_predicates(
        row_type,
        scope: MemoryDocumentScope,
    ) -> tuple[sa.ColumnElement[bool], ...]:
        return scope_predicates(row_type, scope)

    @staticmethod
    def _latest_dream_activity(scope_row) -> sa.ColumnElement[datetime]:
        """Return the latest admission or Job transition for one Dream scope."""

        dream = MemoryDreamRunRow
        job = JobRow
        return (
            sa.select(sa.func.max(sa.func.greatest(dream.created_at, job.updated_at)))
            .select_from(dream)
            .join(job, job.id == dream.job_id)
            .where(
                dream.project_id == scope_row.project_id,
                dream.owner_user_id == scope_row.owner_user_id,
                dream.namespace == scope_row.namespace,
            )
            .correlate(scope_row)
            .scalar_subquery()
        )

    @staticmethod
    def _frozen_document_sections(
        row: MemoryDocumentRow,
    ) -> tuple[tuple[str, ...], uuid.UUID]:
        return frozen_document_sections(row)

    @classmethod
    def _due_condition(
        cls,
        history: type[MemoryHistoryEntryRow],
        document: type[MemoryDocumentRow],
        *,
        now: datetime,
        interval_minutes: int,
    ) -> sa.ColumnElement[bool]:
        """Three-way due rule over one scope's grouped pending entries.

        A scope is due when the interval has elapsed since the last Dream
        activity, when a full batch is already waiting, or when an explicit
        `remember` proposal has been pending longer than its grace window.
        """

        oldest_pending = sa.func.min(history.created_at)
        latest_dream_activity = cls._latest_dream_activity(history)
        due_anchor = sa.func.greatest(
            oldest_pending,
            sa.func.coalesce(document.updated_at, oldest_pending),
            sa.func.coalesce(latest_dream_activity, oldest_pending),
        )
        oldest_tool_pending = sa.func.min(sa.case((history.origin == "tool", history.created_at)))
        return sa.or_(
            due_anchor <= now - timedelta(minutes=interval_minutes),
            sa.func.count() >= DREAM_HISTORY_BATCH_SIZE,
            oldest_tool_pending <= now - timedelta(minutes=TOOL_ENTRY_DUE_MINUTES),
        )

    async def list_due_scopes(
        self,
        *,
        now: datetime,
        interval_minutes: int,
        limit: int = 100,
    ) -> tuple[MemoryDocumentScope, ...]:
        if not isinstance(now, datetime) or now.tzinfo is None or type(interval_minutes) is not int or not 15 <= interval_minutes <= 1_440 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Dream schedule boundary is invalid")
        document = MemoryDocumentRow
        history = MemoryHistoryEntryRow
        oldest_pending = sa.func.min(history.created_at)
        latest_dream_activity = self._latest_dream_activity(history)
        due_anchor = sa.func.greatest(
            oldest_pending,
            sa.func.coalesce(document.updated_at, oldest_pending),
            sa.func.coalesce(latest_dream_activity, oldest_pending),
        )
        rows = tuple(
            await self.session.execute(
                sa.select(
                    history.project_id,
                    history.owner_user_id,
                    history.namespace,
                )
                .outerjoin(
                    document,
                    sa.and_(
                        document.project_id == history.project_id,
                        document.owner_user_id == history.owner_user_id,
                        document.namespace == history.namespace,
                    ),
                )
                .where(
                    history.status == "pending",
                    document.active_dream_job_id.is_(None),
                )
                .group_by(
                    history.project_id,
                    history.owner_user_id,
                    history.namespace,
                    document.updated_at,
                    document.active_dream_job_id,
                )
                .having(
                    self._due_condition(
                        history,
                        document,
                        now=now,
                        interval_minutes=interval_minutes,
                    )
                )
                .order_by(due_anchor, history.project_id, history.owner_user_id)
                .limit(limit)
            )
        )
        return tuple(
            MemoryDocumentScope(
                project_id=row.project_id,
                owner_user_id=row.owner_user_id,
                namespace=row.namespace,
            )
            for row in rows
        )

    async def list_budget_rewrite_scope_page(
        self,
        *,
        budget_tokens: int,
        admissible_roles: tuple[str, ...],
        cursor: MemoryBudgetRewriteScanCursor | None = None,
        limit: int = 100,
    ) -> MemoryBudgetRewriteScopePage:
        """Discover one bounded page for the empty-batch budget rescue.

        ``char_length(content) > budget_tokens`` is a necessary condition for
        being over budget (the token estimate never exceeds the character
        count), so SQL uses it only as a coarse prefilter. Exact filtering uses
        the shared token estimator before applying ``limit``.

        The SQL join removes stable authorization failures before paging:
        disabled owners, unavailable projects, inactive memberships, and roles
        that currently lack private-work creation. The application layer owns
        the role-to-capability mapping and supplies ``admissible_roles``.
        Admission still re-verifies all authority and budget state under locks.

        ``next_cursor`` advances to the last *coarse row inspected*, including
        an exact-token false positive. That is what lets later true candidates
        progress without keeping a database transaction open across pages.
        """

        if (
            type(budget_tokens) is not int
            or not 100 <= budget_tokens <= 8_000
            or type(admissible_roles) is not tuple
            or not admissible_roles
            or any(not isinstance(role, str) or not role or len(role) > 16 for role in admissible_roles)
            or len(set(admissible_roles)) != len(admissible_roles)
            or (cursor is not None and type(cursor) is not MemoryBudgetRewriteScanCursor)
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("Dream schedule boundary is invalid")
        document = MemoryDocumentRow
        history = MemoryHistoryEntryRow
        pending_exists = sa.exists(
            sa.select(sa.literal(1)).where(
                history.project_id == document.project_id,
                history.owner_user_id == document.owner_user_id,
                history.namespace == document.namespace,
                history.status == "pending",
            )
        )
        key_columns = (
            document.updated_at,
            document.project_id,
            document.owner_user_id,
            document.namespace,
        )
        statement = (
            sa.select(
                *key_columns,
                document.content,
            )
            .join(UserRow, UserRow.id == document.owner_user_id)
            .join(ProjectRow, ProjectRow.id == document.project_id)
            .join(
                ProjectMembershipRow,
                sa.and_(
                    ProjectMembershipRow.project_id == document.project_id,
                    ProjectMembershipRow.user_id == document.owner_user_id,
                ),
            )
            .outerjoin(JobRow, JobRow.id == document.active_dream_job_id)
            .where(
                document.version >= 1,
                sa.func.char_length(document.content) > budget_tokens,
                ~pending_exists,
                sa.or_(
                    document.active_dream_job_id.is_(None),
                    JobRow.status.not_in(("queued", "leased", "running", "retry_wait")),
                ),
                UserRow.memory_enabled.is_(True),
                ProjectRow.status == "active",
                ProjectRow.is_suspended.is_(False),
                ProjectMembershipRow.status == "active",
                ProjectMembershipRow.role.in_(admissible_roles),
            )
            .order_by(*key_columns)
            .limit(_BUDGET_REWRITE_SCAN_BATCH_SIZE)
        )
        if cursor is not None:
            statement = statement.where(
                sa.tuple_(*key_columns)
                > (
                    cursor.updated_at,
                    cursor.project_id,
                    cursor.owner_user_id,
                    cursor.namespace,
                )
            )
        rows = tuple((await self.session.execute(statement)).all())
        scopes: list[MemoryDocumentScope] = []
        last_scanned: MemoryBudgetRewriteScanCursor | None = None
        exhausted_page = True
        for index, row in enumerate(rows):
            last_scanned = MemoryBudgetRewriteScanCursor(
                updated_at=row.updated_at,
                project_id=row.project_id,
                owner_user_id=row.owner_user_id,
                namespace=row.namespace,
            )
            if estimate_memory_tokens(row.content) <= budget_tokens:
                continue
            scopes.append(
                MemoryDocumentScope(
                    project_id=row.project_id,
                    owner_user_id=row.owner_user_id,
                    namespace=row.namespace,
                )
            )
            if len(scopes) == limit:
                exhausted_page = index == len(rows) - 1
                break
        has_more = last_scanned is not None and (not exhausted_page or len(rows) == _BUDGET_REWRITE_SCAN_BATCH_SIZE)
        return MemoryBudgetRewriteScopePage(
            scopes=tuple(scopes),
            next_cursor=last_scanned if has_more else None,
        )

    async def is_scope_due(
        self,
        scope: MemoryDocumentScope,
        *,
        now: datetime,
        interval_minutes: int,
    ) -> bool:
        """Recheck one scope against the interval frozen by its admission transaction."""

        if type(scope) is not MemoryDocumentScope or not isinstance(now, datetime) or now.tzinfo is None or type(interval_minutes) is not int or not 15 <= interval_minutes <= 1_440:
            raise ValueError("Dream schedule boundary is invalid")
        document = MemoryDocumentRow
        history = MemoryHistoryEntryRow
        row = await self.session.scalar(
            sa.select(sa.literal(True))
            .select_from(history)
            .outerjoin(
                document,
                sa.and_(
                    document.project_id == history.project_id,
                    document.owner_user_id == history.owner_user_id,
                    document.namespace == history.namespace,
                ),
            )
            .where(
                *self._scope_predicates(history, scope),
                history.status == "pending",
                document.active_dream_job_id.is_(None),
            )
            .group_by(
                history.project_id,
                history.owner_user_id,
                history.namespace,
                document.updated_at,
                document.active_dream_job_id,
            )
            .having(
                self._due_condition(
                    history,
                    document,
                    now=now,
                    interval_minutes=interval_minutes,
                )
            )
            .limit(1)
        )
        return row is True

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
        if (
            type(scope) is not MemoryDocumentScope
            or trigger not in {"auto_dream", "manual_dream", "budget_rewrite"}
            or type(frozen) is not MemoryDreamFrozenRuntime
            or not isinstance(now, datetime)
            or now.tzinfo is None
            or type(max_attempts) is not int
            or not 1 <= max_attempts <= 20
        ):
            raise ValueError("Dream admission input is invalid")
        supplied_creation_material = (
            initial_content is not None,
            initial_sections is not None,
            sections_policy_version_id is not None,
        )
        if any(supplied_creation_material) and not all(supplied_creation_material):
            raise ValueError("Dream document creation material is incomplete")
        creation_sections: tuple[str, ...] | None = None
        if all(supplied_creation_material):
            if not isinstance(initial_content, str) or not isinstance(sections_policy_version_id, uuid.UUID):
                raise ValueError("Dream document creation material is invalid")
            creation_sections = validate_memory_document_sections(initial_sections)
            if initial_content != render_empty_memory_document(creation_sections):
                raise ValueError("Dream initial document does not match its sections")

        document = (await self.session.execute(sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope)).with_for_update(of=MemoryDocumentRow))).scalar_one_or_none()
        if document is None:
            if creation_sections is None or initial_content is None or sections_policy_version_id is None:
                raise MemoryDocumentConflict("Dream document creation policy is unavailable")
            await self.session.execute(
                pg_insert(MemoryDocumentRow)
                .values(
                    project_id=scope.project_id,
                    owner_user_id=scope.owner_user_id,
                    namespace=scope.namespace,
                    content=initial_content,
                    content_digest=memory_document_digest(initial_content),
                    sections=list(creation_sections),
                    sections_policy_version_id=sections_policy_version_id,
                    version=0,
                    dream_cursor=0,
                    active_dream_job_id=None,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=(
                        MemoryDocumentRow.project_id,
                        MemoryDocumentRow.owner_user_id,
                        MemoryDocumentRow.namespace,
                    )
                )
            )
            document = (await self.session.execute(sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope)).with_for_update(of=MemoryDocumentRow))).scalar_one_or_none()
            if document is None:
                raise MemoryDocumentConflict("Dream document creation disappeared")
        document_sections, _sections_policy_version_id = self._frozen_document_sections(document)
        if memory_document_digest(document.content) != document.content_digest:
            raise MemoryDocumentConflict("Memory document digest changed")
        validate_memory_document(
            document.content,
            MAX_MEMORY_DOCUMENT_CHARS,
            sections=document_sections,
        )
        active = await self._active_dream(document, scope)
        if active is not None:
            return active

        rows = tuple(
            (
                await self.session.execute(
                    sa.select(MemoryHistoryEntryRow)
                    .where(
                        *self._scope_predicates(MemoryHistoryEntryRow, scope),
                        MemoryHistoryEntryRow.status == "pending",
                    )
                    .order_by(MemoryHistoryEntryRow.sequence)
                    .limit(DREAM_HISTORY_BATCH_SIZE)
                    .with_for_update(of=MemoryHistoryEntryRow)
                )
            ).scalars()
        )[:DREAM_HISTORY_BATCH_SIZE]
        if trigger == "budget_rewrite":
            # The rescue path is only legal against an empty backlog; a raced
            # `remember` proposal must surface as a conflict, never as a Dream
            # that silently ignores pending work.
            if rows:
                raise MemoryDocumentConflict("Dream budget rewrite requires an empty backlog")
            if int(document.version) < 1:
                raise MemoryDocumentConflict("Dream budget rewrite requires a published document")
            return await self._enqueue_dream(
                scope,
                document=document,
                trigger=trigger,
                frozen=frozen,
                history=(),
                history_digest=BUDGET_REWRITE_HISTORY_DIGEST,
                rows=(),
                now=now,
                max_attempts=max_attempts,
            )
        if not rows:
            return MemoryDreamAdmissionRecord(
                disposition="nothing_pending",
                job_id=None,
                history_count=0,
            )
        history = tuple(
            MemoryDreamHistoryRecord(
                id=row.id,
                sequence=int(row.sequence),
                tagged_text=row.tagged_text,
                content_digest=row.content_digest,
                origin=row.origin,
            )
            for row in rows
        )
        if any(item.tagged_text is None for item in history):
            raise MemoryDocumentConflict("Dream pending history is invalid")
        history_digest = compute_dream_history_digest(history)
        return await self._enqueue_dream(
            scope,
            document=document,
            trigger=trigger,
            frozen=frozen,
            history=history,
            history_digest=history_digest,
            rows=rows,
            now=now,
            max_attempts=max_attempts,
        )

    async def _enqueue_dream(
        self,
        scope: MemoryDocumentScope,
        *,
        document: MemoryDocumentRow,
        trigger: MemoryDreamTrigger,
        frozen: MemoryDreamFrozenRuntime,
        history: tuple[MemoryDreamHistoryRecord, ...],
        history_digest: str,
        rows: tuple[MemoryHistoryEntryRow, ...],
        now: datetime,
        max_attempts: int,
    ) -> MemoryDreamAdmissionRecord:
        prior_generations = int(
            await self.session.scalar(
                sa.select(sa.func.count())
                .select_from(MemoryDreamRunRow)
                .where(
                    *self._scope_predicates(MemoryDreamRunRow, scope),
                    MemoryDreamRunRow.base_document_version == int(document.version),
                    MemoryDreamRunRow.history_digest == history_digest,
                )
            )
            or 0
        )
        idempotency_key = hashlib.sha256(
            "\x1f".join(
                (
                    "memory_dream_v1",
                    str(scope.project_id),
                    scope.owner_user_id,
                    scope.namespace,
                    str(document.version),
                    history_digest,
                    str(prior_generations + 1),
                )
            ).encode("utf-8")
        ).hexdigest()
        job_id = await self.jobs.enqueue(
            EnqueueJob(
                job_type="memory_dream",
                scope=JobScope(scope.project_id, scope.owner_user_id),
                namespace=scope.namespace,
                idempotency_key=idempotency_key,
                run_id=None,
                occurrence_id=None,
                max_attempts=max_attempts,
                retry_safety="safe",
                priority=0 if trigger == "auto_dream" else 10,
            )
        )
        run = MemoryDreamRunRow(
            job_id=job_id,
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            namespace=scope.namespace,
            trigger=trigger,
            history_from=history[0].sequence if history else None,
            history_to=history[-1].sequence if history else None,
            history_count=len(history),
            history_digest=history_digest,
            base_document_version=int(document.version),
            base_content_digest=document.content_digest,
            preference_version=frozen.preference_version,
            policy_revision=frozen.policy_revision,
            model_ref=frozen.model_version_id,
            prompt_version=frozen.prompt_version,
            result_version=None,
            created_at=now,
            completed_at=None,
        )
        self.session.add(run)
        await self.session.flush()
        for row in rows:
            row.status = "processing"
            row.dream_job_id = job_id
        document.active_dream_job_id = job_id
        await self.session.flush()
        return MemoryDreamAdmissionRecord(
            disposition="queued",
            job_id=job_id,
            history_count=len(history),
            admission_kind=("budget_rewrite" if trigger == "budget_rewrite" else "history"),
        )

    async def _active_dream(
        self,
        document: MemoryDocumentRow,
        scope: MemoryDocumentScope,
    ) -> MemoryDreamAdmissionRecord | None:
        job_id = document.active_dream_job_id
        if job_id is None:
            return None
        result = (
            await self.session.execute(
                sa.select(JobRow, MemoryDreamRunRow)
                .outerjoin(
                    MemoryDreamRunRow,
                    MemoryDreamRunRow.job_id == JobRow.id,
                )
                .where(
                    JobRow.id == job_id,
                    JobRow.project_id == scope.project_id,
                    JobRow.owner_user_id == scope.owner_user_id,
                    JobRow.namespace == scope.namespace,
                )
                .with_for_update(of=JobRow)
            )
        ).one_or_none()
        if result is None:
            raise MemoryDocumentConflict("Dream active Job is missing")
        job, run = result
        if job.status in {"queued", "leased", "running", "retry_wait"}:
            if run is None:
                raise MemoryDocumentConflict("Dream run is missing")
            return MemoryDreamAdmissionRecord(
                disposition="already_running",
                job_id=job.id,
                history_count=int(run.history_count),
                admission_kind=("budget_rewrite" if run.trigger == "budget_rewrite" else "history"),
            )
        if run is not None and run.result_version is not None:
            document.active_dream_job_id = None
            return None
        await self.session.execute(
            sa.update(MemoryHistoryEntryRow)
            .where(
                *self._scope_predicates(MemoryHistoryEntryRow, scope),
                MemoryHistoryEntryRow.status == "processing",
                MemoryHistoryEntryRow.dream_job_id == job_id,
            )
            .values(
                status="pending",
                dream_job_id=None,
                consumed_at=None,
            )
        )
        document.active_dream_job_id = None
        await self.session.flush()
        return None

    async def load_dream_work(
        self,
        scope: MemoryDocumentScope,
        job_id: uuid.UUID,
    ) -> MemoryDreamWork | None:
        if type(scope) is not MemoryDocumentScope or not isinstance(job_id, uuid.UUID):
            raise TypeError("Dream work authority is invalid")
        result = (
            await self.session.execute(
                sa.select(
                    MemoryDreamRunRow,
                    MemoryDocumentRow,
                    SystemModelConfigVersionRow,
                    JobRow,
                )
                .join(
                    MemoryDocumentRow,
                    sa.and_(
                        MemoryDocumentRow.project_id == MemoryDreamRunRow.project_id,
                        MemoryDocumentRow.owner_user_id == MemoryDreamRunRow.owner_user_id,
                        MemoryDocumentRow.namespace == MemoryDreamRunRow.namespace,
                    ),
                )
                .join(
                    SystemModelConfigVersionRow,
                    SystemModelConfigVersionRow.id == MemoryDreamRunRow.model_ref,
                )
                .join(JobRow, JobRow.id == MemoryDreamRunRow.job_id)
                .where(
                    MemoryDreamRunRow.job_id == job_id,
                    *self._scope_predicates(MemoryDreamRunRow, scope),
                )
            )
        ).one_or_none()
        if result is None:
            return None
        run, document, model_version, job = result
        history_rows = tuple(
            (
                await self.session.execute(
                    sa.select(MemoryHistoryEntryRow)
                    .where(
                        *self._scope_predicates(MemoryHistoryEntryRow, scope),
                        MemoryHistoryEntryRow.dream_job_id == job_id,
                    )
                    .order_by(MemoryHistoryEntryRow.sequence)
                )
            ).scalars()
        )
        history = tuple(
            MemoryDreamHistoryRecord(
                id=row.id,
                sequence=int(row.sequence),
                tagged_text=row.tagged_text,
                content_digest=row.content_digest,
                origin=row.origin,
            )
            for row in history_rows
        )
        document_sections, sections_policy_version_id = self._frozen_document_sections(document)
        return MemoryDreamWork(
            job_id=run.job_id,
            project_id=run.project_id,
            owner_user_id=run.owner_user_id,
            namespace=run.namespace,
            trigger=run.trigger,
            history_from=(None if run.history_from is None else int(run.history_from)),
            history_to=(None if run.history_to is None else int(run.history_to)),
            history_count=int(run.history_count),
            history_digest=run.history_digest,
            base_document_version=int(run.base_document_version),
            base_content=document.content if run.result_version is None else (await self.documents.read_version(scope, int(run.result_version))).content,
            base_content_digest=run.base_content_digest,
            sections=document_sections,
            sections_policy_version_id=sections_policy_version_id,
            preference_version=int(run.preference_version),
            policy_revision=int(run.policy_revision),
            model_config_id=model_version.model_config_id,
            model_version_id=model_version.id,
            model_payload_checksum=model_version.payload_checksum,
            prompt_version=run.prompt_version,
            result_version=(None if run.result_version is None else int(run.result_version)),
            cancel_requested=job.cancel_requested_at is not None,
            job_status=job.status,
            history=history,
        )

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
        validate_episode_retention_days(episode_retention_days)
        expected_sections = validate_memory_document_sections(expected_sections)
        existing = (
            await self.session.execute(
                sa.select(MemoryDocumentVersionRow).where(
                    *self._scope_predicates(MemoryDocumentVersionRow, scope),
                    MemoryDocumentVersionRow.dream_job_id == job_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return self._version_record(existing)
        document = (await self.session.execute(sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope)).with_for_update(of=MemoryDocumentRow))).scalar_one_or_none()
        run = (
            await self.session.execute(
                sa.select(MemoryDreamRunRow)
                .where(
                    MemoryDreamRunRow.job_id == job_id,
                    *self._scope_predicates(MemoryDreamRunRow, scope),
                )
                .with_for_update(of=MemoryDreamRunRow)
            )
        ).scalar_one_or_none()
        history_rows = tuple(
            (
                await self.session.execute(
                    sa.select(MemoryHistoryEntryRow)
                    .where(
                        *self._scope_predicates(MemoryHistoryEntryRow, scope),
                        MemoryHistoryEntryRow.dream_job_id == job_id,
                    )
                    .order_by(MemoryHistoryEntryRow.sequence)
                    .with_for_update(of=MemoryHistoryEntryRow)
                )
            ).scalars()
        )
        history = tuple(
            MemoryDreamHistoryRecord(
                id=row.id,
                sequence=int(row.sequence),
                tagged_text=row.tagged_text,
                content_digest=row.content_digest,
                origin=row.origin,
            )
            for row in history_rows
        )
        if (
            document is None
            or run is None
            or document.active_dream_job_id != job_id
            or int(document.version) != expected_base_version
            or document.content_digest != expected_base_digest
            or int(run.base_document_version) != expected_base_version
            or run.base_content_digest != expected_base_digest
            or self._frozen_document_sections(document)[0] != expected_sections
            or run.history_digest != expected_history_digest
            or int(run.history_count) != len(history)
            or any(row.status != "processing" for row in history_rows)
        ):
            raise MemoryDreamStaleConflict("Dream settlement contract changed")
        validate_memory_document(
            content,
            MAX_MEMORY_DOCUMENT_CHARS,
            sections=expected_sections,
        )
        if run.trigger == "budget_rewrite":
            if history or expected_history_digest != BUDGET_REWRITE_HISTORY_DIGEST:
                raise MemoryDreamStaleConflict("Dream settlement contract changed")
        elif (
            not history or run.history_from is None or run.history_to is None or int(run.history_from) != history[0].sequence or int(run.history_to) != history[-1].sequence or compute_dream_history_digest(history) != expected_history_digest
        ):
            raise MemoryDreamStaleConflict("Dream settlement contract changed")
        next_version = int(document.version) + 1
        unified_diff = memory_document_unified_diff(document.content, content)
        content_digest = memory_document_digest(content)
        version = MemoryDocumentVersionRow(
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            namespace=scope.namespace,
            version=next_version,
            content=content,
            content_digest=content_digest,
            unified_diff=unified_diff,
            trigger=run.trigger,
            dream_job_id=job_id,
            history_from=run.history_from,
            history_to=run.history_to,
            history_count=run.history_count,
            prompt_version=run.prompt_version,
            model_ref=run.model_ref,
            needs_review=memory_document_needs_review(document.content, content, history),
            created_at=now,
        )
        self.session.add(version)
        for row in history_rows:
            # Archive the full text as an episode in the same transaction that
            # tombstones the history row.  Reusing the history UUID makes a
            # duplicate settlement collide on the primary key instead of
            # silently duplicating the archive.
            self.session.add(
                MemoryEpisodeRow(
                    id=row.id,
                    project_id=row.project_id,
                    owner_user_id=row.owner_user_id,
                    namespace=row.namespace,
                    thread_id=row.thread_id,
                    origin=row.origin,
                    tagged_text=row.tagged_text,
                    content_digest=row.content_digest,
                    occurred_at=row.created_at,
                    consumed_dream_job_id=job_id,
                    created_at=now,
                )
            )
            row.status = "consumed"
            row.tagged_text = None
            row.consumed_at = now
        document.content = content
        document.content_digest = content_digest
        document.version = next_version
        if run.history_to is not None:
            document.dream_cursor = max(int(document.dream_cursor), int(run.history_to))
        document.active_dream_job_id = None
        document.updated_at = now
        run.result_version = next_version
        run.completed_at = now
        await self.session.flush()
        await self._prune_expired_episodes(
            scope,
            now=now,
            episode_retention_days=episode_retention_days,
        )
        if not await self.jobs.settle_success(
            job_id,
            lease_token=lease_token,
            now=now,
        ):
            raise MemoryDreamLeaseConflict("Dream Job lease changed")
        await self.session.flush()
        return self._version_record(version)

    async def _prune_expired_episodes(
        self,
        scope: MemoryDocumentScope,
        *,
        now: datetime,
        episode_retention_days: int,
    ) -> None:
        """Bounded same-transaction cleanup; there is no dedicated purge Job."""

        if episode_retention_days == 0:
            return
        cutoff = now - timedelta(days=episode_retention_days)
        expired = (
            sa.select(MemoryEpisodeRow.id)
            .where(
                *self._scope_predicates(MemoryEpisodeRow, scope),
                MemoryEpisodeRow.occurred_at < cutoff,
            )
            .order_by(MemoryEpisodeRow.occurred_at)
            .limit(_EPISODE_PRUNE_BATCH_LIMIT)
        )
        await self.session.execute(sa.delete(MemoryEpisodeRow).where(MemoryEpisodeRow.id.in_(expired)))

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
        """Release one frozen Dream while preserving Document-before-Job locks.

        The Worker must already hold the authoritative Project and Membership
        locks.  This method then locks the Memory document, Dream run, and
        frozen history before asking ``JobRepository`` to take the Job lock.
        Admission takes the same Document -> Job order.
        """

        if type(retryable) is not bool:
            raise TypeError("Dream retryable flag must be a boolean")

        # These reads deliberately take every Memory resource lock before the
        # Job lock.  A concurrent admission already owns the Document first and
        # may then inspect the active Job, so reversing these two locks can form
        # a real PostgreSQL deadlock.
        document = (await self.session.execute(sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope)).with_for_update(of=MemoryDocumentRow))).scalar_one_or_none()
        run = (
            await self.session.execute(
                sa.select(MemoryDreamRunRow)
                .where(
                    MemoryDreamRunRow.job_id == job_id,
                    *self._scope_predicates(MemoryDreamRunRow, scope),
                )
                .with_for_update(of=MemoryDreamRunRow)
            )
        ).scalar_one_or_none()
        history_rows = tuple(
            (
                await self.session.execute(
                    sa.select(MemoryHistoryEntryRow)
                    .where(
                        *self._scope_predicates(MemoryHistoryEntryRow, scope),
                        MemoryHistoryEntryRow.dream_job_id == job_id,
                    )
                    .order_by(MemoryHistoryEntryRow.sequence)
                    .with_for_update(of=MemoryHistoryEntryRow)
                )
            ).scalars()
        )
        if run is not None and run.result_version is not None:
            return MemoryDreamReleaseResult(disposition="already_published")

        # Bind the supplied Job id to the exact frozen private scope before a
        # JobRepository method mutates it.  This is a non-locking authority
        # read; the Job lock remains the final lock in the sequence above.
        job_authority = (
            await self.session.execute(
                sa.select(JobRow.id).where(
                    JobRow.id == job_id,
                    JobRow.job_type == "memory_dream",
                    JobRow.project_id == scope.project_id,
                    JobRow.owner_user_id == scope.owner_user_id,
                    JobRow.namespace == scope.namespace,
                )
            )
        ).scalar_one_or_none()
        if job_authority != job_id:
            raise MemoryDreamLeaseConflict("Dream Job authority changed")

        if cancelled:
            settled = await self.jobs.settle_cancelled(
                job_id,
                lease_token=lease_token,
                now=now,
            )
        else:
            settled = await self.jobs.retry_or_dead(
                job_id,
                lease_token=lease_token,
                public_error_code=public_error_code,
                retryable=retryable,
                retry_initial_seconds=retry_initial_seconds,
                retry_max_seconds=retry_max_seconds,
                now=now,
            )
        if not settled:
            raise MemoryDreamLeaseConflict("Dream Job lease changed")
        job_status = await self.session.scalar(sa.select(JobRow.status).where(JobRow.id == job_id))
        if job_status == "retry_wait":
            # A retry owns the same frozen batch.  Keep both the document's
            # active pointer and the history rows in processing so no competing
            # Dream can consume or mutate them between attempts.
            return MemoryDreamReleaseResult(disposition="retry_wait")
        if job_status not in {"cancelled", "dead"}:
            raise MemoryDreamSettlementInvariant("Dream Job terminal state is invalid")

        for row in history_rows:
            if row.status == "processing":
                row.status = "pending"
                row.dream_job_id = None
                row.consumed_at = None
        if document is not None and document.active_dream_job_id == job_id:
            document.active_dream_job_id = None
        await self.session.flush()
        return MemoryDreamReleaseResult(disposition=job_status)

    async def request_dream_cancel(
        self,
        scope: MemoryDocumentScope,
        job_id: uuid.UUID,
        *,
        reason: str,
        now: datetime,
    ) -> bool:
        """Request cancellation and release an unowned frozen Dream.

        The caller must already hold the authoritative Project and Membership
        locks.  Resource locks follow the same Document -> Dream -> History ->
        Job suffix as Worker settlement.  A leased/running Job is only marked
        for cooperative cancellation; its frozen state remains owned until the
        Worker settles with its lease.  ``True`` means this transaction made an
        unowned Job terminal and released its Memory state.
        """

        if type(scope) is not MemoryDocumentScope or not isinstance(job_id, uuid.UUID) or type(reason) is not str or not 1 <= len(reason) <= 64 or not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("Dream cancellation input is invalid")

        document = (await self.session.execute(sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope)).with_for_update(of=MemoryDocumentRow))).scalar_one_or_none()
        run = (
            await self.session.execute(
                sa.select(MemoryDreamRunRow)
                .where(
                    MemoryDreamRunRow.job_id == job_id,
                    *self._scope_predicates(MemoryDreamRunRow, scope),
                )
                .with_for_update(of=MemoryDreamRunRow)
            )
        ).scalar_one_or_none()
        history_rows = tuple(
            (
                await self.session.execute(
                    sa.select(MemoryHistoryEntryRow)
                    .where(
                        *self._scope_predicates(MemoryHistoryEntryRow, scope),
                        MemoryHistoryEntryRow.dream_job_id == job_id,
                    )
                    .order_by(MemoryHistoryEntryRow.sequence)
                    .with_for_update(of=MemoryHistoryEntryRow)
                )
            ).scalars()
        )
        job = (
            await self.session.execute(
                sa.select(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.job_type == "memory_dream",
                    JobRow.project_id == scope.project_id,
                    JobRow.owner_user_id == scope.owner_user_id,
                    JobRow.namespace == scope.namespace,
                )
                .with_for_update(of=JobRow)
            )
        ).scalar_one_or_none()
        if run is None or job is None:
            raise MemoryDreamSettlementInvariant("Dream cancellation authority is missing")
        if run.result_version is not None:
            return False

        if job.status in {"cancelled", "dead"}:
            for row in history_rows:
                if row.status == "processing":
                    row.status = "pending"
                    row.dream_job_id = None
                    row.consumed_at = None
            if document is not None and document.active_dream_job_id == job_id:
                document.active_dream_job_id = None
            await self.session.flush()
            return False
        if job.status not in {"queued", "leased", "running", "retry_wait"}:
            raise MemoryDreamSettlementInvariant("Dream cancellation Job state is invalid")
        if document is None or document.active_dream_job_id != job_id:
            raise MemoryDreamSettlementInvariant("Dream cancellation state is inconsistent")

        job_scope = JobScope(scope.project_id, scope.owner_user_id)
        if not await self.jobs.request_cancel(
            job_scope,
            job_id,
            reason=reason,
            now=now,
        ):
            raise MemoryDreamSettlementInvariant("Dream cancellation request changed")
        if not await self.jobs.settle_requested_cancel(
            job_scope,
            job_id,
            now=now,
        ):
            return False

        for row in history_rows:
            if row.status == "processing":
                row.status = "pending"
                row.dream_job_id = None
                row.consumed_at = None
        document.active_dream_job_id = None
        await self.session.flush()
        return True

    @staticmethod
    def _version_record(
        row: MemoryDocumentVersionRow,
    ) -> MemoryDocumentVersionRecord:
        return version_record(row)


__all__ = [
    "DREAM_HISTORY_BATCH_SIZE",
    "MemoryDreamStore",
    "TOOL_ENTRY_DUE_MINUTES",
]
