from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable

from fastapi import Request
from sqlalchemy.exc import DBAPIError

from app.gateway.routers.private_work_routes.dependencies import _runtime_dependency
from app.gateway.run_event_wakeup import RunEventWakeup
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkError, PrivateWorkUnavailable
from app.private_work.http_runtime import format_sse
from app.private_work.run_repository import PrivateRunRecord
from app.private_work.run_service import PrivateRunService
from app.reliability.errors import (
    ReliabilityDatabaseUnavailable,
    ReliabilityInvalidStreamCursor,
)
from deerflow.runtime.events.models import (
    STREAM_TERMINAL_ERROR_CODES,
    StoredStreamFrame,
    StreamCursorOutOfRange,
    stream_terminal_status_for_run_settlement,
)
from deerflow.runtime.events.stream import PostgresStreamBridge, parse_stream_cursor
from deerflow.runtime.runs.private_file_lifecycle import await_despite_cancellation
from deerflow.runtime.runs.schemas import RunStatus

_PRIVATE_STREAM_POLL_SECONDS = 0.25
_PRIVATE_STREAM_WAKEUP_WAIT_SECONDS = 2.5
_PRIVATE_STREAM_HEARTBEAT_SECONDS = 15.0
_PRIVATE_RUN_TERMINAL_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})


def _private_stream_bridge(
    request: Request,
    request_id: str,
) -> PostgresStreamBridge:
    bridge = getattr(request.app.state, "private_stream_bridge", None)
    if not isinstance(bridge, PostgresStreamBridge):
        raise PrivateWorkUnavailable(request_id)
    return bridge


def _require_run_runtime(
    request: Request,
    request_id: str,
) -> PostgresStreamBridge:
    _runtime_dependency(request, request_id, "project_scoped_checkpointer")
    return _private_stream_bridge(request, request_id)


def _run_event_wakeup(request: Request) -> RunEventWakeup | None:
    """Return the per-process wakeup dispatcher; absence degrades to polling."""
    wakeup = getattr(request.app.state, "run_event_wakeup", None)
    return wakeup if isinstance(wakeup, RunEventWakeup) else None


def _private_stream_cursor(request: Request, request_id: str) -> int:
    raw_cursor = request.headers.get("Last-Event-ID")
    if raw_cursor is None or raw_cursor == "":
        return 0
    try:
        return parse_stream_cursor(raw_cursor)
    except ValueError:
        raise ReliabilityInvalidStreamCursor(request_id)


def _private_stream_headers(
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
) -> dict[str, str]:
    run_path = f"/api/projects/{context.project_id}/private-work/threads/{thread_id}/runs/{run_id}"
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "Content-Location": run_path,
        # LangGraph SDK resolves this against its project-private API base.
        "Location": f"/threads/{thread_id}/runs/{run_id}/stream",
    }


async def _await_stream_database_operation[T](operation: Awaitable[T]) -> T:
    """Finish one session-owning operation before propagating cancellation."""

    outcome = await await_despite_cancellation(operation)
    if outcome.cancellation_pending:
        if not outcome.task.cancelled():
            outcome.task.exception()
        raise asyncio.CancelledError
    return outcome.result()


async def _read_private_stream_page(
    bridge: PostgresStreamBridge,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
    cursor: int,
    full_state_horizon: int | None = None,
) -> tuple[StoredStreamFrame, ...]:
    try:
        return await _await_stream_database_operation(
            bridge.read_after(
                context.resource_scope,
                thread_id,
                cursor=cursor,
                limit=100,
                run_id=run_id,
                full_state_horizon=full_state_horizon,
            )
        )
    except StreamCursorOutOfRange:
        raise ReliabilityInvalidStreamCursor(context.request_id) from None
    except DBAPIError:
        raise ReliabilityDatabaseUnavailable(context.request_id) from None


async def _read_private_full_state_horizon(
    bridge: PostgresStreamBridge,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
) -> int:
    """Freeze the reconnect replay compaction horizon once per connection.

    Root ``values`` frames carry the complete Run state, so catch-up replay
    only needs the newest one; frames at or above the horizon (including the
    live tail) are never dropped.
    """
    try:
        return await _await_stream_database_operation(
            bridge.latest_full_state_seq(
                context.resource_scope,
                thread_id,
                run_id=run_id,
            )
        )
    except DBAPIError:
        raise ReliabilityDatabaseUnavailable(context.request_id) from None


