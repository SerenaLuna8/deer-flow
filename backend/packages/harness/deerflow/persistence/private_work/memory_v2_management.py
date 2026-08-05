"""Session-bound management, erasure, and purge operations for Memory v2."""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.jobs.model import JobRow
from deerflow.persistence.jobs.sql import JobRepository, JobScope
from deerflow.persistence.private_work.memory_v2_model import (
    MemoryCandidateRow,
    MemoryConsolidationGenerationRow,
    MemoryContextSummaryRow,
    MemoryExtractionGenerationRow,
    MemoryFactEvidenceRow,
    MemoryFactRevisionRow,
    MemoryFactRow,
    MemorySourceBatchRow,
    MemorySourceItemRow,
    MemorySuppressionRow,
    RunMemoryContextItemRow,
    RunMemoryContextSnapshotRow,
)
from deerflow.runtime.private_scope import PrivateResourceScope

MemoryCandidateStatus = Literal["pending", "accepted", "rejected", "superseded"]
MemoryFactStatus = Literal["active", "disabled", "superseded", "deleted"]


class MemoryV2ManagementError(RuntimeError):
    """Base management invariant failure."""


class MemoryV2ManagementNotFound(MemoryV2ManagementError):
    """The scoped Memory resource does not exist or is no longer visible."""


class MemoryV2ManagementConflict(MemoryV2ManagementError):
    """The supplied optimistic state no longer matches PostgreSQL."""


class MemoryV2ManagementInvalid(MemoryV2ManagementError):
    """The requested state transition is invalid."""


@dataclass(frozen=True, slots=True)
class MemoryV2CandidateView:
    id: uuid.UUID
    candidate_type: str
    content: str | None
    confidence: float
    retention_class: str
    sensitivity: str
    status: MemoryCandidateStatus
    decision_reason: str | None
    decided_at: datetime | None
    content_erased_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryV2RevisionView:
    id: uuid.UUID
    fact_id: uuid.UUID
    revision_number: int
    revision_sequence: int
    content: str | None
    content_digest: str
    category: str
    confidence: float
    valid_from: datetime | None
    valid_to: datetime | None
    last_confirmed_at: datetime | None
    changed_by: str
    source_candidate_id: uuid.UUID | None
    supersedes_revision_id: uuid.UUID | None
    change_reason: str | None
    content_erased_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryV2FactView:
    id: uuid.UUID
    fact_kind: str
    status: MemoryFactStatus
    version: int
    disabled_at: datetime | None
    superseded_at: datetime | None
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime
    current_revision: MemoryV2RevisionView


@dataclass(frozen=True, slots=True)
class MemoryV2EvidenceView:
    id: uuid.UUID
    fact_id: uuid.UUID
    revision_id: uuid.UUID
    source_candidate_id: uuid.UUID | None
    source_item_id: uuid.UUID | None
    thread_id: str | None
    run_id: str | None
    run_event_sequence: int | None
    evidence_excerpt: str | None
    trust_class: str
    source_erased_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryV2FactDetail:
    fact: MemoryV2FactView
    revisions: tuple[MemoryV2RevisionView, ...]
    evidence: tuple[MemoryV2EvidenceView, ...]


@dataclass(frozen=True, slots=True)
class MemoryV2HardForgetResult:
    fact_id: uuid.UUID
    version: int
    status: Literal["deleted"]
    erased_candidates: int
    erased_revisions: int
    erased_evidence: int
    erased_source_items: int


@dataclass(frozen=True, slots=True)
class MemoryV2ResetResult:
    source_batches: int
    candidates: int
    facts: int
    snapshots: int
    jobs_cancelled: int


def _scope(scope: PrivateResourceScope) -> tuple[uuid.UUID, str]:
    if type(scope) is not PrivateResourceScope:
        raise MemoryV2ManagementInvalid
    try:
        return uuid.UUID(scope.project_id), str(uuid.UUID(scope.owner_user_id))
    except (TypeError, ValueError):
        raise MemoryV2ManagementInvalid from None


