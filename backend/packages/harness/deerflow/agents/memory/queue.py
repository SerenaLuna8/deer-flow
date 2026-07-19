"""Memory update queue with debounce mechanism."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.config.memory_config import get_memory_config
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.private_scope import PrivateResourceScope

logger = logging.getLogger(__name__)


ProjectMemoryQueueKey = tuple[str, str, str, str]


@dataclass(frozen=True, slots=True)
class MemoryQueueItem:
    """Project memory update with every authority coordinate frozen at enqueue."""

    scope: PrivateResourceScope
    thread_id: str
    run_id: str
    namespace: str
    membership_version: int
    messages: tuple[Any, ...]
    correction_detected: bool = False
    reinforcement_detected: bool = False
    deerflow_trace_id: str | None = None

    @property
    def key(self) -> ProjectMemoryQueueKey:
        return (
            self.scope.project_id,
            self.scope.owner_user_id,
            self.namespace,
            self.thread_id,
        )


class MemoryMembershipRevalidator(Protocol):
    """Small async boundary used before a queued project-memory write."""

    async def is_active(self, scope: PrivateResourceScope) -> bool: ...


class ProjectMemoryUpdater(Protocol):
    async def aupdate_project_memory(self, **kwargs: Any) -> bool: ...


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


class ProjectMemoryUpdateQueue:
    """Asyncio-debounced queue for PostgreSQL project memory updates.

    The queue stays on the runtime event loop, so asyncpg work never crosses a
    raw timer thread and the captured :class:`MemoryQueueItem` remains the only
    source of project coordinates.
    """

    def __init__(
        self,
        storage: Any,
        *,
        updater: ProjectMemoryUpdater | None = None,
        revalidator: MemoryMembershipRevalidator,
        debounce_seconds: float | None = None,
    ) -> None:
        if updater is None:
            from deerflow.agents.memory.updater import MemoryUpdater

            updater = MemoryUpdater()
        self._storage = storage
        self._updater = updater
        self._revalidator = revalidator
        self._debounce_seconds = debounce_seconds
        self._pending: dict[ProjectMemoryQueueKey, MemoryQueueItem] = {}
        self._tasks: dict[ProjectMemoryQueueKey, asyncio.Task[None]] = {}

    def enqueue(
        self,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
        namespace: str,
        messages: list[Any] | tuple[Any, ...],
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
        deerflow_trace_id: str | None = None,
    ) -> MemoryQueueItem:
        if type(scope) is not PrivateResourceScope:
            raise TypeError("project memory queue requires PrivateResourceScope")
        if not thread_id or not run_id or not namespace:
            raise ValueError("project memory queue coordinates must be non-empty")
        item = MemoryQueueItem(
            scope=scope,
            thread_id=thread_id,
            run_id=run_id,
            namespace=namespace,
            membership_version=scope.membership_version,
            messages=tuple(messages),
            correction_detected=correction_detected,
            reinforcement_detected=reinforcement_detected,
            deerflow_trace_id=deerflow_trace_id,
        )
        existing = self._tasks.pop(item.key, None)
        if existing is not None:
            existing.cancel()
        self._pending[item.key] = item
        self._tasks[item.key] = asyncio.create_task(self._debounced_flush(item.key))
        return item

    async def _debounced_flush(self, key: ProjectMemoryQueueKey) -> None:
        try:
            delay = self._debounce_seconds
            if delay is None:
                delay = get_memory_config().debounce_seconds
            await asyncio.sleep(delay)
            await self._process(key)
        except asyncio.CancelledError:
            return

    async def _process(self, key: ProjectMemoryQueueKey) -> bool:
        item = self._pending.pop(key, None)
        self._tasks.pop(key, None)
        if item is None:
            return False
        if item.membership_version != item.scope.membership_version:
            return False
        if not await self._revalidator.is_active(item.scope):
            logger.info(
                "Dropped project memory update for inactive membership: project=%s owner=%s",
                item.scope.project_id,
                item.scope.owner_user_id,
            )
            return False
        return await self._updater.aupdate_project_memory(
            storage=self._storage,
            scope=item.scope,
            namespace=item.namespace,
            messages=item.messages,
            thread_id=item.thread_id,
            run_id=item.run_id,
            correction_detected=item.correction_detected,
            reinforcement_detected=item.reinforcement_detected,
            deerflow_trace_id=item.deerflow_trace_id,
        )

    async def flush(self, key: ProjectMemoryQueueKey) -> bool:
        task = self._tasks.pop(key, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return await self._process(key)

    async def flush_all(self) -> list[bool]:
        return [await self.flush(key) for key in tuple(self._pending)]

    async def clear(self) -> None:
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        self._pending.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def pending_count(self) -> int:
        return len(self._pending)


_project_memory_queue: ProjectMemoryUpdateQueue | None = None


def get_project_memory_queue() -> ProjectMemoryUpdateQueue:
    """Get the process project-memory queue after persistence initialization."""

    global _project_memory_queue
    if _project_memory_queue is None:
        from deerflow.agents.memory.storage import ProjectMemoryStorage
        from deerflow.persistence import get_session_factory

        session_factory = get_session_factory()
        _project_memory_queue = ProjectMemoryUpdateQueue(
            ProjectMemoryStorage(session_factory),
            revalidator=ProjectMemoryMembershipRevalidator(session_factory),
        )
    return _project_memory_queue


async def reset_project_memory_queue() -> None:
    """Cancel pending project-memory debounce tasks and reset the singleton."""

    global _project_memory_queue
    if _project_memory_queue is not None:
        await _project_memory_queue.clear()
    _project_memory_queue = None