async def _durable_private_sse_consumer(
    *,
    bridge: PostgresStreamBridge,
    service: PrivateRunService,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
    request: Request,
    cursor: int,
    initial_frames: tuple[StoredStreamFrame, ...],
    cancel_on_disconnect: bool,
    wakeup: RunEventWakeup | None = None,
    full_state_horizon: int | None = None,
) -> AsyncIterator[str]:
    frames = initial_frames
    pending_terminal: StoredStreamFrame | None = None
    disconnected = False
    cancelled = False
    terminal_emitted = False
    loop = asyncio.get_running_loop()
    next_heartbeat = loop.time() + _PRIVATE_STREAM_HEARTBEAT_SECONDS
    waiter = wakeup.subscribe(run_id) if wakeup is not None else None
    try:
        while True:
            for frame in frames:
                if frame.terminal:
                    pending_terminal = frame
                    break
                cursor = int(frame.id)
                yield format_sse(
                    frame.event,
                    frame.data,
                    event_id=frame.id,
                )
            frames = ()

            if await request.is_disconnected():
                disconnected = True
                return

            # A persisted stream.end is the immutable browser cursor fact.
            # Do not ask the settled Run row to rewrite or reinterpret an
            # event that another consumer may already have observed.
            if pending_terminal is not None:
                terminal_cursor = int(pending_terminal.id)
                if terminal_cursor > cursor:
                    cursor = terminal_cursor
                    terminal_emitted = True
                    yield format_sse(
                        pending_terminal.event,
                        pending_terminal.data,
                        event_id=pending_terminal.id,
                    )
                return

            if pending_terminal is None:
                # Re-arm before reading so a NOTIFY that lands during the read
                # is not lost between this page and the next idle wait.
                if waiter is not None:
                    waiter.clear()
                frames = await _read_private_stream_page(
                    bridge,
                    context,
                    thread_id,
                    run_id,
                    cursor,
                    full_state_horizon,
                )
                if frames:
                    continue

            record = await _await_stream_database_operation(service.get(context, thread_id, run_id))
            if record.status in _PRIVATE_RUN_TERMINAL_STATUSES:
                terminal = await _await_stream_database_operation(
                    bridge.ensure_settled_terminal(
                        context.resource_scope,
                        thread_id,
                        run_id,
                        status=stream_terminal_status_for_run_settlement(
                            RunStatus(record.status),
                        ),
                        error_code=(record.error if record.error in STREAM_TERMINAL_ERROR_CODES else None),
                    )
                )
                terminal_cursor = int(terminal.id)
                if terminal_cursor > cursor:
                    cursor = terminal_cursor
                    terminal_emitted = True
                    yield format_sse(
                        terminal.event,
                        terminal.data,
                        event_id=terminal.id,
                    )
                return

            now = loop.time()
            if now >= next_heartbeat:
                yield ": heartbeat\n\n"
                next_heartbeat = loop.time() + _PRIVATE_STREAM_HEARTBEAT_SECONDS
            idle_seconds = _PRIVATE_STREAM_WAKEUP_WAIT_SECONDS if waiter is not None and wakeup is not None and wakeup.listening else _PRIVATE_STREAM_POLL_SECONDS
            deadline = min(
                idle_seconds,
                max(0.001, next_heartbeat - loop.time()),
            )
            if waiter is None:
                await asyncio.sleep(deadline)
            else:
                # NOTIFY is only an alarm clock: the timeout fallback keeps the
                # legacy poll behavior whenever a notification is lost.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(waiter.wait(), timeout=deadline)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        if wakeup is not None and waiter is not None:
            wakeup.unsubscribe(run_id, waiter)
        if (disconnected or cancelled) and cancel_on_disconnect and not terminal_emitted:
            await _persist_private_disconnect_cancel(
                service=service,
                context=context,
                thread_id=thread_id,
                run_id=run_id,
            )


async def _persist_private_disconnect_cancel(
    *,
    service: PrivateRunService,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
) -> None:
    cancel_task = asyncio.create_task(
        service.cancel(
            context,
            thread_id,
            run_id,
            reason="client_disconnected",
        )
    )
    try:
        await asyncio.shield(cancel_task)
    except asyncio.CancelledError:
        try:
            await cancel_task
        except PrivateWorkError:
            pass
    except PrivateWorkError:
        pass


async def _wait_for_durable_private_run(
    *,
    service: PrivateRunService,
    context: PrivateWorkContext,
    thread_id: str,
    run_id: str,
    request: Request,
    cancel_on_disconnect: bool,
) -> tuple[bool, PrivateRunRecord]:
    try:
        while True:
            record = await service.get(context, thread_id, run_id)
            if record.status in _PRIVATE_RUN_TERMINAL_STATUSES:
                return True, record
            if await request.is_disconnected():
                if cancel_on_disconnect:
                    await _persist_private_disconnect_cancel(
                        service=service,
                        context=context,
                        thread_id=thread_id,
                        run_id=run_id,
                    )
                return False, record
            await asyncio.sleep(_PRIVATE_STREAM_POLL_SECONDS)
    except asyncio.CancelledError:
        if cancel_on_disconnect:
            await _persist_private_disconnect_cancel(
                service=service,
                context=context,
                thread_id=thread_id,
                run_id=run_id,
            )
        raise
