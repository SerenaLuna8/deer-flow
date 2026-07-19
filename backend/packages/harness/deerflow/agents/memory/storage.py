"""Project-scoped PostgreSQL memory storage."""

import copy
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.private_work.memory_repository import (
    PrivateMemoryFactRecord,
    PrivateMemoryFactWrite,
    PrivateMemoryRecord,
    PrivateMemoryRepository,
)
from deerflow.private_scope import PrivateResourceScope

logger = logging.getLogger(__name__)


def utc_now_iso_z() -> str:
    """Current UTC time as ISO-8601 with ``Z`` suffix (matches prior naive-UTC output)."""
    return datetime.now(UTC).isoformat().removesuffix("+00:00") + "Z"


def create_empty_memory() -> dict[str, Any]:
    """Create an empty memory structure."""
    return {
        "version": "1.0",
        "lastUpdated": utc_now_iso_z(),
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


class ProjectMemoryStorage:
    """Async project-owner scoped adapter over the PostgreSQL Memory repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _context_summary(memory_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(memory_data, dict):
            raise ValueError("memory data")
        empty = create_empty_memory()
        user = memory_data.get("user", empty["user"])
        history = memory_data.get("history", empty["history"])
        if not isinstance(user, dict) or not isinstance(history, dict):
            raise ValueError("memory data")
        version = memory_data.get("version", empty["version"])
        last_updated = memory_data.get("lastUpdated", empty["lastUpdated"])
        if not isinstance(version, str) or not isinstance(last_updated, str):
            raise ValueError("memory data")
        return {
            "version": version,
            "lastUpdated": last_updated,
            "user": copy.deepcopy(user),
            "history": copy.deepcopy(history),
        }

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @classmethod
    def _fact_write(cls, fact: object) -> PrivateMemoryFactWrite:
        if not isinstance(fact, dict):
            raise ValueError("memory fact")
        content = fact.get("content")
        category = fact.get("category", "context")
        confidence = fact.get("confidence", 0.5)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("memory fact content")
        if not isinstance(category, str) or not category.strip() or len(category.strip()) > 32:
            raise ValueError("memory fact category")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("memory fact confidence")
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("memory fact confidence")
        try:
            fact_id = uuid.UUID(str(fact.get("id")))
        except (TypeError, ValueError):
            fact_id = uuid.uuid4()

        source_thread_id = fact.get("sourceThreadId")
        if source_thread_id is None:
            legacy_source = fact.get("source")
            if isinstance(legacy_source, str) and legacy_source not in {"", "manual", "unknown"}:
                source_thread_id = legacy_source
        source_run_id = fact.get("sourceRunId")
        if source_thread_id is not None and (not isinstance(source_thread_id, str) or not source_thread_id):
            raise ValueError("memory fact source thread")
        if source_run_id is not None and (not isinstance(source_run_id, str) or not source_run_id or source_thread_id is None):
            raise ValueError("memory fact source run")
        return PrivateMemoryFactWrite(
            id=fact_id,
            content=content.strip(),
            category=category.strip(),
            confidence=confidence,
            source_thread_id=source_thread_id,
            source_run_id=source_run_id,
            created_at=cls._parse_datetime(fact.get("createdAt")),
        )

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
            "lastUpdated": copy.deepcopy(context.get("lastUpdated", empty["lastUpdated"])),
            "user": copy.deepcopy(context.get("user", empty["user"])),
            "history": copy.deepcopy(context.get("history", empty["history"])),
            "facts": [cls._fact_json(fact) for fact in record.facts],
        }
        return ProjectMemorySnapshot(memory=memory, version=record.version)

    async def create_if_needed(
        self,
        *,
        scope: PrivateResourceScope,
        namespace: str,
    ) -> ProjectMemorySnapshot:
        empty = create_empty_memory()
        async with self._session_factory() as session, session.begin():
            record = await PrivateMemoryRepository(session).create_if_needed(
                scope=scope,
                namespace=namespace,
                context_summary=self._context_summary(empty),
            )
        return self._snapshot(record)

    async def load(
        self,
        *,
        scope: PrivateResourceScope,
        namespace: str,
    ) -> ProjectMemorySnapshot:
        return await self.create_if_needed(scope=scope, namespace=namespace)

    async def reload(
        self,
        *,
        scope: PrivateResourceScope,
        namespace: str,
    ) -> ProjectMemorySnapshot:
        return await self.load(scope=scope, namespace=namespace)

    async def save(
        self,
        memory_data: dict[str, Any],
        *,
        scope: PrivateResourceScope,
        namespace: str,
        expected_version: int,
    ) -> ProjectMemorySnapshot:
        facts = memory_data.get("facts", []) if isinstance(memory_data, dict) else None
        if not isinstance(facts, list):
            raise ValueError("memory facts")
        fact_writes = tuple(self._fact_write(fact) for fact in facts)
        async with self._session_factory() as session, session.begin():
            record = await PrivateMemoryRepository(session).save(
                scope=scope,
                namespace=namespace,
                context_summary=self._context_summary(memory_data),
                facts=fact_writes,
                expected_version=expected_version,
            )
        return self._snapshot(record)

    async def clear(
        self,
        *,
        scope: PrivateResourceScope,
        namespace: str,
        expected_version: int,
    ) -> ProjectMemorySnapshot:
        empty = create_empty_memory()
        async with self._session_factory() as session, session.begin():
            record = await PrivateMemoryRepository(session).clear(
                scope=scope,
                namespace=namespace,
                context_summary=self._context_summary(empty),
                expected_version=expected_version,
            )
        return self._snapshot(record)
