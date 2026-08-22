"""Focused reader components for the private Memory persistence facade.

These helpers deliberately borrow the caller-owned ``AsyncSession``. They do
not open transactions or commit, so composing them behind
``MemoryDocumentRepository`` preserves the existing atomic Dream/reset lock
discipline while separating document/version and episode read concerns.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.memory_contract import (
    EPISODE_SIMILARITY_FLOOR,
    MAX_EPISODE_QUERY_CHARS,
    REMEMBER_BACKLOG_LIMIT,
    REMEMBER_PROMPT_VERSION,
    REMEMBER_RUN_LIMIT,
    MemoryDocumentConflict,
    MemoryDocumentNotFound,
    MemoryDocumentRecord,
    MemoryDocumentScope,
    MemoryDocumentState,
    MemoryDocumentVersionRecord,
    MemoryEpisodeRecord,
    MemoryHistoryActivation,
    MemoryHistoryActivationResult,
    MemoryPendingEntryRecord,
    MemoryProposalOutcome,
    MemoryRememberProposal,
    compute_snip_content_digest,
    escape_like_pattern,
    memory_document_unified_diff,
    scope_predicates,
    validate_episode_retention_days,
    validate_memory_document,
    validate_memory_document_sections,
    validated_episode_tags,
)
from deerflow.persistence.private_work.memory_document_model import (
    MemoryDocumentRow,
    MemoryDocumentVersionRow,
    MemoryEpisodeRow,
    MemoryHistoryEntryRow,
)
from deerflow.persistence.user.model import UserRow


def frozen_document_sections(
    row: MemoryDocumentRow,
) -> tuple[tuple[str, ...], uuid.UUID]:
    if row.sections_policy_section != "memory_document" or not isinstance(
        row.sections_policy_version_id,
        uuid.UUID,
    ):
        raise MemoryDocumentConflict("Memory document sections provenance is invalid")
    return (
        validate_memory_document_sections(row.sections),
        row.sections_policy_version_id,
    )


def document_record(row: MemoryDocumentRow | None) -> MemoryDocumentRecord:
    if row is None:
        return MemoryDocumentRecord(
            content="",
            content_digest="",
            sections=(),
            sections_policy_version_id=None,
            version=0,
            dream_cursor=0,
            active_dream_job_id=None,
            updated_at=None,
        )
    sections, sections_policy_version_id = frozen_document_sections(row)
    return MemoryDocumentRecord(
        content=row.content,
        content_digest=row.content_digest,
        sections=sections,
        sections_policy_version_id=sections_policy_version_id,
        version=int(row.version),
        dream_cursor=int(row.dream_cursor),
        active_dream_job_id=row.active_dream_job_id,
        updated_at=row.updated_at,
    )


def version_record(row: MemoryDocumentVersionRow) -> MemoryDocumentVersionRecord:
    return MemoryDocumentVersionRecord(
        version=int(row.version),
        content=row.content,
        content_digest=row.content_digest,
        unified_diff=row.unified_diff,
        trigger=row.trigger,
        dream_job_id=row.dream_job_id,
        history_from=None if row.history_from is None else int(row.history_from),
        history_to=None if row.history_to is None else int(row.history_to),
        history_count=None if row.history_count is None else int(row.history_count),
        prompt_version=row.prompt_version,
        needs_review=bool(row.needs_review),
        created_at=row.created_at,
    )


class MemoryHistoryRepository:
    """Receipt activation and explicit remember-proposal persistence."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def activate(
        self,
        activation: MemoryHistoryActivation,
    ) -> MemoryHistoryActivationResult:
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
                summary_model_config_id=activation.summary_model.model_config_id,
                summary_model_payload_checksum=(activation.summary_model.payload_checksum),
                summary_model_secret_generation_id=(activation.summary_model.secret_generation_id),
                summary_model_secret_envelope_digest=(activation.summary_model.secret_envelope_digest),
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
                    *scope_predicates(
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
    def _validate_activated_history(row: MemoryHistoryEntryRow, activation) -> None:
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
            or row.summary_model_config_id != activation.summary_model.model_config_id
            or row.summary_model_payload_checksum != activation.summary_model.payload_checksum
            or row.summary_model_secret_generation_id != activation.summary_model.secret_generation_id
            or row.summary_model_secret_envelope_digest != activation.summary_model.secret_envelope_digest
            or row.status not in {"pending", "processing", "consumed"}
            or (row.status in {"pending", "processing"} and row.tagged_text != activation.tagged_text)
            or (row.status == "consumed" and row.tagged_text is not None)
        ):
            raise MemoryDocumentConflict("Memory history receipt conflicts")

    async def propose(
        self,
        proposal: MemoryRememberProposal,
    ) -> MemoryProposalOutcome:
        if type(proposal) is not MemoryRememberProposal:
            raise TypeError("MemoryRememberProposal is required")
        preference = (
            await self.session.execute(
                sa.select(
                    UserRow.memory_enabled,
                    UserRow.preferences_version,
                )
                .where(UserRow.id == proposal.scope.owner_user_id)
                .with_for_update(of=UserRow)
            )
        ).one_or_none()
        if preference is None or not bool(preference.memory_enabled):
            return MemoryProposalOutcome(
                disposition="memory_disabled",
                entry_id=None,
                tagged_text=None,
            )

        predicates = scope_predicates(
            MemoryHistoryEntryRow,
            proposal.scope,
        )
        existing_id = await self.session.scalar(
            sa.select(MemoryHistoryEntryRow.id).where(
                *predicates,
                MemoryHistoryEntryRow.thread_id == proposal.thread_id,
                MemoryHistoryEntryRow.source_digest == proposal.source_digest,
            )
        )
        if existing_id is not None:
            return MemoryProposalOutcome(
                disposition="duplicate",
                entry_id=existing_id,
                tagged_text=proposal.tagged_text,
            )

        run_count = int(
            await self.session.scalar(
                sa.select(sa.func.count())
                .select_from(MemoryHistoryEntryRow)
                .where(
                    *predicates,
                    MemoryHistoryEntryRow.origin == "tool",
                    MemoryHistoryEntryRow.source_run_id == proposal.run_id,
                )
            )
            or 0
        )
        if run_count >= REMEMBER_RUN_LIMIT:
            return MemoryProposalOutcome(
                disposition="run_limit_reached",
                entry_id=None,
                tagged_text=None,
            )

        pending_count = int(
            await self.session.scalar(
                sa.select(sa.func.count())
                .select_from(MemoryHistoryEntryRow)
                .where(
                    *predicates,
                    MemoryHistoryEntryRow.status == "pending",
                )
            )
            or 0
        )
        if pending_count >= REMEMBER_BACKLOG_LIMIT:
            return MemoryProposalOutcome(
                disposition="backlog_full",
                entry_id=None,
                tagged_text=None,
            )

        inserted_id = await self.session.scalar(
            pg_insert(MemoryHistoryEntryRow)
            .values(
                project_id=proposal.scope.project_id,
                owner_user_id=proposal.scope.owner_user_id,
                namespace=proposal.scope.namespace,
                thread_id=proposal.thread_id,
                origin="tool",
                source_run_id=proposal.run_id,
                source_digest=proposal.source_digest,
                status="pending",
                tagged_text=proposal.tagged_text,
                content_digest=compute_snip_content_digest(proposal.tagged_text),
                preference_version=int(preference.preferences_version),
                snip_prompt_version=REMEMBER_PROMPT_VERSION,
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
        if inserted_id is None:
            raise MemoryDocumentConflict("Memory proposal disappeared under lock")
        await self.session.flush()
        return MemoryProposalOutcome(
            disposition="recorded",
            entry_id=inserted_id,
            tagged_text=proposal.tagged_text,
        )


class MemoryDocumentStore:
    """Current document, immutable versions, pending reads, and restore writes."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def read_state(
        self,
        scope: MemoryDocumentScope,
        *,
        for_update: bool = False,
    ) -> MemoryDocumentState:
        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        statement = sa.select(MemoryDocumentRow).where(
            *scope_predicates(MemoryDocumentRow, scope),
        )
        if for_update:
            statement = statement.with_for_update(of=MemoryDocumentRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        pending_count = int(
            await self.session.scalar(
                sa.select(sa.func.count())
                .select_from(MemoryHistoryEntryRow)
                .where(
                    *scope_predicates(MemoryHistoryEntryRow, scope),
                    MemoryHistoryEntryRow.status == "pending",
                )
            )
            or 0
        )
        return MemoryDocumentState(
            document=document_record(row),
            pending_count=pending_count,
        )

    async def list_pending_entries(
        self,
        scope,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MemoryPendingEntryRecord, ...]:
        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 10_000:
            raise ValueError("Memory pending pagination is invalid")
        rows = (
            await self.session.execute(
                sa.select(MemoryHistoryEntryRow)
                .where(
                    *scope_predicates(
                        MemoryHistoryEntryRow,
                        scope,
                    ),
                    MemoryHistoryEntryRow.status == "pending",
                )
                .order_by(MemoryHistoryEntryRow.sequence)
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
        return tuple(
            MemoryPendingEntryRecord(
                sequence=int(row.sequence),
                origin=row.origin,
                tagged_text=row.tagged_text or "",
                created_at=row.created_at,
            )
            for row in rows
        )

    async def list_versions(
        self,
        scope,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MemoryDocumentVersionRecord, ...]:
        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        if not 1 <= limit <= 100 or not 0 <= offset <= 10_000:
            raise ValueError("Memory version pagination is invalid")
        rows = (
            await self.session.execute(
                sa.select(MemoryDocumentVersionRow)
                .where(
                    *scope_predicates(
                        MemoryDocumentVersionRow,
                        scope,
                    )
                )
                .order_by(MemoryDocumentVersionRow.version.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
        return tuple(version_record(row) for row in rows)

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
                    *scope_predicates(
                        MemoryDocumentVersionRow,
                        scope,
                    ),
                    MemoryDocumentVersionRow.version == version,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise MemoryDocumentNotFound
        return version_record(row)

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
        expected_sections = validate_memory_document_sections(expected_sections)
        if (
            type(target_version) is not int
            or target_version < 1
            or type(expected_current_version) is not int
            or expected_current_version < 0
            or type(max_tokens) is not int
            or max_tokens < 1
            or not isinstance(now, datetime)
            or now.tzinfo is None
        ):
            raise ValueError("Memory restore input is invalid")
        document = (await self.session.execute(sa.select(MemoryDocumentRow).where(*scope_predicates(MemoryDocumentRow, scope)).with_for_update(of=MemoryDocumentRow))).scalar_one_or_none()
        if document is None:
            raise MemoryDocumentNotFound
        if document.active_dream_job_id is not None or int(document.version) != expected_current_version or frozen_document_sections(document)[0] != expected_sections:
            raise MemoryDocumentConflict("Memory restore CAS conflict")
        target = (
            await self.session.execute(
                sa.select(MemoryDocumentVersionRow).where(
                    *scope_predicates(MemoryDocumentVersionRow, scope),
                    MemoryDocumentVersionRow.version == target_version,
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise MemoryDocumentNotFound
        validate_memory_document(
            target.content,
            max_tokens,
            sections=expected_sections,
        )
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
            created_at=now,
        )
        self.session.add(restored)
        document.content = target.content
        document.content_digest = target.content_digest
        document.version = next_version
        document.updated_at = now
        await self.session.flush()
        return version_record(restored)


# Compatibility alias for callers that still use the read-only historical name.
MemoryDocumentReader = MemoryDocumentStore


class MemoryEpisodeReader:
    """Retention-aware episode search and browse component."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _read_boundary(scope, limit: int, retention_days: int, now: datetime) -> None:
        if type(scope) is not MemoryDocumentScope:
            raise TypeError("MemoryDocumentScope is required")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Episode limit is out of contract")
        validate_episode_retention_days(retention_days)
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("Episode read time must be timezone-aware")

    @staticmethod
    def _predicates(
        scope,
        *,
        tags: tuple[str, ...],
        retention_days: int,
        now: datetime,
    ) -> list[sa.ColumnElement[bool]]:
        predicates: list[sa.ColumnElement[bool]] = [
            *scope_predicates(
                MemoryEpisodeRow,
                scope,
            ),
        ]
        if retention_days:
            predicates.append(MemoryEpisodeRow.occurred_at >= now - timedelta(days=retention_days))
        if tags:
            predicates.append(sa.or_(*(MemoryEpisodeRow.tagged_text.like(f"%[{tag}]%") for tag in tags)))
        return predicates

    @staticmethod
    def _record(row: MemoryEpisodeRow) -> MemoryEpisodeRecord:
        return MemoryEpisodeRecord(
            id=row.id,
            thread_id=row.thread_id,
            origin=row.origin,
            tagged_text=row.tagged_text,
            occurred_at=row.occurred_at,
            created_at=row.created_at,
        )

    async def search(
        self,
        scope,
        *,
        query: str,
        tags: tuple[str, ...],
        limit: int,
        retention_days: int,
        now: datetime,
    ) -> tuple[MemoryEpisodeRecord, ...]:
        self._read_boundary(scope, limit, retention_days, now)
        if not isinstance(query, str):
            raise ValueError("Episode query must be a string")
        query = query.strip()
        if not query or len(query) > MAX_EPISODE_QUERY_CHARS:
            raise ValueError("Episode query is out of contract")
        normalized_tags = validated_episode_tags(tags)
        pattern = f"%{escape_like_pattern(query)}%"
        exact_hit = sa.case(
            (MemoryEpisodeRow.tagged_text.ilike(pattern, escape="\\"), 1),
            else_=0,
        )
        similarity = sa.func.similarity(MemoryEpisodeRow.tagged_text, query)
        statement = (
            sa.select(MemoryEpisodeRow)
            .where(
                *self._predicates(
                    scope,
                    tags=normalized_tags,
                    retention_days=retention_days,
                    now=now,
                ),
                sa.or_(
                    exact_hit == 1,
                    similarity >= EPISODE_SIMILARITY_FLOOR,
                ),
            )
            .order_by(
                exact_hit.desc(),
                similarity.desc(),
                MemoryEpisodeRow.occurred_at.desc(),
                MemoryEpisodeRow.id.desc(),
            )
            .limit(limit)
        )
        rows = (await self.session.execute(statement)).scalars()
        return tuple(self._record(row) for row in rows)

    async def list(
        self,
        scope,
        *,
        tags: tuple[str, ...],
        cursor: tuple[datetime, uuid.UUID] | None,
        limit: int,
        retention_days: int,
        now: datetime,
    ) -> tuple[MemoryEpisodeRecord, ...]:
        if type(limit) is not int or not 1 <= limit <= 51:
            raise ValueError("Episode page limit is out of contract")
        self._read_boundary(scope, min(limit, 50), retention_days, now)
        if cursor is not None:
            if type(cursor) is not tuple or len(cursor) != 2 or not isinstance(cursor[0], datetime) or cursor[0].tzinfo is None or type(cursor[1]) is not uuid.UUID:
                raise ValueError("Episode cursor is invalid")
        normalized_tags = validated_episode_tags(tags)
        predicates = self._predicates(
            scope,
            tags=normalized_tags,
            retention_days=retention_days,
            now=now,
        )
        if cursor is not None:
            occurred_at, episode_id = cursor
            predicates.append(
                sa.or_(
                    MemoryEpisodeRow.occurred_at < occurred_at,
                    sa.and_(
                        MemoryEpisodeRow.occurred_at == occurred_at,
                        MemoryEpisodeRow.id < episode_id,
                    ),
                )
            )
        statement = (
            sa.select(MemoryEpisodeRow)
            .where(*predicates)
            .order_by(
                MemoryEpisodeRow.occurred_at.desc(),
                MemoryEpisodeRow.id.desc(),
            )
            .limit(limit)
        )
        rows = (await self.session.execute(statement)).scalars()
        return tuple(self._record(row) for row in rows)


__all__ = [
    "MemoryDocumentReader",
    "MemoryDocumentStore",
    "MemoryEpisodeReader",
    "MemoryHistoryRepository",
]
