"""One canonical Run-to-stream terminal status spelling.

The live Worker terminal path historically wrote ``str(RunStatus.success)``
(``"success"``) while the settlement repair path wrote ``"completed"``; both
spellings are schema-legal, so the divergence surfaced only when the
suspension settlement compared its repair frame against an already-published
live frame and rolled the whole settlement back. These tests pin:

1. one typed adapter projects every terminal Run settlement to a stream status;
2. terminal frame construction canonicalizes retained stream spellings;
3. the settlement repair treats a retained legacy ``"success"`` frame as
   semantically equal to ``"completed"`` instead of failing the settlement;
4. genuinely disagreeing terminals are still rejected.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from deerflow.runtime.events.models import (
    StreamFrame,
    StreamWriteAuthorityRequired,
    canonical_stream_terminal_status,
    stream_terminal_status_for_run_settlement,
)
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.runs.schemas import RunStatus


class _StatementResult:
    def __init__(self, value=None) -> None:
        self._value = value

    def one_or_none(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    def __init__(self) -> None:
        self._execute_values = iter(())

    async def execute(self, _statement, _params=None):
        return _StatementResult(next(self._execute_values))


def _scope() -> PrivateResourceScope:
    return PrivateResourceScope(
        project_id=str(uuid.uuid4()),
        owner_user_id=str(uuid.uuid4()),
        membership_version=1,
    )


def test_stream_status_canonicalization_preserves_legacy_success_rows() -> None:
    assert canonical_stream_terminal_status("success") == "completed"
    for identity in ("completed", "error", "timeout", "interrupted", "cancelled", "failed"):
        assert canonical_stream_terminal_status(identity) == identity


@pytest.mark.parametrize(
    ("run_status", "expected_stream_status"),
    (
        (RunStatus.success, "completed"),
        (RunStatus.error, "error"),
        (RunStatus.timeout, "timeout"),
        (RunStatus.interrupted, "interrupted"),
    ),
)
def test_run_settlement_status_maps_to_stream_terminal_status(
    run_status: RunStatus,
    expected_stream_status: str,
) -> None:
    assert stream_terminal_status_for_run_settlement(run_status) == expected_stream_status


def test_stream_end_writes_the_canonical_success_spelling() -> None:
    assert StreamFrame.end(status="success").data == {"status": "completed"}
    assert StreamFrame.end(status="completed").data == {"status": "completed"}
    assert StreamFrame.end(status="interrupted").data == {"status": "interrupted"}
    with pytest.raises(ValueError):
        StreamFrame.end(status="running")


def _repair_store() -> DbRunEventStore:
    return DbRunEventStore(AsyncMock(), run_event_notify_enabled=False)


def _prepare_repair(
    store: DbRunEventStore,
    *,
    terminal_content: str,
) -> _Session:
    job_id = uuid.uuid4()
    job = SimpleNamespace(status="succeeded")
    run = SimpleNamespace(job_id=job_id, status="success", error=None)
    terminal = SimpleNamespace(
        content=terminal_content,
        event_metadata={
            "stream_terminal": True,
            "content_is_json": True,
            "content_is_dict": True,
        },
        seq=17,
    )

    class _RepairSession(_Session):
        def __init__(self) -> None:
            super().__init__()
            self._execute_values = iter((job, run, terminal))

        async def scalar(self, _statement):
            return job_id

    session = _RepairSession()
    session.flush = AsyncMock()  # type: ignore[attr-defined]
    return session


@pytest.mark.asyncio
async def test_terminal_repair_accepts_legacy_success_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live-published legacy ``success`` frame settles as ``completed``."""

    store = _repair_store()
    stream_rows: list[object] = []
    monkeypatch.setattr(store, "_lock_stream_governance", AsyncMock())
    monkeypatch.setattr(
        store,
        "_lock_event_sequence",
        AsyncMock(return_value=SimpleNamespace(high_watermark=17)),
    )
    monkeypatch.setattr(
        store,
        "_stream_row",
        lambda row, *, created: stream_rows.append((row, created)) or object(),
    )
    monkeypatch.setattr(store, "_notify_stream_append", AsyncMock())
    session = _prepare_repair(store, terminal_content='{"status": "success"}')

    await store.ensure_settled_stream_terminal(
        session,  # type: ignore[arg-type]
        scope=_scope(),
        thread_id="thread-1",
        run_id="run-1",
        status="completed",
    )

    assert stream_rows and stream_rows[0][1] is False
    session.flush.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_terminal_repair_still_rejects_a_real_disagreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _repair_store()
    monkeypatch.setattr(store, "_lock_stream_governance", AsyncMock())
    monkeypatch.setattr(
        store,
        "_lock_event_sequence",
        AsyncMock(return_value=SimpleNamespace(high_watermark=17)),
    )
    monkeypatch.setattr(store, "_notify_stream_append", AsyncMock())
    session = _prepare_repair(store, terminal_content='{"status": "interrupted"}')

    with pytest.raises(
        StreamWriteAuthorityRequired,
        match="existing stream terminal",
    ):
        await store.ensure_settled_stream_terminal(
            session,  # type: ignore[arg-type]
            scope=_scope(),
            thread_id="thread-1",
            run_id="run-1",
            status="completed",
        )
