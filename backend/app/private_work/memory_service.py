from __future__ import annotations

import copy
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkError,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.projects.capabilities import Capability
from deerflow.agents.memory.storage import ProjectMemorySnapshot, ProjectMemoryStorage
from deerflow.persistence.private_work.memory_repository import (
    PrivateMemoryInvalid as RepositoryMemoryInvalid,
)
from deerflow.persistence.private_work.memory_repository import (
    PrivateMemoryVersionConflict,
)


@dataclass(frozen=True, slots=True)
class PrivateMemoryStatus:
    namespace: str
    version: int
    fact_count: int
    last_updated: str


class PrivateMemoryService:
    """Application boundary for callable project Memory operations."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        storage: ProjectMemoryStorage | None = None,
        revalidator: PrivateWorkRevalidator | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage or ProjectMemoryStorage(session_factory)
        self._revalidator = revalidator or PrivateWorkRevalidator()

    async def _require(
        self,
        context: PrivateWorkContext,
        capability: Capability,
    ) -> PrivateWorkContext:
        context = require_issued_private_work_context(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._revalidator.require(session, context, capability)
        except PrivateWorkError:
            raise
        except DBAPIError:
            raise PrivateWorkUnavailable(context.request_id) from None
        return context

    @staticmethod
    def _map_storage_error(context: PrivateWorkContext, exc: Exception) -> PrivateWorkError:
        if isinstance(exc, PrivateMemoryVersionConflict):
            return PrivateWorkConflict(context.request_id)
        if isinstance(exc, (RepositoryMemoryInvalid, ValueError, IntegrityError)):
            return PrivateWorkInvalid(context.request_id)
        return PrivateWorkUnavailable(context.request_id)

    async def _read(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str,
        reload: bool = False,
    ) -> ProjectMemorySnapshot:
        context = await self._require(context, Capability.PRIVATE_WORK_READ_OWN)
        try:
            if reload:
                return await self._storage.reload(
                    scope=context.resource_scope,
                    namespace=namespace,
                )
            return await self._storage.load(
                scope=context.resource_scope,
                namespace=namespace,
            )
        except Exception as exc:
            raise self._map_storage_error(context, exc) from None

    async def _save(
        self,
        context: PrivateWorkContext,
        memory_data: dict,
        *,
        namespace: str,
        expected_version: int,
        capability: Capability = Capability.PRIVATE_WORK_CREATE,
    ) -> ProjectMemorySnapshot:
        context = await self._require(context, capability)
        try:
            return await self._storage.save(
                memory_data,
                scope=context.resource_scope,
                namespace=namespace,
                expected_version=expected_version,
            )
        except Exception as exc:
            raise self._map_storage_error(context, exc) from None

    async def status(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str = "default",
    ) -> PrivateMemoryStatus:
        snapshot = await self._read(context, namespace=namespace)
        return PrivateMemoryStatus(
            namespace=namespace,
            version=snapshot.version,
            fact_count=len(snapshot.memory.get("facts", [])),
            last_updated=str(snapshot.memory.get("lastUpdated", "")),
        )

    async def list(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str = "default",
    ) -> ProjectMemorySnapshot:
        return await self._read(context, namespace=namespace)

    async def reload(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str = "default",
    ) -> ProjectMemorySnapshot:
        return await self._read(context, namespace=namespace, reload=True)

    async def export(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str = "default",
    ) -> dict:
        return copy.deepcopy((await self._read(context, namespace=namespace)).memory)

    async def import_memory(
        self,
        context: PrivateWorkContext,
        memory_data: dict,
        *,
        namespace: str = "default",
        expected_version: int,
    ) -> ProjectMemorySnapshot:
        return await self._save(
            context,
            memory_data,
            namespace=namespace,
            expected_version=expected_version,
        )

    async def create_fact(
        self,
        context: PrivateWorkContext,
        *,
        namespace: str = "default",
        expected_version: int,
        content: str,
        category: str = "context",
        confidence: float = 0.8,
    ) -> ProjectMemorySnapshot:
        current = await self._read(context, namespace=namespace)
        if current.version != expected_version:
            raise PrivateWorkConflict(context.request_id)
        if not isinstance(content, str) or not content.strip():
            raise PrivateWorkInvalid(context.request_id)
        if not isinstance(category, str) or not category.strip() or len(category.strip()) > 32:
            raise PrivateWorkInvalid(context.request_id)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise PrivateWorkInvalid(context.request_id)
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise PrivateWorkInvalid(context.request_id)

        now = datetime.now(UTC).isoformat().removesuffix("+00:00") + "Z"
        memory_data = copy.deepcopy(current.memory)
        memory_data["lastUpdated"] = now
        memory_data.setdefault("facts", []).append(
            {
                "id": str(uuid.uuid4()),
                "content": content.strip(),
                "category": category.strip(),
                "confidence": confidence,
                "createdAt": now,
                "source": "manual",
            }
        )
        return await self._save(
            context,
            memory_data,
            namespace=namespace,
            expected_version=expected_version,
        )

    async def update(
        self,
        context: PrivateWorkContext,
        fact_id: str,
        *,
        namespace: str = "default",
        expected_version: int,
        content: str | None = None,
        category: str | None = None,
        confidence: float | None = None,
    ) -> ProjectMemorySnapshot:
        current = await self._read(context, namespace=namespace)
        if current.version != expected_version:
            raise PrivateWorkConflict(context.request_id)
        memory_data = copy.deepcopy(current.memory)
        selected = next(
            (fact for fact in memory_data.get("facts", []) if fact.get("id") == fact_id),
            None,
        )
        if selected is None:
            raise PrivateWorkNotFound(context.request_id)
        if content is not None:
            if not isinstance(content, str) or not content.strip():
                raise PrivateWorkInvalid(context.request_id)
            selected["content"] = content.strip()
        if category is not None:
            if not isinstance(category, str):
                raise PrivateWorkInvalid(context.request_id)
            selected["category"] = category.strip() or "context"
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise PrivateWorkInvalid(context.request_id)
            confidence = float(confidence)
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise PrivateWorkInvalid(context.request_id)
            selected["confidence"] = confidence
        return await self._save(
            context,
            memory_data,
            namespace=namespace,
            expected_version=expected_version,
        )

    async def delete(
        self,
        context: PrivateWorkContext,
        fact_id: str | None = None,
        *,
        namespace: str = "default",
        expected_version: int,
    ) -> ProjectMemorySnapshot:
        if fact_id is None:
            context = await self._require(context, Capability.PRIVATE_WORK_READ_OWN)
            try:
                return await self._storage.clear(
                    scope=context.resource_scope,
                    namespace=namespace,
                    expected_version=expected_version,
                )
            except Exception as exc:
                raise self._map_storage_error(context, exc) from None

        current = await self._read(context, namespace=namespace)
        if current.version != expected_version:
            raise PrivateWorkConflict(context.request_id)
        memory_data = copy.deepcopy(current.memory)
        facts = memory_data.get("facts", [])
        filtered = [fact for fact in facts if fact.get("id") != fact_id]
        if len(filtered) == len(facts):
            raise PrivateWorkNotFound(context.request_id)
        memory_data["facts"] = filtered
        return await self._save(
            context,
            memory_data,
            namespace=namespace,
            expected_version=expected_version,
            capability=Capability.PRIVATE_WORK_READ_OWN,
        )