def _namespace(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= 128:
        raise MemoryV2ManagementInvalid
    return value


def _page(limit: int, offset: int) -> tuple[int, int]:
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise MemoryV2ManagementInvalid
    return limit, offset


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MemoryV2ManagementInvalid
    return value.astimezone(UTC)


def _content(value: str) -> str:
    if not isinstance(value, str):
        raise MemoryV2ManagementInvalid
    normalized = value.strip()
    if not 1 <= len(normalized) <= 16_000:
        raise MemoryV2ManagementInvalid
    return normalized


def _category(value: str) -> str:
    if not isinstance(value, str):
        raise MemoryV2ManagementInvalid
    normalized = value.strip()
    if not 1 <= len(normalized) <= 32:
        raise MemoryV2ManagementInvalid
    return normalized


def _optional_filter(value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MemoryV2ManagementInvalid
    normalized = value.strip()
    if not 1 <= len(normalized) <= max_length:
        raise MemoryV2ManagementInvalid
    return normalized


def _contains_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _confidence(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MemoryV2ManagementInvalid
    normalized = float(value)
    if not math.isfinite(normalized) or not 0 <= normalized <= 1:
        raise MemoryV2ManagementInvalid
    return normalized


def _candidate_view(row: MemoryCandidateRow) -> MemoryV2CandidateView:
    return MemoryV2CandidateView(
        id=row.id,
        candidate_type=row.candidate_type,
        content=row.content,
        confidence=float(row.confidence),
        retention_class=row.retention_class,
        sensitivity=row.sensitivity,
        status=row.status,
        decision_reason=row.decision_reason,
        decided_at=row.decided_at,
        content_erased_at=row.content_erased_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _revision_view(row: MemoryFactRevisionRow) -> MemoryV2RevisionView:
    return MemoryV2RevisionView(
        id=row.id,
        fact_id=row.fact_id,
        revision_number=int(row.revision_number),
        revision_sequence=int(row.revision_sequence),
        content=row.content,
        content_digest=row.content_digest,
        category=row.category,
        confidence=float(row.confidence),
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        last_confirmed_at=row.last_confirmed_at,
        changed_by=row.changed_by,
        source_candidate_id=row.source_candidate_id,
        supersedes_revision_id=row.supersedes_revision_id,
        change_reason=row.change_reason,
        content_erased_at=row.content_erased_at,
        created_at=row.created_at,
    )


def _fact_view(
    row: MemoryFactRow,
    revision: MemoryFactRevisionRow,
) -> MemoryV2FactView:
    return MemoryV2FactView(
        id=row.id,
        fact_kind=row.fact_kind,
        status=row.status,
        version=int(row.version),
        disabled_at=row.disabled_at,
        superseded_at=row.superseded_at,
        deleted_at=row.deleted_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        current_revision=_revision_view(revision),
    )


def _evidence_view(row: MemoryFactEvidenceRow) -> MemoryV2EvidenceView:
    return MemoryV2EvidenceView(
        id=row.id,
        fact_id=row.fact_id,
        revision_id=row.revision_id,
        source_candidate_id=row.source_candidate_id,
        source_item_id=row.source_item_id,
        thread_id=row.thread_id,
        run_id=row.run_id,
        run_event_sequence=row.run_event_sequence,
        evidence_excerpt=row.evidence_excerpt,
        trust_class=row.trust_class,
        source_erased_at=row.source_erased_at,
        created_at=row.created_at,
    )


class MemoryV2ManagementRepository:
    """Manage one exact owner-private Memory v2 scope in the caller transaction."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        jobs: JobRepository | None = None,
    ) -> None:
        self.session = session
        self.jobs = jobs or JobRepository(session)

    @staticmethod
    def _predicates(
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        row_type,
    ) -> tuple[object, ...]:
        return (
            row_type.project_id == project_id,
            row_type.owner_user_id == owner_user_id,
            row_type.namespace == namespace,
        )

    async def list_facts(
        self,
        scope: PrivateResourceScope,
        *,
        namespace: str,
        statuses: tuple[Literal["active", "disabled"], ...],
        limit: int,
        offset: int,
        query: str | None = None,
        category: str | None = None,
    ) -> tuple[MemoryV2FactView, ...]:
        project_id, owner_user_id = _scope(scope)
        namespace = _namespace(namespace)
        limit, offset = _page(limit, offset)
        query = _optional_filter(query, max_length=200)
        category = _optional_filter(category, max_length=32)
        if not statuses or any(status not in {"active", "disabled"} for status in statuses):
            raise MemoryV2ManagementInvalid
        statement = (
            select(MemoryFactRow, MemoryFactRevisionRow)
            .join(
                MemoryFactRevisionRow,
                (MemoryFactRevisionRow.project_id == MemoryFactRow.project_id)
                & (MemoryFactRevisionRow.owner_user_id == MemoryFactRow.owner_user_id)
                & (MemoryFactRevisionRow.namespace == MemoryFactRow.namespace)
                & (MemoryFactRevisionRow.fact_id == MemoryFactRow.id)
                & (MemoryFactRevisionRow.id == MemoryFactRow.current_revision_id),
            )
            .where(
                *self._predicates(
                    project_id,
                    owner_user_id,
                    namespace,
                    MemoryFactRow,
                ),
                MemoryFactRow.status.in_(statuses),
            )
        )
        if query is not None:
            pattern = _contains_pattern(query)
            statement = statement.where(
                or_(
                    MemoryFactRevisionRow.content.ilike(pattern, escape="\\"),
                    MemoryFactRevisionRow.category.ilike(pattern, escape="\\"),
                )
            )
        if category is not None:
            statement = statement.where(MemoryFactRevisionRow.category == category)
        rows = (await self.session.execute(statement.order_by(MemoryFactRow.updated_at.desc(), MemoryFactRow.id).limit(limit).offset(offset))).all()
        return tuple(_fact_view(fact, revision) for fact, revision in rows)

    async def list_candidates(
        self,
        scope: PrivateResourceScope,
        *,
        namespace: str,
        statuses: tuple[MemoryCandidateStatus, ...],
        limit: int,
        offset: int,
    ) -> tuple[MemoryV2CandidateView, ...]:
        project_id, owner_user_id = _scope(scope)
        namespace = _namespace(namespace)
        limit, offset = _page(limit, offset)
        allowed = {"pending", "accepted", "rejected", "superseded"}
        if not statuses or any(status not in allowed for status in statuses):
            raise MemoryV2ManagementInvalid
        rows = (
            (
                await self.session.execute(
                    select(MemoryCandidateRow)
                    .where(
                        *self._predicates(
                            project_id,
                            owner_user_id,
                            namespace,
                            MemoryCandidateRow,
                        ),
                        MemoryCandidateRow.status.in_(statuses),
                    )
                    .order_by(
                        MemoryCandidateRow.created_at.desc(),
                        MemoryCandidateRow.id,
                    )
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return tuple(_candidate_view(row) for row in rows)

    async def get_fact_detail(
        self,
        scope: PrivateResourceScope,
        *,
        namespace: str,
        fact_id: uuid.UUID,
    ) -> MemoryV2FactDetail:
        project_id, owner_user_id = _scope(scope)
        namespace = _namespace(namespace)
        pair = (
            await self.session.execute(
                select(MemoryFactRow, MemoryFactRevisionRow)
                .join(
                    MemoryFactRevisionRow,
                    (MemoryFactRevisionRow.project_id == MemoryFactRow.project_id)
                    & (MemoryFactRevisionRow.owner_user_id == MemoryFactRow.owner_user_id)
                    & (MemoryFactRevisionRow.namespace == MemoryFactRow.namespace)
                    & (MemoryFactRevisionRow.fact_id == MemoryFactRow.id)
                    & (MemoryFactRevisionRow.id == MemoryFactRow.current_revision_id),
                )
                .where(
                    *self._predicates(
                        project_id,
                        owner_user_id,
                        namespace,
                        MemoryFactRow,
                    ),
                    MemoryFactRow.id == fact_id,
                    MemoryFactRow.status.in_(("active", "disabled")),
                )
            )
        ).one_or_none()
        if pair is None:
            raise MemoryV2ManagementNotFound
        fact, current = pair
        revisions = tuple(
            (
                await self.session.execute(
                    select(MemoryFactRevisionRow)
                    .where(
                        *self._predicates(
                            project_id,
                            owner_user_id,
                            namespace,
                            MemoryFactRevisionRow,
                        ),
                        MemoryFactRevisionRow.fact_id == fact_id,
                    )
                    .order_by(
                        MemoryFactRevisionRow.revision_number.desc(),
                        MemoryFactRevisionRow.id,
                    )
                )
            ).scalars()
        )
        evidence = tuple(
            (
                await self.session.execute(
                    select(MemoryFactEvidenceRow)
                    .where(
                        *self._predicates(
                            project_id,
                            owner_user_id,
                            namespace,
                            MemoryFactEvidenceRow,
                        ),
                        MemoryFactEvidenceRow.fact_id == fact_id,
                    )
                    .order_by(
                        MemoryFactEvidenceRow.created_at,
                        MemoryFactEvidenceRow.id,
                    )
                )
            ).scalars()
        )
        return MemoryV2FactDetail(
            fact=_fact_view(fact, current),
            revisions=tuple(_revision_view(row) for row in revisions),
            evidence=tuple(_evidence_view(row) for row in evidence),
        )

    async def _candidate_for_decision(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        candidate_id: uuid.UUID,
        expected_updated_at: datetime,
    ) -> MemoryCandidateRow:
        row = (
            await self.session.execute(
                select(MemoryCandidateRow)
                .where(
                    *self._predicates(
                        project_id,
                        owner_user_id,
                        namespace,
                        MemoryCandidateRow,
                    ),
                    MemoryCandidateRow.id == candidate_id,
                )
                .with_for_update(of=MemoryCandidateRow)
            )
        ).scalar_one_or_none()
        if row is None:
            raise MemoryV2ManagementNotFound
        if row.status != "pending" or _aware(row.updated_at) != _aware(expected_updated_at):
            raise MemoryV2ManagementConflict
        if row.consolidation_generation_id is not None:
            generation = await self.session.scalar(
                select(MemoryConsolidationGenerationRow).where(
                    MemoryConsolidationGenerationRow.project_id == project_id,
                    MemoryConsolidationGenerationRow.owner_user_id == owner_user_id,
                    MemoryConsolidationGenerationRow.namespace == namespace,
                    MemoryConsolidationGenerationRow.id == row.consolidation_generation_id,
                    MemoryConsolidationGenerationRow.fact_committed_at.is_(None),
                )
            )
            if generation is not None:
                raise MemoryV2ManagementConflict
        return row

    async def _next_revision_sequence(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
    ) -> int:
        return (
            int(
                await self.session.scalar(
                    select(
                        func.coalesce(
                            func.max(MemoryFactRevisionRow.revision_sequence),
                            0,
                        )
                    ).where(
                        *self._predicates(
                            project_id,
                            owner_user_id,
                            namespace,
                            MemoryFactRevisionRow,
                        )
                    )
                )
                or 0
            )
            + 1
        )

    async def accept_candidate(
        self,
        scope: PrivateResourceScope,
        *,
        namespace: str,
        candidate_id: uuid.UUID,
        expected_updated_at: datetime,
        now: datetime,
    ) -> MemoryV2FactView:
        project_id, owner_user_id = _scope(scope)
        namespace = _namespace(namespace)
        decided_at = _aware(now)
        candidate = await self._candidate_for_decision(
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            candidate_id=candidate_id,
            expected_updated_at=expected_updated_at,
        )
        if candidate.content is None or candidate.content_erased_at is not None or candidate.sensitivity != "normal":
            raise MemoryV2ManagementInvalid
        source = (
            await self.session.execute(
                select(MemorySourceItemRow, MemorySourceBatchRow)
                .join(
                    MemorySourceBatchRow,
                    (MemorySourceBatchRow.project_id == MemorySourceItemRow.project_id)
                    & (MemorySourceBatchRow.owner_user_id == MemorySourceItemRow.owner_user_id)
                    & (MemorySourceBatchRow.namespace == MemorySourceItemRow.namespace)
                    & (MemorySourceBatchRow.id == MemorySourceItemRow.source_batch_id),
                )
                .where(
                    *self._predicates(
                        project_id,
                        owner_user_id,
                        namespace,
                        MemorySourceItemRow,
                    ),
                    MemorySourceItemRow.id == candidate.source_item_id,
                    MemorySourceItemRow.source_erased_at.is_(None),
                    MemorySourceItemRow.content.is_not(None),
                    MemorySourceBatchRow.suppressed_at.is_(None),
                )
                .with_for_update(
                    of=(MemorySourceItemRow, MemorySourceBatchRow),
                )
            )
        ).one_or_none()
        if source is None:
            raise MemoryV2ManagementConflict
        source_item, source_batch = source
        active_count = int(
            await self.session.scalar(
                select(func.count())
                .select_from(MemoryFactRow)
                .where(
                    *self._predicates(
                        project_id,
                        owner_user_id,
                        namespace,
                        MemoryFactRow,
                    ),
                    MemoryFactRow.status.in_(("active", "disabled")),
                )
            )
            or 0
        )
        if active_count >= 500:
            raise MemoryV2ManagementInvalid
        fact_id = uuid.uuid4()
        revision_id = uuid.uuid4()
        content = _content(candidate.content)
        revision = MemoryFactRevisionRow(
            id=revision_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            fact_id=fact_id,
            revision_number=1,
            revision_sequence=await self._next_revision_sequence(
                project_id=project_id,
                owner_user_id=owner_user_id,
                namespace=namespace,
            ),
            content=content,
            content_digest=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            category=_category(candidate.candidate_type),
            confidence=_confidence(candidate.confidence),
            valid_from=decided_at,
            valid_to=None,
            last_confirmed_at=decided_at,
            changed_by="user",
            source_candidate_id=candidate.id,
            supersedes_revision_id=None,
            change_reason="user_accepted",
            content_erased_at=None,
            created_at=decided_at,
        )
        fact = MemoryFactRow(
            id=fact_id,
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            fact_kind=candidate.candidate_type,
            status="active",
            current_revision_id=revision_id,
            version=1,
            disabled_at=None,
            superseded_at=None,
            deleted_at=None,
            created_at=decided_at,
            updated_at=decided_at,
        )
        evidence = MemoryFactEvidenceRow(
            id=uuid.uuid4(),
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            fact_id=fact_id,
            revision_id=revision_id,
            source_candidate_id=candidate.id,
            source_item_id=source_item.id,
            thread_id=source_batch.thread_id,
            run_id=source_batch.run_id,
            run_event_sequence=source_item.run_event_sequence,
            source_identity_hmac=source_item.content_hmac,
            evidence_excerpt=content[:4_000],
            trust_class="direct",
            source_erased_at=None,
            created_at=decided_at,
        )
        candidate.status = "accepted"
        candidate.decision_reason = "user_accepted"
        candidate.decided_at = decided_at
        candidate.updated_at = decided_at
        # Fact and Revision form an intentionally deferred FK cycle.  Flush
        # that aggregate before inserting Evidence so SQLAlchemy cannot emit
        # the Evidence row before its referenced Revision exists.
        self.session.add_all((fact, revision))
        await self.session.flush()
        self.session.add(evidence)
        await self.session.flush()
        return _fact_view(fact, revision)

    async def reject_candidate(
        self,
        scope: PrivateResourceScope,
        *,
        namespace: str,
        candidate_id: uuid.UUID,
        expected_updated_at: datetime,
        now: datetime,
    ) -> MemoryV2CandidateView:
        project_id, owner_user_id = _scope(scope)
        namespace = _namespace(namespace)
        decided_at = _aware(now)
        candidate = await self._candidate_for_decision(
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            candidate_id=candidate_id,
            expected_updated_at=expected_updated_at,
        )
        candidate.status = "rejected"
        candidate.decision_reason = "user_rejected"
        candidate.decided_at = decided_at
        candidate.updated_at = decided_at
        await self.session.flush()
        return _candidate_view(candidate)

    async def _fact_for_update(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        namespace: str,
        fact_id: uuid.UUID,
        expected_version: int,
    ) -> tuple[MemoryFactRow, MemoryFactRevisionRow]:
        pair = (
            await self.session.execute(
                select(MemoryFactRow, MemoryFactRevisionRow)
                .join(
                    MemoryFactRevisionRow,
                    (MemoryFactRevisionRow.project_id == MemoryFactRow.project_id)
                    & (MemoryFactRevisionRow.owner_user_id == MemoryFactRow.owner_user_id)
                    & (MemoryFactRevisionRow.namespace == MemoryFactRow.namespace)
                    & (MemoryFactRevisionRow.fact_id == MemoryFactRow.id)
                    & (MemoryFactRevisionRow.id == MemoryFactRow.current_revision_id),
                )
                .where(
                    *self._predicates(
                        project_id,
                        owner_user_id,
                        namespace,
                        MemoryFactRow,
                    ),
                    MemoryFactRow.id == fact_id,
                )
                .with_for_update(of=(MemoryFactRow, MemoryFactRevisionRow))
            )
        ).one_or_none()
        if pair is None:
            raise MemoryV2ManagementNotFound
        fact, revision = pair
        if fact.status not in {"active", "disabled"}:
            raise MemoryV2ManagementNotFound
        if type(expected_version) is not int or expected_version < 1 or int(fact.version) != expected_version:
            raise MemoryV2ManagementConflict
        return fact, revision

    async def revise_fact(
        self,
        scope: PrivateResourceScope,
        *,
        namespace: str,
        fact_id: uuid.UUID,
        expected_version: int,
        content: str | None,
        category: str | None,
        confidence: float | None,
        reason: str | None,
        now: datetime,
    ) -> MemoryV2FactView:
        project_id, owner_user_id = _scope(scope)
        namespace = _namespace(namespace)
        changed_at = _aware(now)
        fact, current = await self._fact_for_update(
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            fact_id=fact_id,
            expected_version=expected_version,
        )
        next_content = current.content if content is None else _content(content)
        if next_content is None:
            raise MemoryV2ManagementNotFound
        next_category = current.category if category is None else _category(category)
        next_confidence = float(current.confidence) if confidence is None else _confidence(confidence)
        if reason is not None and (not isinstance(reason, str) or reason != reason.strip() or not 1 <= len(reason) <= 64):
            raise MemoryV2ManagementInvalid
        if next_content == current.content and next_category == current.category and next_confidence == float(current.confidence):
            return _fact_view(fact, current)
        revision = MemoryFactRevisionRow(
            id=uuid.uuid4(),
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            fact_id=fact.id,
            revision_number=int(current.revision_number) + 1,
            revision_sequence=await self._next_revision_sequence(
                project_id=project_id,
                owner_user_id=owner_user_id,
                namespace=namespace,
            ),
            content=next_content,
            content_digest=hashlib.sha256(next_content.encode("utf-8")).hexdigest(),
            category=next_category,
            confidence=next_confidence,
            valid_from=changed_at,
            valid_to=None,
            last_confirmed_at=changed_at,
            changed_by="user",
            source_candidate_id=None,
            supersedes_revision_id=current.id,
            change_reason=reason or "user_edit",
            content_erased_at=None,
            created_at=changed_at,
        )
        current.valid_to = changed_at
        fact.current_revision_id = revision.id
        fact.version = int(fact.version) + 1
        fact.updated_at = changed_at
        self.session.add(revision)
        await self.session.flush()
        return _fact_view(fact, revision)

    async def set_fact_enabled(
        self,
        scope: PrivateResourceScope,
        *,
        namespace: str,
        fact_id: uuid.UUID,
        expected_version: int,
        enabled: bool,
        now: datetime,
    ) -> MemoryV2FactView:
        if type(enabled) is not bool:
            raise MemoryV2ManagementInvalid
        project_id, owner_user_id = _scope(scope)
        namespace = _namespace(namespace)
        changed_at = _aware(now)
        fact, current = await self._fact_for_update(
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            fact_id=fact_id,
            expected_version=expected_version,
        )
        desired = "active" if enabled else "disabled"
        if fact.status == desired:
            return _fact_view(fact, current)
        if enabled and fact.status != "disabled":
            raise MemoryV2ManagementInvalid
        if not enabled and fact.status != "active":
            raise MemoryV2ManagementInvalid
        fact.status = desired
        fact.disabled_at = None if enabled else changed_at
        fact.version = int(fact.version) + 1
        fact.updated_at = changed_at
        await self.session.flush()
        return _fact_view(fact, current)

    async def _request_job_cancellations(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        job_ids: tuple[uuid.UUID, ...],
        reason: str,
        now: datetime,
    ) -> None:
        scope = JobScope(project_id, owner_user_id)
        for job_id in sorted(set(job_ids), key=str):
            if await self.jobs.request_cancel(
                scope,
                job_id,
                reason=reason,
                now=now,
            ):
                await self.jobs.settle_requested_cancel(
                    scope,
                    job_id,
                    now=now,
                )

    async def hard_forget_fact(
        self,
        scope: PrivateResourceScope,
        *,
        namespace: str,
        fact_id: uuid.UUID,
        expected_version: int,
        lineage_identity_hmac: str,
        lineage_hmac_key_version: str,
        now: datetime,
    ) -> MemoryV2HardForgetResult:
        project_id, owner_user_id = _scope(scope)
        namespace = _namespace(namespace)
        forgotten_at = _aware(now)
        if (
            not isinstance(lineage_identity_hmac, str)
            or len(lineage_identity_hmac) != 64
            or any(character not in "0123456789abcdef" for character in lineage_identity_hmac)
            or not isinstance(lineage_hmac_key_version, str)
            or not 1 <= len(lineage_hmac_key_version) <= 64
        ):
            raise MemoryV2ManagementInvalid
        fact, _current = await self._fact_for_update(
            project_id=project_id,
            owner_user_id=owner_user_id,
            namespace=namespace,
            fact_id=fact_id,
            expected_version=expected_version,
        )
        revisions = tuple(
            (
                await self.session.execute(
                    select(MemoryFactRevisionRow)
                    .where(
                        *self._predicates(
                            project_id,
                            owner_user_id,
                            namespace,
                            MemoryFactRevisionRow,
                        ),
                        MemoryFactRevisionRow.fact_id == fact.id,
                    )
                    .order_by(MemoryFactRevisionRow.id)
                    .with_for_update(of=MemoryFactRevisionRow)
                )
            ).scalars()
        )
        evidence = tuple(
            (
                await self.session.execute(
                    select(MemoryFactEvidenceRow)
                    .where(
                        *self._predicates(
                            project_id,
                            owner_user_id,
                            namespace,
                            MemoryFactEvidenceRow,
                        ),
                        MemoryFactEvidenceRow.fact_id == fact.id,
                    )
                    .order_by(MemoryFactEvidenceRow.id)
                    .with_for_update(of=MemoryFactEvidenceRow)
                )
            ).scalars()
        )
        source_item_ids = {row.source_item_id for row in evidence if row.source_item_id is not None}
        candidate_ids = {row.source_candidate_id for row in (*revisions, *evidence) if row.source_candidate_id is not None}
        if source_item_ids:
            candidate_ids.update(
                (
                    await self.session.execute(
                        select(MemoryCandidateRow.id).where(
                            *self._predicates(
                                project_id,
                                owner_user_id,
                                namespace,
                                MemoryCandidateRow,
                            ),
                            MemoryCandidateRow.source_item_id.in_(source_item_ids),
                        )
                    )
                ).scalars()
            )
        candidates = tuple(
            (
                await self.session.execute(
                    select(MemoryCandidateRow)
                    .where(
                        *self._predicates(
                            project_id,
                            owner_user_id,
                            namespace,
                            MemoryCandidateRow,
                        ),
                        MemoryCandidateRow.id.in_(candidate_ids or {uuid.uuid4()}),
                    )
                    .order_by(MemoryCandidateRow.id)
                    .with_for_update(of=MemoryCandidateRow)
                )
            ).scalars()
        )
        source_items = tuple(
            (
                await self.session.execute(
                    select(MemorySourceItemRow)
                    .where(
                        *self._predicates(
                            project_id,
                            owner_user_id,
                            namespace,
                            MemorySourceItemRow,
                        ),
                        MemorySourceItemRow.id.in_(source_item_ids or {uuid.uuid4()}),
                    )
                    .order_by(MemorySourceItemRow.id)
                    .with_for_update(of=MemorySourceItemRow)
                )
            ).scalars()
        )
        batch_ids = {row.source_batch_id for row in source_items}
        batches = tuple(
            (
                await self.session.execute(
                    select(MemorySourceBatchRow)
                    .where(
                        *self._predicates(
                            project_id,
                            owner_user_id,
                            namespace,
                            MemorySourceBatchRow,
                        ),
                        MemorySourceBatchRow.id.in_(batch_ids or {uuid.uuid4()}),
                    )
                    .order_by(MemorySourceBatchRow.id)
                    .with_for_update(of=MemorySourceBatchRow)
                )
            ).scalars()
        )
        batch_key_versions = {row.id: row.source_hmac_key_version for row in batches}
        suppressions = [
            {
                "id": uuid.uuid4(),
                "project_id": project_id,
                "owner_user_id": owner_user_id,
                "namespace": namespace,
                "suppression_kind": "fact_lineage",
                "identity_hmac": lineage_identity_hmac,
                "hmac_key_version": lineage_hmac_key_version,
                "reason": "hard_forget",
                "created_at": forgotten_at,
            }
        ]
        suppressions.extend(
            {
                "id": uuid.uuid4(),
                "project_id": project_id,
                "owner_user_id": owner_user_id,
                "namespace": namespace,
                "suppression_kind": "source",
                "identity_hmac": item.content_hmac,
                "hmac_key_version": batch_key_versions[item.source_batch_id],
                "reason": "hard_forget",
                "created_at": forgotten_at,
            }
            for item in source_items
        )
        await self.session.execute(
            insert(MemorySuppressionRow)
            .values(suppressions)
            .on_conflict_do_nothing(
                constraint="uq_memory_suppressions_identity",
            )
        )
        extraction_job_ids = tuple(
            (
                await self.session.execute(
                    select(MemoryExtractionGenerationRow.job_id).where(
                        *self._predicates(
                            project_id,
                            owner_user_id,
                            namespace,
                            MemoryExtractionGenerationRow,
                        ),
                        MemoryExtractionGenerationRow.source_batch_id.in_(batch_ids or {uuid.uuid4()}),
                    )
                )
            ).scalars()
        )
        consolidation_ids = {row.consolidation_generation_id for row in candidates if row.consolidation_generation_id is not None}
        consolidation_job_ids = tuple(
            (
                await self.session.execute(
                    select(MemoryConsolidationGenerationRow.job_id).where(
                        *self._predicates(
                            project_id,
                            owner_user_id,
                            namespace,
                            MemoryConsolidationGenerationRow,
                        ),
                        MemoryConsolidationGenerationRow.id.in_(consolidation_ids or {uuid.uuid4()}),
                    )
                )
            ).scalars()
        )
        await self._request_job_cancellations(
            project_id=project_id,
            owner_user_id=owner_user_id,
            job_ids=extraction_job_ids + consolidation_job_ids,
            reason="memory_hard_forget",
            now=forgotten_at,
        )
        if consolidation_ids:
            await self.session.execute(
                update(MemoryCandidateRow)
                .where(
                    *self._predicates(
                        project_id,
                        owner_user_id,
                        namespace,
                        MemoryCandidateRow,
                    ),
                    MemoryCandidateRow.consolidation_generation_id.in_(consolidation_ids),
                )
                .values(consolidation_generation_id=None)
            )
            await self.session.execute(
                delete(MemoryConsolidationGenerationRow).where(
                    *self._predicates(
                        project_id,
                        owner_user_id,
                        namespace,
                        MemoryConsolidationGenerationRow,
                    ),
                    MemoryConsolidationGenerationRow.id.in_(consolidation_ids),
                )
            )
        for batch in batches:
            batch.suppressed_at = batch.suppressed_at or forgotten_at
            batch.suppression_reason = batch.suppression_reason or "hard_forget"
        for item in source_items:
            item.content = None
            item.source_erased_at = item.source_erased_at or forgotten_at
        for candidate in candidates:
            candidate.content = None
            candidate.content_erased_at = candidate.content_erased_at or forgotten_at
            candidate.consolidation_generation_id = None
            if candidate.status == "pending":
                candidate.status = "rejected"
                candidate.decision_reason = "hard_forget"
                candidate.decided_at = forgotten_at
            elif candidate.status == "accepted":
                candidate.status = "superseded"
                candidate.decision_reason = "hard_forget"
                candidate.decided_at = candidate.decided_at or forgotten_at
            candidate.updated_at = forgotten_at
        for revision in revisions:
            revision.content = None
            revision.content_erased_at = revision.content_erased_at or forgotten_at
            revision.source_candidate_id = None
            revision.change_reason = "hard_forget"
        affected_evidence = {row.id: row for row in evidence}
        related_evidence_predicates = []
        if source_item_ids:
            related_evidence_predicates.append(
                MemoryFactEvidenceRow.source_item_id.in_(source_item_ids),
            )
        if candidate_ids:
            related_evidence_predicates.append(
                MemoryFactEvidenceRow.source_candidate_id.in_(candidate_ids),
            )
        if related_evidence_predicates:
            related = tuple(
                (
                    await self.session.execute(
                        select(MemoryFactEvidenceRow)
                        .where(
                            *self._predicates(
                                project_id,
                                owner_user_id,
                                namespace,
                                MemoryFactEvidenceRow,
                            ),
                            or_(*related_evidence_predicates),
                        )
                        .order_by(MemoryFactEvidenceRow.id)
                        .with_for_update(of=MemoryFactEvidenceRow)
                    )
                ).scalars()
            )
            affected_evidence.update((row.id, row) for row in related)
        if candidate_ids:
            await self.session.execute(
                update(MemoryFactRevisionRow)
                .where(
                    *self._predicates(
                        project_id,
                        owner_user_id,
                        namespace,
                        MemoryFactRevisionRow,
                    ),
                    MemoryFactRevisionRow.source_candidate_id.in_(candidate_ids),
                )
                .values(source_candidate_id=None)
            )
        for row in affected_evidence.values():
            row.source_candidate_id = None
            row.source_item_id = None
            row.thread_id = None
            row.run_id = None
            row.run_event_sequence = None
            row.evidence_excerpt = None
            row.source_erased_at = row.source_erased_at or forgotten_at
        await self.session.execute(
            update(MemoryContextSummaryRow)
            .where(
                *self._predicates(
                    project_id,
                    owner_user_id,
                    namespace,
                    MemoryContextSummaryRow,
                ),
                MemoryContextSummaryRow.summary_text.is_not(None),
            )
            .values(summary_text=None, content_erased_at=forgotten_at)
        )
        await self.session.execute(
            delete(RunMemoryContextItemRow).where(
                *self._predicates(
                    project_id,
                    owner_user_id,
                    namespace,
                    RunMemoryContextItemRow,
                ),
                RunMemoryContextItemRow.fact_id == fact.id,
            )
        )
        await self.session.execute(
            update(RunMemoryContextSnapshotRow)
            .where(
                *self._predicates(
                    project_id,
                    owner_user_id,
                    namespace,
                    RunMemoryContextSnapshotRow,
                ),
                RunMemoryContextSnapshotRow.rendered_content.is_not(None),
            )
            .values(rendered_content=None, content_erased_at=forgotten_at)
        )
        fact.status = "deleted"
        fact.disabled_at = None
        fact.superseded_at = None
        fact.deleted_at = forgotten_at
        fact.version = int(fact.version) + 1
        fact.updated_at = forgotten_at
        await self.session.flush()
        return MemoryV2HardForgetResult(
            fact_id=fact.id,
            version=int(fact.version),
            status="deleted",
            erased_candidates=len(candidates),
            erased_revisions=len(revisions),
            erased_evidence=len(affected_evidence),
            erased_source_items=len(source_items),
        )

    async def erase_sources(
        self,
        scope: PrivateResourceScope,
        *,
        thread_id: str,
        run_id: str | None,
        reason: Literal["thread_deleted", "run_deleted"],
        now: datetime,
    ) -> int:
        project_id, owner_user_id = _scope(scope)
        erased_at = _aware(now)
        if not isinstance(thread_id, str) or not thread_id or (run_id is not None and (not isinstance(run_id, str) or not run_id)):
            raise MemoryV2ManagementInvalid
        batch_statement = select(MemorySourceBatchRow).where(
            MemorySourceBatchRow.project_id == project_id,
            MemorySourceBatchRow.owner_user_id == owner_user_id,
            MemorySourceBatchRow.thread_id == thread_id,
        )
        if run_id is not None:
            batch_statement = batch_statement.where(
                MemorySourceBatchRow.run_id == run_id,
            )
        batches = tuple(
            (
                await self.session.execute(
                    batch_statement.order_by(
                        MemorySourceBatchRow.namespace,
                        MemorySourceBatchRow.id,
                    ).with_for_update(of=MemorySourceBatchRow)
                )
            ).scalars()
        )
        if not batches:
            snapshot_statement = delete(RunMemoryContextSnapshotRow).where(
                RunMemoryContextSnapshotRow.project_id == project_id,
                RunMemoryContextSnapshotRow.owner_user_id == owner_user_id,
                RunMemoryContextSnapshotRow.thread_id == thread_id,
            )
            if run_id is not None:
                snapshot_statement = snapshot_statement.where(
                    RunMemoryContextSnapshotRow.run_id == run_id,
                )
            await self.session.execute(snapshot_statement)
            return 0
        batch_ids = {row.id for row in batches}
        items = tuple(
            (
                await self.session.execute(
                    select(MemorySourceItemRow)
                    .where(
                        MemorySourceItemRow.project_id == project_id,
                        MemorySourceItemRow.owner_user_id == owner_user_id,
                        MemorySourceItemRow.source_batch_id.in_(batch_ids),
                    )
                    .order_by(MemorySourceItemRow.namespace, MemorySourceItemRow.id)
                    .with_for_update(of=MemorySourceItemRow)
                )
            ).scalars()
        )
        batch_by_id = {row.id: row for row in batches}
        suppression_values = [
            {
                "id": uuid.uuid4(),
                "project_id": item.project_id,
                "owner_user_id": item.owner_user_id,
                "namespace": item.namespace,
                "suppression_kind": "source",
                "identity_hmac": item.content_hmac,
                "hmac_key_version": batch_by_id[item.source_batch_id].source_hmac_key_version,
                "reason": reason,
                "created_at": erased_at,
            }
            for item in items
        ]
        if suppression_values:
            await self.session.execute(
                insert(MemorySuppressionRow)
                .values(suppression_values)
                .on_conflict_do_nothing(
                    constraint="uq_memory_suppressions_identity",
                )
            )
        for batch in batches:
            batch.suppressed_at = batch.suppressed_at or erased_at
            batch.suppression_reason = batch.suppression_reason or reason
        for item in items:
            item.content = None
            item.source_erased_at = item.source_erased_at or erased_at
        candidates = tuple(
            (
                await self.session.execute(
                    select(MemoryCandidateRow)
                    .where(
                        MemoryCandidateRow.project_id == project_id,
                        MemoryCandidateRow.owner_user_id == owner_user_id,
                        MemoryCandidateRow.source_batch_id.in_(batch_ids),
                    )
                    .order_by(MemoryCandidateRow.namespace, MemoryCandidateRow.id)
                    .with_for_update(of=MemoryCandidateRow)
                )
            ).scalars()
        )
        consolidation_ids = {row.consolidation_generation_id for row in candidates if row.consolidation_generation_id is not None}
        for candidate in candidates:
            candidate.consolidation_generation_id = None
            candidate.content = None
            candidate.content_erased_at = candidate.content_erased_at or erased_at
            if candidate.status == "pending":
                candidate.status = "rejected"
                candidate.decision_reason = "source_erased"
                candidate.decided_at = erased_at
                candidate.updated_at = erased_at
        evidence_rows = tuple(
            (
                await self.session.execute(
                    select(MemoryFactEvidenceRow)
                    .where(
                        MemoryFactEvidenceRow.project_id == project_id,
                        MemoryFactEvidenceRow.owner_user_id == owner_user_id,
                        MemoryFactEvidenceRow.thread_id == thread_id,
                        *(() if run_id is None else (MemoryFactEvidenceRow.run_id == run_id,)),
                    )
                    .order_by(MemoryFactEvidenceRow.namespace, MemoryFactEvidenceRow.id)
                    .with_for_update(of=MemoryFactEvidenceRow)
                )
            ).scalars()
        )
        affected_candidate_ids = {row.source_candidate_id for row in evidence_rows if row.source_candidate_id is not None}
        if affected_candidate_ids:
            await self.session.execute(
                update(MemoryFactRevisionRow)
                .where(
                    MemoryFactRevisionRow.project_id == project_id,
                    MemoryFactRevisionRow.owner_user_id == owner_user_id,
                    MemoryFactRevisionRow.source_candidate_id.in_(affected_candidate_ids),
                )
                .values(source_candidate_id=None)
            )
        for row in evidence_rows:
            row.source_candidate_id = None
            row.source_item_id = None
            row.thread_id = None
            row.run_id = None
            row.run_event_sequence = None
            row.evidence_excerpt = None
            row.source_erased_at = row.source_erased_at or erased_at
        extraction_job_ids = tuple(
            (
                await self.session.execute(
                    select(MemoryExtractionGenerationRow.job_id).where(
                        MemoryExtractionGenerationRow.project_id == project_id,
                        MemoryExtractionGenerationRow.owner_user_id == owner_user_id,
                        MemoryExtractionGenerationRow.source_batch_id.in_(batch_ids),
                    )
                )
            ).scalars()
        )
        consolidation_job_ids = tuple(
            (
                await self.session.execute(
                    select(MemoryConsolidationGenerationRow.job_id).where(
                        MemoryConsolidationGenerationRow.project_id == project_id,
                        MemoryConsolidationGenerationRow.owner_user_id == owner_user_id,
                        MemoryConsolidationGenerationRow.id.in_(consolidation_ids or {uuid.uuid4()}),
                    )
                )
            ).scalars()
        )
        await self._request_job_cancellations(
            project_id=project_id,
            owner_user_id=owner_user_id,
            job_ids=extraction_job_ids + consolidation_job_ids,
            reason=reason,
            now=erased_at,
        )
        if consolidation_ids:
            await self.session.execute(
                update(MemoryCandidateRow)
                .where(
                    MemoryCandidateRow.project_id == project_id,
                    MemoryCandidateRow.owner_user_id == owner_user_id,
                    MemoryCandidateRow.consolidation_generation_id.in_(consolidation_ids),
                )
                .values(consolidation_generation_id=None)
            )
            await self.session.execute(
                delete(MemoryConsolidationGenerationRow).where(
                    MemoryConsolidationGenerationRow.project_id == project_id,
                    MemoryConsolidationGenerationRow.owner_user_id == owner_user_id,
                    MemoryConsolidationGenerationRow.id.in_(consolidation_ids),
                )
            )
        snapshot_statement = delete(RunMemoryContextSnapshotRow).where(
            RunMemoryContextSnapshotRow.project_id == project_id,
            RunMemoryContextSnapshotRow.owner_user_id == owner_user_id,
            RunMemoryContextSnapshotRow.thread_id == thread_id,
        )
        if run_id is not None:
            snapshot_statement = snapshot_statement.where(
                RunMemoryContextSnapshotRow.run_id == run_id,
            )
        await self.session.execute(snapshot_statement)
        await self.session.flush()
        return len(items)

    async def purge_scope(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str | None,
        now: datetime,
    ) -> None:
        purged_at = _aware(now)
        owner = () if owner_user_id is None else (owner_user_id,)

        def predicates(row_type) -> tuple[object, ...]:
            return (
                row_type.project_id == project_id,
                *(() if not owner else (row_type.owner_user_id == owner_user_id,)),
            )

        await self.session.execute(
            update(JobRow)
            .where(
                JobRow.project_id == project_id,
                *(() if owner_user_id is None else (JobRow.owner_user_id == owner_user_id,)),
                JobRow.job_type.in_(
                    (
                        "memory_extract",
                        "memory_consolidate",
                        "memory_retention_purge",
                    )
                ),
                JobRow.status.in_(("queued", "leased", "running", "retry_wait")),
            )
            .values(
                cancel_requested_at=func.coalesce(
                    JobRow.cancel_requested_at,
                    purged_at,
                ),
                cancel_reason=func.coalesce(
                    JobRow.cancel_reason,
                    "retention_scope_completed",
                ),
                updated_at=purged_at,
            )
        )
        for row_type in (
            RunMemoryContextSnapshotRow,
            MemoryContextSummaryRow,
            MemoryFactEvidenceRow,
            MemoryFactRow,
            MemoryCandidateRow,
            MemoryConsolidationGenerationRow,
            MemoryExtractionGenerationRow,
            MemorySourceItemRow,
            MemorySourceBatchRow,
            MemorySuppressionRow,
        ):
            await self.session.execute(delete(row_type).where(*predicates(row_type)))

    async def reset_scope(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        now: datetime,
    ) -> MemoryV2ResetResult:
        """Erase one account-owned project scope while preserving replay suppression."""

        reset_at = _aware(now)
        try:
            uuid.UUID(owner_user_id)
        except (TypeError, ValueError):
            raise MemoryV2ManagementInvalid from None

        def predicates(row_type):
            return (
                row_type.project_id == project_id,
                row_type.owner_user_id == owner_user_id,
            )

        source_batches = int(await self.session.scalar(select(func.count()).select_from(MemorySourceBatchRow).where(*predicates(MemorySourceBatchRow))) or 0)
        candidates = int(await self.session.scalar(select(func.count()).select_from(MemoryCandidateRow).where(*predicates(MemoryCandidateRow))) or 0)
        facts = int(await self.session.scalar(select(func.count()).select_from(MemoryFactRow).where(*predicates(MemoryFactRow))) or 0)
        snapshots = int(await self.session.scalar(select(func.count()).select_from(RunMemoryContextSnapshotRow).where(*predicates(RunMemoryContextSnapshotRow))) or 0)

        source_identities = (
            await self.session.execute(
                select(
                    MemorySourceItemRow.namespace,
                    MemorySourceItemRow.content_hmac,
                    MemorySourceBatchRow.source_hmac_key_version,
                )
                .join(
                    MemorySourceBatchRow,
                    MemorySourceBatchRow.id == MemorySourceItemRow.source_batch_id,
                )
                .where(*predicates(MemorySourceItemRow))
                .order_by(MemorySourceItemRow.namespace, MemorySourceItemRow.id)
            )
        ).all()
        if source_identities:
            await self.session.execute(
                insert(MemorySuppressionRow)
                .values(
                    [
                        {
                            "id": uuid.uuid4(),
                            "project_id": project_id,
                            "owner_user_id": owner_user_id,
                            "namespace": row.namespace,
                            "suppression_kind": "source",
                            "identity_hmac": row.content_hmac,
                            "hmac_key_version": row.source_hmac_key_version,
                            "reason": "account_memory_reset",
                            "created_at": reset_at,
                        }
                        for row in source_identities
                    ]
                )
                .on_conflict_do_nothing(
                    constraint="uq_memory_suppressions_identity",
                )
            )

        active_job_ids = tuple(
            (
                await self.session.execute(
                    select(JobRow.id)
                    .where(
                        JobRow.project_id == project_id,
                        JobRow.owner_user_id == owner_user_id,
                        JobRow.job_type.in_(
                            (
                                "memory_extract",
                                "memory_consolidate",
                                "memory_retention_purge",
                            )
                        ),
                        JobRow.status.in_(("queued", "leased", "running", "retry_wait")),
                    )
                    .order_by(JobRow.id)
                    .with_for_update(of=JobRow)
                )
            ).scalars()
        )
        scope = JobScope(project_id, owner_user_id)
        jobs_cancelled = 0
        for job_id in active_job_ids:
            if await self.jobs.request_cancel(
                scope,
                job_id,
                reason="account_memory_reset",
                now=reset_at,
            ):
                jobs_cancelled += 1
                await self.jobs.settle_requested_cancel(
                    scope,
                    job_id,
                    now=reset_at,
                )

        for row_type in (
            RunMemoryContextSnapshotRow,
            MemoryContextSummaryRow,
            MemoryFactEvidenceRow,
            MemoryFactRow,
            MemoryCandidateRow,
            MemoryConsolidationGenerationRow,
            MemoryExtractionGenerationRow,
            MemorySourceItemRow,
            MemorySourceBatchRow,
        ):
            await self.session.execute(delete(row_type).where(*predicates(row_type)))
        await self.session.flush()
        return MemoryV2ResetResult(
            source_batches=source_batches,
            candidates=candidates,
            facts=facts,
            snapshots=snapshots,
            jobs_cancelled=jobs_cancelled,
        )


__all__ = [
    "MemoryCandidateStatus",
    "MemoryFactStatus",
    "MemoryV2CandidateView",
    "MemoryV2EvidenceView",
    "MemoryV2FactDetail",
    "MemoryV2FactView",
    "MemoryV2HardForgetResult",
    "MemoryV2ManagementConflict",
    "MemoryV2ManagementError",
    "MemoryV2ManagementInvalid",
    "MemoryV2ManagementNotFound",
    "MemoryV2ManagementRepository",
    "MemoryV2RevisionView",
    "MemoryV2ResetResult",
]
