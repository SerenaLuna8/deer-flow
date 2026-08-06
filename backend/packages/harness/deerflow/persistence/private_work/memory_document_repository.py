"""Scoped repository for the final Memory document model."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.agents.memory.snip import (
    SNIP_NOTHING,
    compute_snip_content_digest,
    validate_snip_output,
)
from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import EnqueueJob, JobRepository, JobScope
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    MemoryDocumentVersionRow,
    MemoryDreamRunRow,
    MemoryHistoryEntryRow,
    RunMemoryContextSnapshotRow,
)
from deerflow.persistence.system_settings import SystemModelConfigVersionRow
from deerflow.persistence.user.model import UserRow

DEFAULT_MEMORY_NAMESPACE = "default"


class MemoryDocumentNotFound(LookupError):
    pass


class MemoryDocumentConflict(RuntimeError):
    pass


MemoryDreamTrigger = Literal["auto_dream", "manual_dream"]
MemoryDreamAdmissionDisposition = Literal[
    "queued",
    "already_running",
    "nothing_pending",
]
MemoryHistoryActivationStatus = Literal[
    "created",
    "pending",
    "processing",
    "consumed",
    "stale",
]

_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class MemoryDocumentScope:
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str = DEFAULT_MEMORY_NAMESPACE

    def __post_init__(self) -> None:
        try:
            project_id = uuid.UUID(str(self.project_id))
            owner_user_id = str(uuid.UUID(str(self.owner_user_id)))
        except (TypeError, ValueError):
            raise ValueError("Memory scope requires project and owner UUIDs") from None
        namespace = self.namespace.strip() if isinstance(self.namespace, str) else ""
        if not namespace or len(namespace) > 255:
            raise ValueError("Memory scope requires a bounded namespace")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "owner_user_id", owner_user_id)
        object.__setattr__(self, "namespace", namespace)


@dataclass(frozen=True, slots=True)
class MemoryHistoryActivation:
    """Exact committed-checkpoint receipt accepted by the history repository."""

    scope: MemoryDocumentScope
    thread_id: str
    source_checkpoint_id: str
    committed_checkpoint_id: str
    source_digest: str
    tagged_text: str
    content_digest: str
    preference_version: int
    snip_prompt_version: str
    summary_model_ref: uuid.UUID

    def __post_init__(self) -> None:
        try:
            summary_model_ref = uuid.UUID(str(self.summary_model_ref))
        except (TypeError, ValueError):
            raise ValueError("Memory history model reference is invalid") from None
        try:
            tagged_text = validate_snip_output(self.tagged_text)
        except (TypeError, ValueError):
            raise ValueError("Memory history SNIP text is invalid") from None
        if (
            type(self.scope) is not MemoryDocumentScope
            or not isinstance(self.thread_id, str)
            or not self.thread_id
            or len(self.thread_id) > 64
            or not isinstance(self.source_checkpoint_id, str)
            or not self.source_checkpoint_id
            or len(self.source_checkpoint_id) > 128
            or not isinstance(self.committed_checkpoint_id, str)
            or not self.committed_checkpoint_id
            or len(self.committed_checkpoint_id) > 128
            or not isinstance(self.source_digest, str)
            or _SHA256_HEX.fullmatch(self.source_digest) is None
            or tagged_text == SNIP_NOTHING
            or not isinstance(self.content_digest, str)
            or _SHA256_HEX.fullmatch(self.content_digest) is None
            or self.content_digest != compute_snip_content_digest(tagged_text)
            or type(self.preference_version) is not int
            or self.preference_version < 1
            or not isinstance(self.snip_prompt_version, str)
            or not self.snip_prompt_version
            or len(self.snip_prompt_version) > 64
        ):
            raise ValueError("Memory history activation is invalid")
        object.__setattr__(self, "tagged_text", tagged_text)
        object.__setattr__(self, "summary_model_ref", summary_model_ref)


@dataclass(frozen=True, slots=True)
class MemoryHistoryActivationResult:
    status: MemoryHistoryActivationStatus
    entry_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class MemoryDocumentRecord:
    content: str
    content_digest: str
    version: int
    dream_cursor: int
    active_dream_job_id: uuid.UUID | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class MemoryDocumentState:
    document: MemoryDocumentRecord
    pending_count: int


@dataclass(frozen=True, slots=True)
class MemoryDocumentVersionRecord:
    version: int
    content: str
    content_digest: str
    unified_diff: str
    trigger: str
    dream_job_id: uuid.UUID | None
    history_from: int | None
    history_to: int | None
    history_count: int | None
    prompt_version: str | None
    model_ref: uuid.UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryDreamFrozenRuntime:
    preference_version: int
    policy_revision: int
    model_config_id: uuid.UUID
    model_version_id: uuid.UUID
    model_payload_checksum: str
    prompt_version: str

    def __post_init__(self) -> None:
        if (
            type(self.preference_version) is not int
            or self.preference_version < 1
            or type(self.policy_revision) is not int
            or self.policy_revision < 1
            or not isinstance(self.model_config_id, uuid.UUID)
            or not isinstance(self.model_version_id, uuid.UUID)
            or not isinstance(self.model_payload_checksum, str)
            or len(self.model_payload_checksum) != 64
            or not isinstance(self.prompt_version, str)
            or not self.prompt_version
            or len(self.prompt_version) > 64
        ):
            raise ValueError("Dream frozen runtime is invalid")


@dataclass(frozen=True, slots=True)
class MemoryDreamAdmissionRecord:
    disposition: MemoryDreamAdmissionDisposition
    job_id: uuid.UUID | None
    history_count: int


@dataclass(frozen=True, slots=True)
class MemoryDreamHistoryRecord:
    id: uuid.UUID
    sequence: int
    tagged_text: str | None
    content_digest: str


@dataclass(frozen=True, slots=True)
class MemoryDreamWork:
    job_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str
    trigger: MemoryDreamTrigger
    history_from: int
    history_to: int
    history_count: int
    history_digest: str
    base_document_version: int
    base_content: str
    base_content_digest: str
    preference_version: int
    policy_revision: int
    model_config_id: uuid.UUID
    model_version_id: uuid.UUID
    model_payload_checksum: str
    prompt_version: str
    result_version: int | None
    cancel_requested: bool
    job_status: str
    history: tuple[MemoryDreamHistoryRecord, ...]


@dataclass(frozen=True, slots=True)
class MemoryResetCounts:
    scopes_reset: int
    history_entries: int
    documents: int
    versions: int
    dream_runs: int
    snapshots: int
    jobs_cancelled: int


def compute_dream_history_digest(
    history: tuple[MemoryDreamHistoryRecord, ...],
) -> str:
    if not history or len(history) > 20:
        raise ValueError("Dream history batch is invalid")
    if any(current.sequence >= following.sequence for current, following in zip(history, history[1:], strict=False)):
        raise ValueError("Dream history batch is not strictly ordered")
    payload = [
        {
            "content_digest": item.content_digest,
            "id": str(item.id),
            "sequence": item.sequence,
            "tagged_text": item.tagged_text,
        }
        for item in history
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def memory_document_digest(content: str) -> str:
    if not isinstance(content, str):
        raise TypeError("Memory document must be text")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def memory_document_unified_diff(before: str, after: str) -> str:
    if not isinstance(before, str) or not isinstance(after, str):
        raise TypeError("Memory document diff requires text")
    if before == after:
        return ""
    lines = tuple(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="memory-before.md",
            tofile="memory-after.md",
            lineterm="",
        )
    )
    return "\n".join(lines) + "\n"


class MemoryDocumentRepository:
    def __init__(
        self,
        session: AsyncSession,
        *,
        jobs: JobRepository | None = None,
    ) -> None:
        self.session = session
        self.jobs = jobs or JobRepository(session)

    @staticmethod
    def _scope_predicates(
        row_type,
        scope: MemoryDocumentScope,
    ) -> tuple[sa.ColumnElement[bool], ...]:
        return (
            row_type.project_id == scope.project_id,
            row_type.owner_user_id == scope.owner_user_id,
            row_type.namespace == scope.namespace,
        )

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
    def _document_record(row: MemoryDocumentRow | None) -> MemoryDocumentRecord:
        if row is None:
            return MemoryDocumentRecord(
                content="",
                content_digest="",
                version=0,
                dream_cursor=0,
                active_dream_job_id=None,
                updated_at=None,
            )
        return MemoryDocumentRecord(
            content=row.content,
            content_digest=row.content_digest,
            version=int(row.version),
            dream_cursor=int(row.dream_cursor),
            active_dream_job_id=row.active_dream_job_id,
            updated_at=row.updated_at,
        )

    async def activate_history(
        self,
        activation: MemoryHistoryActivation,
    ) -> MemoryHistoryActivationResult:
        """Idempotently activate a receipt from one committed checkpoint.

        Account preference is locked and compared before touching history.  A
        reset or preference change therefore permanently stales receipts left
        in older checkpoints.  Existing processing/consumed entries are
        validated as immutable identities and are never moved backwards.
        """

        if type(activation) is not MemoryHistoryActivation:
            raise TypeError("MemoryHistoryActivation is required")
        preference = (
            await self.session.execute(
                sa.select(
                    UserRow.memory_enabled,
                    UserRow.preferences_version,
                )
                .where(UserRow.id == activation.scope.owner_user_id)
                .with_for_update(of=UserRow)
            )
        ).one_or_none()
        if preference is None or not bool(preference.memory_enabled) or int(preference.preferences_version) != activation.preference_version:
            return MemoryHistoryActivationResult(
                status="stale",
                entry_id=None,
            )

        inserted_id = await self.session.scalar(
            pg_insert(MemoryHistoryEntryRow)
            .values(
                project_id=activation.scope.project_id,
                owner_user_id=activation.scope.owner_user_id,
                namespace=activation.scope.namespace,
                thread_id=activation.thread_id,
                source_checkpoint_id=activation.source_checkpoint_id,
                committed_checkpoint_id=activation.committed_checkpoint_id,
                source_digest=activation.source_digest,
                status="pending",
                tagged_text=activation.tagged_text,
                content_digest=activation.content_digest,
                preference_version=activation.preference_version,
                snip_prompt_version=activation.snip_prompt_version,
                summary_model_ref=activation.summary_model_ref,
            )
            .on_conflict_do_nothing(
                index_elements=(
                    MemoryHistoryEntryRow.project_id,
                    MemoryHistoryEntryRow.owner_user_id,
                    MemoryHistoryEntryRow.namespace,
                    MemoryHistoryEntryRow.thread_id,
                    MemoryHistoryEntryRow.source_digest,
                )
            )
            .returning(MemoryHistoryEntryRow.id)
        )
        row = (
            await self.session.execute(
                sa.select(MemoryHistoryEntryRow)
                .where(
                    *self._scope_predicates(
                        MemoryHistoryEntryRow,
                        activation.scope,
                    ),
                    MemoryHistoryEntryRow.thread_id == activation.thread_id,
                    MemoryHistoryEntryRow.source_digest == activation.source_digest,
                )
                .with_for_update(of=MemoryHistoryEntryRow)
            )
        ).scalar_one_or_none()
        if row is None:
            raise MemoryDocumentConflict("Memory history activation disappeared")
        self._validate_activated_history(row, activation)
        await self.session.flush()
        if inserted_id is not None:
            return MemoryHistoryActivationResult(
                status="created",
                entry_id=row.id,
            )
        if row.status not in {"pending", "processing", "consumed"}:
            raise MemoryDocumentConflict("Memory history status is invalid")
        return MemoryHistoryActivationResult(
            status=row.status,
            entry_id=row.id,
        )

    @staticmethod
    def _validate_activated_history(
        row: MemoryHistoryEntryRow,
        activation: MemoryHistoryActivation,
    ) -> None:
        # ``committed_checkpoint_id`` intentionally keeps the first successful
        # child checkpoint. A retry from the same source may commit another
        # child while still carrying the identical source identity.
        if (
            row.project_id != activation.scope.project_id
            or row.owner_user_id != activation.scope.owner_user_id
            or row.namespace != activation.scope.namespace
            or row.thread_id != activation.thread_id
            or row.source_checkpoint_id != activation.source_checkpoint_id
            or row.source_digest != activation.source_digest
            or row.content_digest != activation.content_digest
            or int(row.preference_version) != activation.preference_version
            or row.snip_prompt_version != activation.snip_prompt_version
            or row.summary_model_ref != activation.summary_model_ref
            or row.status not in {"pending", "processing", "consumed"}
            or (row.status in {"pending", "processing"} and row.tagged_text != activation.tagged_text)
            or (row.status == "consumed" and row.tagged_text is not None)
        ):
            raise MemoryDocumentConflict("Memory history receipt conflicts")

    async def read_state(
        self,
        scope: MemoryDocumentScope,
        *,
        for_update: bool = False,
    ) -> MemoryDocumentState:
        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        statement = sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope))
        if for_update:
            statement = statement.with_for_update(of=MemoryDocumentRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        pending_count = int(
            await self.session.scalar(
                sa.select(sa.func.count())
                .select_from(MemoryHistoryEntryRow)
                .where(
                    *self._scope_predicates(MemoryHistoryEntryRow, scope),
                    MemoryHistoryEntryRow.status == "pending",
                )
            )
            or 0
        )
        return MemoryDocumentState(
            document=self._document_record(row),
            pending_count=pending_count,
        )

    async def list_versions(
        self,
        scope: MemoryDocumentScope,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MemoryDocumentVersionRecord, ...]:
        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        if not 1 <= limit <= 100 or not 0 <= offset <= 10_000:
            raise ValueError("Memory version pagination is invalid")
        rows = (await self.session.execute(sa.select(MemoryDocumentVersionRow).where(*self._scope_predicates(MemoryDocumentVersionRow, scope)).order_by(MemoryDocumentVersionRow.version.desc()).limit(limit).offset(offset))).scalars()
        return tuple(self._version_record(row) for row in rows)

    async def read_version(
        self,
        scope: MemoryDocumentScope,
        version: int,
    ) -> MemoryDocumentVersionRecord:
        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        if version < 1:
            raise MemoryDocumentNotFound
        row = (
            await self.session.execute(
                sa.select(MemoryDocumentVersionRow).where(
                    *self._scope_predicates(MemoryDocumentVersionRow, scope),
                    MemoryDocumentVersionRow.version == version,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise MemoryDocumentNotFound
        return self._version_record(row)

    async def list_due_scopes(
        self,
        *,
        now: datetime,
        interval_minutes: int,
        limit: int = 100,
    ) -> tuple[MemoryDocumentScope, ...]:
        if not isinstance(now, datetime) or now.tzinfo is None or type(interval_minutes) is not int or not 15 <= interval_minutes <= 1_440 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Dream schedule boundary is invalid")
        cutoff = now - timedelta(minutes=interval_minutes)
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
                .having(due_anchor <= cutoff)
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
        cutoff = now - timedelta(minutes=interval_minutes)
        document = MemoryDocumentRow
        history = MemoryHistoryEntryRow
        oldest_pending = sa.func.min(history.created_at)
        latest_dream_activity = self._latest_dream_activity(history)
        due_anchor = sa.func.greatest(
            oldest_pending,
            sa.func.coalesce(document.updated_at, oldest_pending),
            sa.func.coalesce(latest_dream_activity, oldest_pending),
        )
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
            .having(due_anchor <= cutoff)
            .limit(1)
        )
        return row is True

    async def admit_dream(
        self,
        scope: MemoryDocumentScope,
        *,
        trigger: MemoryDreamTrigger,
        frozen: MemoryDreamFrozenRuntime,
        initial_content: str,
        now: datetime,
        max_attempts: int = 3,
    ) -> MemoryDreamAdmissionRecord:
        if (
            type(scope) is not MemoryDocumentScope
            or trigger not in {"auto_dream", "manual_dream"}
            or type(frozen) is not MemoryDreamFrozenRuntime
            or not isinstance(initial_content, str)
            or not initial_content
            or len(initial_content) > 16_000
            or not isinstance(now, datetime)
            or now.tzinfo is None
            or type(max_attempts) is not int
            or not 1 <= max_attempts <= 20
        ):
            raise ValueError("Dream admission input is invalid")
        await self.session.execute(
            pg_insert(MemoryDocumentRow)
            .values(
                project_id=scope.project_id,
                owner_user_id=scope.owner_user_id,
                namespace=scope.namespace,
                content=initial_content,
                content_digest=memory_document_digest(initial_content),
                version=0,
                dream_cursor=0,
                active_dream_job_id=None,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    MemoryDocumentRow.project_id,
                    MemoryDocumentRow.owner_user_id,
                    MemoryDocumentRow.namespace,
                ]
            )
        )
        document = (await self.session.execute(sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope)).with_for_update(of=MemoryDocumentRow))).scalar_one()
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
                    .limit(20)
                    .with_for_update(of=MemoryHistoryEntryRow)
                )
            ).scalars()
        )[:20]
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
            )
            for row in rows
        )
        if any(item.tagged_text is None for item in history):
            raise MemoryDocumentConflict("Dream pending history is invalid")
        history_digest = compute_dream_history_digest(history)
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
                priority=10 if trigger == "manual_dream" else 0,
            )
        )
        run = MemoryDreamRunRow(
            job_id=job_id,
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            namespace=scope.namespace,
            trigger=trigger,
            history_from=history[0].sequence,
            history_to=history[-1].sequence,
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
            )
            for row in history_rows
        )
        return MemoryDreamWork(
            job_id=run.job_id,
            project_id=run.project_id,
            owner_user_id=run.owner_user_id,
            namespace=run.namespace,
            trigger=run.trigger,
            history_from=int(run.history_from),
            history_to=int(run.history_to),
            history_count=int(run.history_count),
            history_digest=run.history_digest,
            base_document_version=int(run.base_document_version),
            base_content=document.content if run.result_version is None else (await self.read_version(scope, int(run.result_version))).content,
            base_content_digest=run.base_content_digest,
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
        content: str,
        now: datetime,
    ) -> MemoryDocumentVersionRecord:
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
            or run.history_digest != expected_history_digest
            or int(run.history_count) != len(history)
            or not history
            or int(run.history_from) != history[0].sequence
            or int(run.history_to) != history[-1].sequence
            or any(row.status != "processing" for row in history_rows)
            or compute_dream_history_digest(history) != expected_history_digest
        ):
            raise MemoryDocumentConflict("Dream settlement contract changed")
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
            created_at=now,
        )
        self.session.add(version)
        for row in history_rows:
            row.status = "consumed"
            row.tagged_text = None
            row.consumed_at = now
        document.content = content
        document.content_digest = content_digest
        document.version = next_version
        document.dream_cursor = max(int(document.dream_cursor), int(run.history_to))
        document.active_dream_job_id = None
        document.updated_at = now
        run.result_version = next_version
        run.completed_at = now
        await self.session.flush()
        if not await self.jobs.settle_success(
            job_id,
            lease_token=lease_token,
            now=now,
        ):
            raise MemoryDocumentConflict("Dream Job lease changed")
        await self.session.flush()
        return self._version_record(version)

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
    ) -> bool:
        if type(retryable) is not bool:
            raise TypeError("Dream retryable flag must be a boolean")
        completed_version = await self.session.scalar(
            sa.select(MemoryDreamRunRow.result_version).where(
                MemoryDreamRunRow.job_id == job_id,
                *self._scope_predicates(MemoryDreamRunRow, scope),
            )
        )
        if completed_version is not None:
            return True
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
            raise MemoryDocumentConflict("Dream Job lease changed")
        job_status = await self.session.scalar(sa.select(JobRow.status).where(JobRow.id == job_id))
        if job_status == "retry_wait":
            # A retry owns the same frozen batch.  Keep both the document's
            # active pointer and the history rows in processing so no competing
            # Dream can consume or mutate them between attempts.
            return True
        if job_status not in {"cancelled", "dead", "failed"}:
            raise MemoryDocumentConflict("Dream Job terminal state is invalid")

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
        if run is not None and run.result_version is not None:
            return True
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
        if document is not None and document.active_dream_job_id == job_id:
            document.active_dream_job_id = None
        await self.session.flush()
        return True

    async def restore_version(
        self,
        scope: MemoryDocumentScope,
        *,
        target_version: int,
        expected_current_version: int,
        now: datetime,
    ) -> MemoryDocumentVersionRecord:
        if type(target_version) is not int or target_version < 1 or type(expected_current_version) is not int or expected_current_version < 0 or not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("Memory restore input is invalid")
        document = (await self.session.execute(sa.select(MemoryDocumentRow).where(*self._scope_predicates(MemoryDocumentRow, scope)).with_for_update(of=MemoryDocumentRow))).scalar_one_or_none()
        if document is None:
            raise MemoryDocumentNotFound
        if document.active_dream_job_id is not None or int(document.version) != expected_current_version:
            raise MemoryDocumentConflict("Memory restore CAS conflict")
        target = (
            await self.session.execute(
                sa.select(MemoryDocumentVersionRow).where(
                    *self._scope_predicates(MemoryDocumentVersionRow, scope),
                    MemoryDocumentVersionRow.version == target_version,
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise MemoryDocumentNotFound
        next_version = int(document.version) + 1
        restored = MemoryDocumentVersionRow(
            project_id=scope.project_id,
            owner_user_id=scope.owner_user_id,
            namespace=scope.namespace,
            version=next_version,
            content=target.content,
            content_digest=target.content_digest,
            unified_diff=memory_document_unified_diff(
                document.content,
                target.content,
            ),
            trigger="restore",
            dream_job_id=None,
            history_from=None,
            history_to=None,
            history_count=None,
            prompt_version=None,
            model_ref=None,
            created_at=now,
        )
        self.session.add(restored)
        document.content = target.content
        document.content_digest = target.content_digest
        document.version = next_version
        document.updated_at = now
        await self.session.flush()
        return self._version_record(restored)

    @staticmethod
    def _version_record(
        row: MemoryDocumentVersionRow,
    ) -> MemoryDocumentVersionRecord:
        return MemoryDocumentVersionRecord(
            version=int(row.version),
            content=row.content,
            content_digest=row.content_digest,
            unified_diff=row.unified_diff,
            trigger=row.trigger,
            dream_job_id=row.dream_job_id,
            history_from=(None if row.history_from is None else int(row.history_from)),
            history_to=None if row.history_to is None else int(row.history_to),
            history_count=(None if row.history_count is None else int(row.history_count)),
            prompt_version=row.prompt_version,
            model_ref=row.model_ref,
            created_at=row.created_at,
        )

    async def reset_owner(
        self,
        owner_user_id: str,
        *,
        now: datetime,
    ) -> MemoryResetCounts:
        try:
            owner_user_id = str(uuid.UUID(str(owner_user_id)))
        except (TypeError, ValueError):
            raise ValueError("Memory reset requires an owner UUID") from None
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Memory reset time must be timezone-aware")

        scope_rows = set(
            (
                await self.session.execute(
                    sa.select(
                        MemoryDocumentRow.project_id,
                        MemoryDocumentRow.namespace,
                    ).where(MemoryDocumentRow.owner_user_id == owner_user_id)
                )
            ).all()
        )
        scope_rows.update(
            (
                await self.session.execute(
                    sa.select(
                        MemoryHistoryEntryRow.project_id,
                        MemoryHistoryEntryRow.namespace,
                    )
                    .where(MemoryHistoryEntryRow.owner_user_id == owner_user_id)
                    .distinct()
                )
            ).all()
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
            "snapshots": await self._count(
                RunMemoryContextSnapshotRow,
                owner_user_id,
            ),
        }

        active_jobs = tuple(
            (
                await self.session.execute(
                    sa.select(JobRow.id, JobRow.project_id, JobRow.owner_user_id)
                    .where(
                        JobRow.owner_user_id == owner_user_id,
                        JobRow.job_type == "memory_dream",
                        JobRow.status.in_(("queued", "leased", "running", "retry_wait")),
                    )
                    .with_for_update(of=JobRow)
                )
            ).all()
        )
        jobs = JobRepository(self.session)
        jobs_cancelled = 0
        for job_id, project_id, job_owner_user_id in active_jobs:
            scope = JobScope(project_id, job_owner_user_id)
            requested = await jobs.request_cancel(
                scope,
                job_id,
                reason="memory_reset",
                now=now,
            )
            if requested:
                jobs_cancelled += 1
            await jobs.settle_requested_cancel(scope, job_id, now=now)

        await self.session.execute(sa.delete(RunMemoryContextSnapshotRow).where(RunMemoryContextSnapshotRow.owner_user_id == owner_user_id))
        await self.session.execute(sa.delete(MemoryHistoryEntryRow).where(MemoryHistoryEntryRow.owner_user_id == owner_user_id))
        await self.session.execute(sa.delete(MemoryDocumentRow).where(MemoryDocumentRow.owner_user_id == owner_user_id))
        await self.session.flush()
        return MemoryResetCounts(
            scopes_reset=len(scope_rows),
            history_entries=counts["history_entries"],
            documents=counts["documents"],
            versions=counts["versions"],
            dream_runs=counts["dream_runs"],
            snapshots=counts["snapshots"],
            jobs_cancelled=jobs_cancelled,
        )

    async def _count(self, row_type, owner_user_id: str) -> int:
        return int(await self.session.scalar(sa.select(sa.func.count()).select_from(row_type).where(row_type.owner_user_id == owner_user_id)) or 0)


__all__ = [
    "DEFAULT_MEMORY_NAMESPACE",
    "MemoryDocumentConflict",
    "MemoryDocumentNotFound",
    "MemoryDocumentRecord",
    "MemoryDocumentRepository",
    "MemoryDocumentScope",
    "MemoryDocumentState",
    "MemoryDocumentVersionRecord",
    "MemoryDreamAdmissionDisposition",
    "MemoryDreamAdmissionRecord",
    "MemoryDreamFrozenRuntime",
    "MemoryDreamHistoryRecord",
    "MemoryDreamTrigger",
    "MemoryDreamWork",
    "MemoryHistoryActivation",
    "MemoryHistoryActivationResult",
    "MemoryHistoryActivationStatus",
    "MemoryResetCounts",
    "compute_dream_history_digest",
    "memory_document_digest",
    "memory_document_unified_diff",
]
