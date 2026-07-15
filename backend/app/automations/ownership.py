"""Process-lifetime PostgreSQL ownership for the automation scheduler."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.automations.errors import AutomationUnavailable

logger = logging.getLogger(__name__)

# Session-level advisory lock held by the one Gateway process allowed to run
# automation startup reconciliation and polling. Keep this distinct from the
# transaction-level admission lock used by AutomationOccurrenceService.
AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY = 0x0DEE_12F1_0A55_0007
_OWNERSHIP_REQUEST_ID = "automation-scheduler-ownership"


class AutomationSchedulerOwnership:
    """Hold one PostgreSQL session advisory lock for the runtime lifetime.

    The connection intentionally remains checked out while ownership is held:
    PostgreSQL session advisory locks belong to the physical connection, not a
    transaction or SQLAlchemy session. Returning it to the pool would make the
    lock outlive this object's ownership state.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._connection: AsyncConnection | None = None
        self._operation_lock = asyncio.Lock()

    @property
    def is_acquired(self) -> bool:
        return self._connection is not None

    async def acquire(self) -> None:
        """Acquire exclusive scheduler ownership or fail closed."""
        async with self._operation_lock:
            if self._connection is not None:
                return

            connection: AsyncConnection | None = None
            lock_may_be_held = False
            try:
                connection = await self._engine.connect()
                acquired = await connection.scalar(
                    sa.text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY},
                )
                lock_may_be_held = bool(acquired)
                # End the implicit transaction while retaining the session lock.
                await connection.commit()
                if not lock_may_be_held:
                    await connection.close()
                    raise AutomationUnavailable(_OWNERSHIP_REQUEST_ID)
                self._connection = connection
            except AutomationUnavailable:
                raise
            except SQLAlchemyError as error:
                if connection is not None:
                    await self._cleanup_failed_acquire(connection)
                raise AutomationUnavailable(_OWNERSHIP_REQUEST_ID) from error
            except BaseException:
                if connection is not None:
                    await self._cleanup_failed_acquire(connection)
                raise

    async def release(self) -> None:
        """Release scheduler ownership before the engine pool is disposed."""
        async with self._operation_lock:
            connection = self._connection
            self._connection = None
            if connection is None:
                return

            cleanup = asyncio.create_task(self._unlock_and_close(connection))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Do not return a still-locked physical connection to the pool
                # when shutdown receives a second cancellation signal.
                try:
                    await asyncio.shield(cleanup)
                except Exception:
                    logger.exception("Automation scheduler ownership cleanup failed after cancellation")
                raise

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[AutomationSchedulerOwnership]:
        """Acquire ownership for the surrounding runtime lifespan."""
        await self.acquire()
        try:
            yield self
        finally:
            await self.release()

    @staticmethod
    async def _cleanup_failed_acquire(
        connection: AsyncConnection,
    ) -> None:
        # A lost query result is ambiguous: PostgreSQL may have acquired the
        # session lock even though the client never received ``true``. Always
        # attempt unlock (and invalidate on failure) before returning it.
        cleanup = asyncio.create_task(AutomationSchedulerOwnership._unlock_and_close(connection))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await asyncio.shield(cleanup)
            raise

    @staticmethod
    async def _unlock_and_close(connection: AsyncConnection) -> None:
        try:
            released = await connection.scalar(
                sa.text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY},
            )
            await connection.commit()
            if not released:
                logger.warning("Automation scheduler ownership was already absent during release")
        except SQLAlchemyError:
            # Never return an uncertain physical session to the pool: invalidating
            # it makes PostgreSQL release any remaining session lock on disconnect.
            logger.exception("Automation scheduler ownership release failed")
            try:
                await connection.invalidate()
            except SQLAlchemyError:
                logger.exception("Automation scheduler ownership connection invalidation failed")
        finally:
            await connection.close()


__all__ = [
    "AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY",
    "AutomationSchedulerOwnership",
]
