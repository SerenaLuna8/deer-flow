"""Project-scoped PostgreSQL memory storage."""

import copy
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.private_work.memory_repository import (
    PrivateMemoryFactRecord,
    PrivateMemoryRecord,
    PrivateMemoryRepository,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.private_scope import PrivateResourceScope


def utc_now_iso_z() -> str:
    """Current UTC time as ISO-8601 with ``Z`` suffix (matches prior naive-UTC output)."""
    return datetime.now(UTC).isoformat().removesuffix("+00:00") + "Z"


def create_empty_memory(*, last_updated: str | None = None) -> dict[str, Any]:
    """Create an empty memory structure."""
    return {
        "version": "1.0",
        "lastUpdated": utc_now_iso_z() if last_updated is None else last_updated,
        "user": {
            "workContext": {"summary": "", "updatedAt": ""},
            "personalContext": {"summary": "", "updatedAt": ""},
            "topOfMind": {"summary": "", "updatedAt": ""},
        },
        "history": {
            "recentMonths": {"summary": "", "updatedAt": ""},
            "earlierContext": {"summary": "", "updatedAt": ""},
            "longTermBackground": {"summary": "", "updatedAt": ""},
        },
        "facts": [],
    }


@dataclass(frozen=True, slots=True)
class ProjectMemorySnapshot:
    """Existing Memory JSON plus its PostgreSQL optimistic-lock version."""

    memory: dict[str, Any]
    version: int


class ProjectMemoryMembershipRevalidator:
    """Check that the captured owner still has the captured active membership."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def is_active(self, scope: PrivateResourceScope) -> bool:
        if type(scope) is not PrivateResourceScope:
            return False
        try:
            project_id = uuid.UUID(scope.project_id)
            owner_user_id = str(uuid.UUID(scope.owner_user_id))
        except (TypeError, ValueError):
            return False

        async with self._session_factory() as session:
            membership_id = (
                await session.execute(
                    select(ProjectMembershipRow.id)
                    .join(ProjectRow, ProjectRow.id == ProjectMembershipRow.project_id)
                    .where(
                        ProjectMembershipRow.project_id == project_id,
                        ProjectMembershipRow.user_id == owner_user_id,
                        ProjectMembershipRow.status == "active",
                        ProjectMembershipRow.version == scope.membership_version,
                        ProjectRow.status == "active",
                        ProjectRow.is_suspended.is_(False),
                    )
                )
            ).scalar_one_or_none()
        return membership_id is not None


class ProjectMemoryStorage:
    """Async project-owner scoped adapter over the PostgreSQL Memory repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _iso_z(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().removesuffix("+00:00") + "Z"

    @classmethod
    def _fact_json(cls, fact: PrivateMemoryFactRecord) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": str(fact.id),
            "content": fact.content,
            "category": fact.category,
            "confidence": fact.confidence,
            "createdAt": cls._iso_z(fact.created_at),
            "source": fact.source_thread_id or "manual",
        }
        if fact.source_thread_id is not None:
            result["sourceThreadId"] = fact.source_thread_id
        if fact.source_run_id is not None:
            result["sourceRunId"] = fact.source_run_id
        return result

    @classmethod
    def _snapshot(cls, record: PrivateMemoryRecord) -> ProjectMemorySnapshot:
        empty = create_empty_memory()
        context = record.context_summary
        memory = {
            "version": copy.deepcopy(context.get("version", empty["version"])),
            "lastUpdated": cls._iso_z(record.updated_at),
            "user": copy.deepcopy(context.get("user", empty["user"])),
            "history": copy.deepcopy(context.get("history", empty["history"])),
            "facts": [cls._fact_json(fact) for fact in record.facts],
        }
        return ProjectMemorySnapshot(memory=memory, version=record.version)

    async def load(
        self,
        *,
        scope: PrivateResourceScope,
        namespace: str,
    ) -> ProjectMemorySnapshot:
        async with self._session_factory() as session, session.begin():
            record = await PrivateMemoryRepository(session).load(
                scope=scope,
                namespace=namespace,
            )
        if record is None:
            return ProjectMemorySnapshot(
                memory=create_empty_memory(last_updated=""),
                version=0,
            )
        return self._snapshot(record)
