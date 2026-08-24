from __future__ import annotations

import hashlib
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

import app.private_work.file_finalizer as file_finalizer_module
from app.private_work.errors import (
    PrivateWorkQuotaUnavailable,
    PrivateWorkUnavailable,
)
from app.private_work.file_finalizer import FinalizationResult, PrivateFileFinalizer
from deerflow.sandbox.sandbox import AuthorizationRevoked
from deerflow.workspace_changes.types import WorkspaceChangeLimits


class _SqlStateError(Exception):
    sqlstate = "08006"


class _ChunkStream:
    def __init__(self, chunks: tuple[SimpleNamespace, ...]) -> None:
        self._chunks = iter(chunks)
        self.closed = False

    def __aiter__(self) -> _ChunkStream:
        return self

    async def __anext__(self) -> SimpleNamespace:
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self) -> None:
        self.closed = True


class _StreamingSession:
    def __init__(self, stream: _ChunkStream) -> None:
        self.stream = stream
        self.statement = None

    async def stream_scalars(self, statement):
        self.statement = statement
        return self.stream


class _CommitAckLossTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        if args[0] is None:
            raise ConnectionError("private commit acknowledgement sentinel")


class _CommitAckLossSession:
    async def __aenter__(self) -> _CommitAckLossSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    def begin(self) -> _CommitAckLossTransaction:
        return _CommitAckLossTransaction()


class _CommitAckLossFactory:
    def __call__(self) -> _CommitAckLossSession:
        return _CommitAckLossSession()


class _PassTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        del args


class _PassSession:
    async def __aenter__(self) -> _PassSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    def begin(self) -> _PassTransaction:
        return _PassTransaction()


class _PassFactory:
    def __call__(self) -> _PassSession:
        return _PassSession()


