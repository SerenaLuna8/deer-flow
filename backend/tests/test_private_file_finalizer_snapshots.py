from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest

from app.private_work.file_finalizer import PrivateFileFinalizer
from deerflow.workspace_changes.types import WorkspaceChangeLimits


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
