"""Process-lifetime PostgreSQL ownership for the automation scheduler."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.automations.errors import AutomationUnavailable

logger = logging.getLogger(__name__)

# Session-level advisory lock held by the one Gateway process allowed to run
# automation startup reconciliation and polling. Keep this distinct from the
# transaction-level admission lock used by AutomationOccurrenceService.
AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY = 0x0DEE_12F1_0A55_0007
_OWNERSHIP_REQUEST_ID = "automation-scheduler-ownership"
_ACQUIRE_SQL = sa.text(
    """SELECT pg_backend_pid() AS backend_pid,
              pg_try_advisory_lock(:lock_key) AS acquired"""
)
_VERIFY_SQL = sa.text(
    """SELECT pg_backend_pid() AS backend_pid,
              EXISTS (
                  SELECT 1
                  FROM pg_locks
                  WHERE locktype = 'advisory'
                    AND pid = pg_backend_pid()
                    AND granted
                    AND classid = (
                        (CAST(:lock_key AS bigint) >> 32) & 4294967295
                    )::oid
                    AND objid = (
                        CAST(:lock_key AS bigint) & 4294967295
                    )::oid
                    AND objsubid = 1
              ) AS owns_lock"""
)


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
        self._backend_pid: int | None = None
        self._lost = False
        self._operation_lock = asyncio.Lock()

    @property
    def is_acquired(self) -> bool:
        return self._connection is not None and not self._lost

    @property
    def is_lost(self) -> bool:
        return self._lost

    @property
    def backend_pid(self) -> int | None:
        return self._backend_pid

    async def acquire(self) -> None:
        """Acquire exclusive scheduler ownership or fail closed."""
        async with self._operation_lock:
            if self._lost:
                raise AutomationUnavailable(_OWNERSHIP_REQUEST_ID)
            if self._connection is not None:
                return

            connection: AsyncConnection | None = None
            try:
                connection = await self._engine.connect()
                result = await connection.execute(
                    _ACQUIRE_SQL,
                    {"lock_key": AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY},
                )
                backend_pid, acquired = result.one()
                # End the implicit transaction while retaining the session lock.
                await connection.commit()
                if not acquired:
                    await connection.close()
                    raise AutomationUnavailable(_OWNERSHIP_REQUEST_ID)
                self._connection = connection
                self._backend_pid = int(backend_pid)
            except AutomationUnavailable:
                raise
            except Exception as error:  # noqa: BLE001 - driver failures may bypass SQLAlchemy wrappers
                if connection is not None:
                    await self._cleanup_failed_acquire(connection)
                raise AutomationUnavailable(_OWNERSHIP_REQUEST_ID) from error
            except BaseException:
                if connection is not None:
                    await self._cleanup_failed_acquire(connection)
                raise

    async def verify(self) -> None:
        """Verify the original physical session still owns the lock.

        This query observes ``pg_locks`` and never calls an advisory-lock
        acquisition function, so a heartbeat cannot increase the session lock
        count. Any uncertainty permanently loses ownership for this object.
        """
        async with self._operation_lock:
            connection = self._connection
            backend_pid = self._backend_pid
            if self._lost or connection is None or backend_pid is None:
                raise AutomationUnavailable(_OWNERSHIP_REQUEST_ID)
            try:
                result = await connection.execute(
                    _VERIFY_SQL,
                    {"lock_key": AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY},
                )
                current_pid, owns_lock = result.one()
                await connection.commit()
            except Exception as error:  # noqa: BLE001 - driver disconnects may bypass SQLAlchemy wrappers
                await self._mark_lost(connection)
                raise AutomationUnavailable(_OWNERSHIP_REQUEST_ID) from error
            if int(current_pid) != backend_pid or not owns_lock:
                await self._mark_lost(connection)
                raise AutomationUnavailable(_OWNERSHIP_REQUEST_ID)

    async def release(self) -> None:
        """Release scheduler ownership before the engine pool is disposed."""
        async with self._operation_lock:
            connection = self._connection
            self._connection = None
            if connection is None:
                return
            if not self._lost:
                self._backend_pid = None

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

    async def _mark_lost(self, connection: AsyncConnection) -> None:
        self._lost = True
        self._connection = None
        cleanup = asyncio.create_task(self._invalidate_and_close(connection))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await asyncio.shield(cleanup)
            raise

    @staticmethod
    async def _invalidate_and_close(connection: AsyncConnection) -> None:
        try:
            await connection.invalidate()
        except Exception as error:  # noqa: BLE001 - cleanup must contain driver failures
            logger.error(
                "Automation scheduler lost-ownership connection invalidation failed: error_type=%s",
                type(error).__name__,
            )
        try:
            await connection.close()
        except Exception as error:  # noqa: BLE001 - cleanup must contain driver failures
            logger.error(
                "Automation scheduler lost-ownership connection close failed: error_type=%s",
                type(error).__name__,
            )

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
        except Exception as error:  # noqa: BLE001 - cleanup must contain driver failures
            # Never return an uncertain physical session to the pool: invalidating
            # it makes PostgreSQL release any remaining session lock on disconnect.
            logger.error(
                "Automation scheduler ownership release failed: error_type=%s",
                type(error).__name__,
            )
            try:
                await connection.invalidate()
            except Exception as invalidate_error:  # noqa: BLE001 - cleanup containment
                logger.error(
                    "Automation scheduler ownership connection invalidation failed: error_type=%s",
                    type(invalidate_error).__name__,
                )
        finally:
            try:
                await connection.close()
            except Exception as close_error:  # noqa: BLE001 - cleanup containment
                logger.error(
                    "Automation scheduler ownership connection close failed: error_type=%s",
                    type(close_error).__name__,
                )


__all__ = [
    "AUTOMATION_SCHEDULER_OWNERSHIP_LOCK_KEY",
    "AutomationSchedulerOwnership",
]
