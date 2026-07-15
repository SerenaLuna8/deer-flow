from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.private_work.model import (
    UserProjectMemoryFactRow,
    UserProjectMemoryRow,
)
from deerflow.private_scope import PrivateResourceScope


class PrivateMemoryConflict(Exception):
    """A project memory write could not satisfy its domain invariants."""


class PrivateMemoryVersionConflict(PrivateMemoryConflict):
    """The authoritative memory version changed before this write."""


class PrivateMemoryInvalid(PrivateMemoryConflict):
    """Project memory coordinates or payload fields are invalid."""


@dataclass(frozen=True, slots=True)
class PrivateMemoryFactWrite:
    id: uuid.UUID
    content: str
    category: str
    confidence: float
    source_thread_id: str | None = None
    source_run_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PrivateMemoryFactRecord:
    id: uuid.UUID
    content: str
    category: str
    confidence: float
    source_thread_id: str | None
    source_run_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PrivateMemoryRecord:
    id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    namespace: str
    context_summary: dict
    version: int
    created_at: datetime
    updated_at: datetime
    facts: tuple[PrivateMemoryFactRecord, ...]


class PrivateMemoryRepository:
    """Session-bound PostgreSQL authority for project-owner Memory."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _coordinates(scope: PrivateResourceScope) -> tuple[uuid.UUID, str]:
        if type(scope) is not PrivateResourceScope:
            raise PrivateMemoryInvalid("private memory scope is required")
        try:
            return uuid.UUID(scope.project_id), str(uuid.UUID(scope.owner_user_id))
        except (TypeError, ValueError):
            raise PrivateMemoryInvalid("private memory scope is invalid") from None

    @staticmethod
    def _namespace(namespace: str) -> str:
        if not isinstance(namespace, str) or not namespace or namespace.strip() != namespace or len(namespace) > 255:
            raise PrivateMemoryInvalid("memory namespace is invalid")
        return namespace

    @classmethod
    def _scope_predicates(cls, scope: PrivateResourceScope, namespace: str):
        project_id, owner_user_id = cls._coordinates(scope)
        namespace = cls._namespace(namespace)
        return (
            UserProjectMemoryRow.project_id == project_id,
            UserProjectMemoryRow.owner_user_id == owner_user_id,
            UserProjectMemoryRow.namespace == namespace,
        )

    @staticmethod
    def _fact_record(row: UserProjectMemoryFactRow) -> PrivateMemoryFactRecord:
        return PrivateMemoryFactRecord(
            id=row.id,
            content=row.content,
            category=row.category,
            confidence=row.confidence,
            source_thread_id=row.source_thread_id,
            source_run_id=row.source_run_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def _record(self, row: UserProjectMemoryRow) -> PrivateMemoryRecord:
        facts = (
            await self.session.execute(
                select(UserProjectMemoryFactRow)
                .where(
                    UserProjectMemoryFactRow.project_id == row.project_id,
                    UserProjectMemoryFactRow.owner_user_id == row.owner_user_id,
                    UserProjectMemoryFactRow.memory_id == row.id,
                )
                .order_by(UserProjectMemoryFactRow.created_at, UserProjectMemoryFactRow.id)
            )
        ).scalars()
        return PrivateMemoryRecord(
            id=row.id,
            project_id=row.project_id,
            owner_user_id=row.owner_user_id,
            namespace=row.namespace,
            context_summary=dict(row.context_summary),
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            facts=tuple(self._fact_record(fact) for fact in facts),
        )

    async def load(
        self,
        *,
        scope: PrivateResourceScope,
        namespace: str,
        lock: bool = False,
    ) -> PrivateMemoryRecord | None:
        statement = select(UserProjectMemoryRow).where(*self._scope_predicates(scope, namespace))
        if lock:
            statement = statement.with_for_update(of=UserProjectMemoryRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        return None if row is None else await self._record(row)

    async def create_if_needed(
        self,
        *,
        scope: PrivateResourceScope,
        namespace: str,
        context_summary: Mapping[str, object] | None = None,
    ) -> PrivateMemoryRecord:
        project_id, owner_user_id = self._coordinates(scope)
        namespace = self._namespace(namespace)
        now = datetime.now(UTC)
        await self.session.execute(
            insert(UserProjectMemoryRow)
            .values(
                id=uuid.uuid4(),
                project_id=project_id,
                owner_user_id=owner_user_id,
                namespace=namespace,
                context_summary=dict(context_summary or {}),
                version=1,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=(
                    UserProjectMemoryRow.project_id,
                    UserProjectMemoryRow.owner_user_id,
                    UserProjectMemoryRow.namespace,
                )
            )
        )
        row = (await self.session.execute(select(UserProjectMemoryRow).where(*self._scope_predicates(scope, namespace)))).scalar_one()
        return await self._record(row)

    async def save(
        self,
        *,
        scope: PrivateResourceScope,
        namespace: str,
        context_summary: Mapping[str, object],
        facts: Sequence[PrivateMemoryFactWrite],
        expected_version: int,
    ) -> PrivateMemoryRecord:
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 1:
            raise PrivateMemoryInvalid("expected memory version is invalid")
        if not isinstance(context_summary, Mapping):
            raise PrivateMemoryInvalid("memory context summary is invalid")
        now = datetime.now(UTC)
        updated = (
            await self.session.execute(
                update(UserProjectMemoryRow)
                .where(
                    *self._scope_predicates(scope, namespace),
                    UserProjectMemoryRow.version == expected_version,
                )
                .values(
                    context_summary=dict(context_summary),
                    version=UserProjectMemoryRow.version + 1,
                    updated_at=now,
                )
                .returning(UserProjectMemoryRow.id)
            )
        ).scalar_one_or_none()
        if updated is None:
            raise PrivateMemoryVersionConflict("project memory version conflict")

        project_id, owner_user_id = self._coordinates(scope)
        await self.session.execute(
            delete(UserProjectMemoryFactRow).where(
                UserProjectMemoryFactRow.project_id == project_id,
                UserProjectMemoryFactRow.owner_user_id == owner_user_id,
                UserProjectMemoryFactRow.memory_id == updated,
            )
        )
        for fact in facts:
            if type(fact) is not PrivateMemoryFactWrite:
                raise PrivateMemoryInvalid("memory fact is invalid")
            self.session.add(
                UserProjectMemoryFactRow(
                    id=fact.id,
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    memory_id=updated,
                    content=fact.content,
                    category=fact.category,
                    confidence=fact.confidence,
                    source_thread_id=fact.source_thread_id,
                    source_run_id=fact.source_run_id,
                    created_at=fact.created_at or now,
                    updated_at=now,
                )
            )
        await self.session.flush()
        row = (
            await self.session.execute(
                select(UserProjectMemoryRow).where(
                    UserProjectMemoryRow.id == updated,
                    *self._scope_predicates(scope, namespace),
                )
            )
        ).scalar_one()
        return await self._record(row)

    async def clear(
        self,
        *,
        scope: PrivateResourceScope,
        namespace: str,
        context_summary: Mapping[str, object],
        expected_version: int,
    ) -> PrivateMemoryRecord:
        return await self.save(
            scope=scope,
            namespace=namespace,
            context_summary=context_summary,
            facts=(),
            expected_version=expected_version,
        )