def _row(file_id: uuid.UUID, path: str, content: bytes) -> SimpleNamespace:
    return SimpleNamespace(
        id=file_id,
        logical_path=path,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _chunk(file_id: uuid.UUID, index: int, content: bytes) -> SimpleNamespace:
    return SimpleNamespace(
        file_id=file_id,
        chunk_index=index,
        content=content,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


@pytest.mark.asyncio
async def test_authoritative_snapshots_stream_chunks_and_keep_empty_files() -> None:
    first_id = uuid.UUID(int=1)
    second_id = uuid.UUID(int=2)
    empty_id = uuid.UUID(int=3)
    first_content = b"alpha\nbeta\n"
    second_content = b"gamma\n"
    rows = (
        _row(first_id, "outputs/first.md", first_content),
        _row(second_id, "outputs/second.md", second_content),
        _row(empty_id, "outputs/empty.md", b""),
    )
    stream = _ChunkStream(
        (
            _chunk(first_id, 0, first_content[:6]),
            _chunk(first_id, 1, first_content[6:]),
            _chunk(second_id, 0, second_content),
        )
    )
    session = _StreamingSession(stream)

    snapshots = await PrivateFileFinalizer(object())._authoritative_file_snapshots(
        session,
        SimpleNamespace(context=SimpleNamespace(request_id="request-1")),
        rows,
        limits=WorkspaceChangeLimits(),
    )

    assert tuple(snapshots) == (first_id, second_id, empty_id)
    assert snapshots[first_id].text == first_content.decode()
    assert snapshots[second_id].text == second_content.decode()
    assert snapshots[empty_id].text == ""
    assert stream.closed is True
    assert session.statement is not None
    assert session.statement.get_execution_options()["yield_per"] == 128


@pytest.mark.asyncio
async def test_commit_retries_one_rolled_back_transaction_body() -> None:
    expected = FinalizationResult((), (), (), None, ())
    finalizer = PrivateFileFinalizer(object())
    calls = 0
    retryable = getattr(
        file_finalizer_module,
        "_RetryableFinalizationCommit",
    )

    async def commit_attempt(*_args: object) -> FinalizationResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise retryable(
                phase="commit_audit",
                failure_type="ConnectionError",
            )
        return expected

    finalizer._commit_attempt = commit_attempt  # type: ignore[attr-defined,method-assign]

    result = await finalizer._commit(
        SimpleNamespace(context=SimpleNamespace(request_id="request-1")),
        SimpleNamespace(),
        (),
        (),
        (),
    )

    assert result is expected
    assert calls == 2


def test_transient_database_classifier_accepts_invalidated_sqlalchemy_error() -> None:
    error = DBAPIError(
        "SELECT 1",
        {},
        ConnectionError("wrapped connection sentinel"),
        connection_invalidated=True,
    )

    assert file_finalizer_module._is_transient_database_error(error)  # type: ignore[attr-defined]


def test_transient_database_classifier_reads_sqlstate_from_dbapi_orig() -> None:
    error = DBAPIError(
        "SELECT 1",
        {},
        _SqlStateError("wrapped sqlstate sentinel"),
        connection_invalidated=False,
    )

    assert file_finalizer_module._is_transient_database_error(error)  # type: ignore[attr-defined]


def test_transient_database_classifier_rejects_integrity_error() -> None:
    error = IntegrityError(
        "INSERT INTO private_files ...",
        {},
        RuntimeError("integrity sentinel"),
    )

    assert not file_finalizer_module._is_transient_database_error(error)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_commit_transaction_retries_only_typed_transient_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    finalizer = PrivateFileFinalizer(_PassFactory())
    state = SimpleNamespace(
        result=None,
        body_complete=False,
        phase="commit_quota",
    )
    retryable = getattr(
        file_finalizer_module,
        "_RetryableFinalizationCommit",
    )

    with caplog.at_level(logging.WARNING), pytest.raises(retryable):
        async with finalizer._commit_transaction(  # type: ignore[attr-defined]
            SimpleNamespace(),
            state,
        ):
            raise PrivateWorkQuotaUnavailable("private quota sentinel")

    assert "phase=commit_quota" in caplog.text
    assert "reason_code=quota_unavailable" in caplog.text
    assert "outcome=retrying" in caplog.text
    assert "private quota sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_commit_transaction_does_not_retry_generic_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    finalizer = PrivateFileFinalizer(_PassFactory())
    state = SimpleNamespace(
        result=None,
        body_complete=False,
        phase="commit_files",
    )

    with caplog.at_level(logging.ERROR), pytest.raises(PrivateWorkUnavailable):
        async with finalizer._commit_transaction(  # type: ignore[attr-defined]
            SimpleNamespace(),
            state,
        ):
            raise PrivateWorkUnavailable("private invariant sentinel")

    assert "phase=commit_files" in caplog.text
    assert "outcome=failed" in caplog.text
    assert "private invariant sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_commit_transaction_does_not_retry_programming_error() -> None:
    finalizer = PrivateFileFinalizer(_PassFactory())

    with pytest.raises(ValueError, match="programming sentinel"):
        async with finalizer._commit_transaction(  # type: ignore[attr-defined]
            SimpleNamespace(),
            SimpleNamespace(
                result=None,
                body_complete=False,
                phase="commit_files",
            ),
        ):
            raise ValueError("programming sentinel")


@pytest.mark.asyncio
async def test_commit_ack_loss_uses_complete_receipt_without_leaking_error_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected = FinalizationResult((), (), (), None, ())
    state = SimpleNamespace(
        result=None,
        body_complete=False,
        phase="commit_authority",
    )
    finalizer = PrivateFileFinalizer(_CommitAckLossFactory())
    finalizer._read_finalization_receipt = AsyncMock(  # type: ignore[attr-defined,method-assign]
        return_value="complete",
    )

    with caplog.at_level(logging.WARNING):
        async with finalizer._commit_transaction(  # type: ignore[attr-defined]
            SimpleNamespace(),
            state,
        ):
            state.result = expected
            state.body_complete = True
            state.phase = "commit_ack"

    assert state.result is expected
    assert "phase=commit_ack" in caplog.text
    assert "outcome=recovered" in caplog.text
    assert "failure_type=ConnectionError" in caplog.text
    assert "private commit acknowledgement sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_commit_ack_retries_one_failed_receipt_read_without_leaking_error_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected = FinalizationResult((), (), (), None, ())
    state = SimpleNamespace(
        result=None,
        body_complete=False,
        phase="commit_authority",
    )
    finalizer = PrivateFileFinalizer(_CommitAckLossFactory())
    finalizer._read_finalization_receipt = AsyncMock(  # type: ignore[attr-defined,method-assign]
        side_effect=(
            PrivateWorkUnavailable("private receipt read sentinel"),
            "complete",
        ),
    )

    with caplog.at_level(logging.WARNING):
        async with finalizer._commit_transaction(  # type: ignore[attr-defined]
            SimpleNamespace(),
            state,
        ):
            state.result = expected
            state.body_complete = True
            state.phase = "commit_ack"

    assert finalizer._read_finalization_receipt.await_count == 2  # type: ignore[attr-defined]
    assert "phase=commit_reconcile" in caplog.text
    assert "outcome=retrying" in caplog.text
    assert "phase=commit_ack" in caplog.text
    assert "outcome=recovered" in caplog.text
    assert "private receipt read sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_commit_stops_after_one_safe_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    finalizer = PrivateFileFinalizer(object())
    calls = 0
    retryable = getattr(
        file_finalizer_module,
        "_RetryableFinalizationCommit",
    )

    async def commit_attempt(*_args: object) -> FinalizationResult:
        nonlocal calls
        calls += 1
        raise retryable(
            phase="commit_diff",
            failure_type="ConnectionError",
        )

    finalizer._commit_attempt = commit_attempt  # type: ignore[attr-defined,method-assign]

    with caplog.at_level(logging.WARNING), pytest.raises(PrivateWorkUnavailable):
        await finalizer._commit(
            SimpleNamespace(context=SimpleNamespace(request_id="request-1")),
            SimpleNamespace(),
            (),
            (),
            (),
        )

    assert calls == 2
    assert "phase=commit_diff" in caplog.text
    assert "outcome=failed" in caplog.text


@pytest.mark.asyncio
async def test_commit_never_retries_revoked_authority() -> None:
    finalizer = PrivateFileFinalizer(_CommitAckLossFactory())
    finalizer._read_finalization_receipt = AsyncMock()  # type: ignore[attr-defined,method-assign]

    with pytest.raises(AuthorizationRevoked):
        async with finalizer._commit_transaction(  # type: ignore[attr-defined]
            SimpleNamespace(),
            SimpleNamespace(
                result=None,
                body_complete=False,
                phase="commit_authority",
            ),
        ):
            raise AuthorizationRevoked

    finalizer._read_finalization_receipt.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_commit_ack_never_retries_revoked_receipt_authority() -> None:
    expected = FinalizationResult((), (), (), None, ())
    state = SimpleNamespace(
        result=None,
        body_complete=False,
        phase="commit_authority",
    )
    finalizer = PrivateFileFinalizer(_CommitAckLossFactory())
    finalizer._read_finalization_receipt = AsyncMock(  # type: ignore[attr-defined,method-assign]
        side_effect=AuthorizationRevoked,
    )

    with pytest.raises(AuthorizationRevoked):
        async with finalizer._commit_transaction(  # type: ignore[attr-defined]
            SimpleNamespace(),
            state,
        ):
            state.result = expected
            state.body_complete = True
            state.phase = "commit_ack"

    assert finalizer._read_finalization_receipt.await_count == 1  # type: ignore[attr-defined]
