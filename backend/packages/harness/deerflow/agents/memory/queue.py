"""Memory update queue with debounce mechanism."""

import asyncio
import logging
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.config.memory_config import get_memory_config
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.private_scope import PrivateResourceScope
from deerflow.trace_context import request_trace_context

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

    Unlike the legacy queue below, this queue never crosses into a raw Timer
    thread. That keeps asyncpg work on the runtime event loop and makes the
    captured :class:`MemoryQueueItem` the only source of project coordinates.
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


@dataclass
class ConversationContext:
    """Context for a conversation to be processed for memory update."""

    thread_id: str
    messages: list[Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    agent_name: str | None = None
    user_id: str | None = None
    deerflow_trace_id: str | None = None
    correction_detected: bool = False
    reinforcement_detected: bool = False


class MemoryUpdateQueue:
    """Queue for memory updates with debounce mechanism.

    This queue collects conversation contexts and processes them after
    a configurable debounce period. Multiple conversations received within
    the debounce window are batched together.
    """

    def __init__(self):
        """Initialize the memory update queue."""
        self._queue: list[ConversationContext] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._processing = False

    @staticmethod
    def _queue_key(
        thread_id: str,
        user_id: str | None,
        agent_name: str | None,
    ) -> tuple[str, str | None, str | None]:
        """Return the debounce identity for a memory update target."""
        return (thread_id, user_id, agent_name)

    def add(
        self,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None = None,
        user_id: str | None = None,
        deerflow_trace_id: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
    ) -> None:
        """Add a conversation to the update queue.

        Args:
            thread_id: The thread ID.
            messages: The conversation messages.
            agent_name: If provided, memory is stored per-agent. If None, uses global memory.
            user_id: The user ID captured at enqueue time. Stored in ConversationContext so it
                survives the threading.Timer boundary (ContextVar does not propagate across
                raw threads).
            deerflow_trace_id: Request trace id captured at enqueue time so the
                later Timer thread can attach it to memory LLM tracing metadata.
            correction_detected: Whether recent turns include an explicit correction signal.
            reinforcement_detected: Whether recent turns include a positive reinforcement signal.
        """
        config = get_memory_config()
        if not config.enabled:
            return

        with self._lock:
            self._enqueue_locked(
                thread_id=thread_id,
                messages=messages,
                agent_name=agent_name,
                user_id=user_id,
                deerflow_trace_id=deerflow_trace_id,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
            )
            self._reset_timer()

        logger.info("Memory update queued for thread %s, queue size: %d", thread_id, len(self._queue))

    def add_nowait(
        self,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None = None,
        user_id: str | None = None,
        deerflow_trace_id: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
    ) -> None:
        """Add a conversation and start processing immediately in the background."""
        config = get_memory_config()
        if not config.enabled:
            return

        with self._lock:
            self._enqueue_locked(
                thread_id=thread_id,
                messages=messages,
                agent_name=agent_name,
                user_id=user_id,
                deerflow_trace_id=deerflow_trace_id,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
            )
            self._schedule_timer(0)

        logger.info("Memory update queued for immediate processing on thread %s, queue size: %d", thread_id, len(self._queue))

    def _enqueue_locked(
        self,
        *,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None,
        user_id: str | None,
        deerflow_trace_id: str | None,
        correction_detected: bool,
        reinforcement_detected: bool,
    ) -> None:
        queue_key = self._queue_key(thread_id, user_id, agent_name)
        existing_context = next(
            (context for context in self._queue if self._queue_key(context.thread_id, context.user_id, context.agent_name) == queue_key),
            None,
        )
        merged_correction_detected = correction_detected or (existing_context.correction_detected if existing_context is not None else False)
        merged_reinforcement_detected = reinforcement_detected or (existing_context.reinforcement_detected if existing_context is not None else False)
        context = ConversationContext(
            thread_id=thread_id,
            messages=messages,
            agent_name=agent_name,
            user_id=user_id,
            deerflow_trace_id=deerflow_trace_id,
            correction_detected=merged_correction_detected,
            reinforcement_detected=merged_reinforcement_detected,
        )

        self._queue = [context for context in self._queue if self._queue_key(context.thread_id, context.user_id, context.agent_name) != queue_key]
        self._queue.append(context)

    def _reset_timer(self) -> None:
        """Reset the debounce timer."""
        config = get_memory_config()
        self._schedule_timer(config.debounce_seconds)

        logger.debug("Memory update timer set for %ss", config.debounce_seconds)

    def _schedule_timer(self, delay_seconds: float) -> None:
        """Schedule queue processing after the provided delay."""
        # Cancel existing timer if any
        if self._timer is not None:
            self._timer.cancel()

        self._timer = threading.Timer(
            delay_seconds,
            self._process_queue,
        )
        self._timer.daemon = True
        self._timer.start()

    def _process_queue(self) -> None:
        """Process all queued conversation contexts."""
        # Import here to avoid circular dependency
        from deerflow.agents.memory.updater import MemoryUpdater

        with self._lock:
            if self._processing:
                # Preserve immediate flush semantics even if another worker is active.
                self._schedule_timer(0)
                return

            if not self._queue:
                return

            self._processing = True
            contexts_to_process = self._queue.copy()
            self._queue.clear()
            self._timer = None

        logger.info("Processing %d queued memory updates", len(contexts_to_process))

        try:
            updater = MemoryUpdater()

            for context in contexts_to_process:
                # Rebind the request-trace ContextVar from the value captured at
                # enqueue time so ``TraceContextFilter`` attaches the correct
                # trace id to every log record emitted below (this Timer thread
                # does not inherit the enqueue-thread's ContextVar). Each
                # iteration is scoped independently so id A does not leak into
                # id B's logs.
                trace_ctx = request_trace_context(context.deerflow_trace_id) if context.deerflow_trace_id else nullcontext()
                with trace_ctx:
                    try:
                        logger.info("Updating memory for thread %s", context.thread_id)
                        success = updater.update_memory(
                            messages=context.messages,
                            thread_id=context.thread_id,
                            agent_name=context.agent_name,
                            correction_detected=context.correction_detected,
                            reinforcement_detected=context.reinforcement_detected,
                            user_id=context.user_id,
                            deerflow_trace_id=context.deerflow_trace_id,
                        )
                        if success:
                            logger.info("Memory updated successfully for thread %s", context.thread_id)
                        else:
                            logger.warning("Memory update skipped/failed for thread %s", context.thread_id)
                    except Exception as e:
                        logger.error("Error updating memory for thread %s: %s", context.thread_id, e)

                    # Small delay between updates to avoid rate limiting
                    if len(contexts_to_process) > 1:
                        time.sleep(0.5)

        finally:
            with self._lock:
                self._processing = False

    def flush(self) -> None:
        """Force immediate processing of the queue.

        This is useful for testing or graceful shutdown.
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

        self._process_queue()

    def flush_nowait(self) -> None:
        """Start queue processing immediately in a background thread."""
        with self._lock:
            # Daemon thread: queued messages may be lost if the process exits
            # before _process_queue completes. Acceptable for best-effort memory updates.
            self._schedule_timer(0)

    def clear(self) -> None:
        """Clear the queue without processing.

        This is useful for testing.
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._queue.clear()
            self._processing = False

    @property
    def pending_count(self) -> int:
        """Get the number of pending updates."""
        with self._lock:
            return len(self._queue)

    @property
    def is_processing(self) -> bool:
        """Check if the queue is currently being processed."""
        with self._lock:
            return self._processing


# Global singleton instance
_memory_queue: MemoryUpdateQueue | None = None
_queue_lock = threading.Lock()
_project_memory_queue: ProjectMemoryUpdateQueue | None = None


def get_memory_queue() -> MemoryUpdateQueue:
    """Get the global memory update queue singleton.

    Returns:
        The memory update queue instance.
    """
    global _memory_queue
    with _queue_lock:
        if _memory_queue is None:
            _memory_queue = MemoryUpdateQueue()
        return _memory_queue


def reset_memory_queue() -> None:
    """Reset the global memory queue.

    This is useful for testing.
    """
    global _memory_queue
    with _queue_lock:
        if _memory_queue is not None:
            _memory_queue.clear()
        _memory_queue = None


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
