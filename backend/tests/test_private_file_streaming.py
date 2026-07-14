from __future__ import annotations

import hashlib
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.private_work.errors import PrivateWorkNotFound, PrivateWorkUnavailable
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef


@pytest_asyncio.fixture()
async def streaming_seed(migrated_postgres_database_url: str):
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"file-stream-{uuid.uuid4()}"
    run_id = f"run-{uuid.uuid4()}"
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        await PrivateRunRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(run_id=run_id),
        )
    try:
        yield seed, thread_id, run_id
    finally:
        await seed.engine.dispose()


async def _ready_file(
    seed: M4ThreadSeed,
    thread_id: str,
    chunks: tuple[bytes, ...],
    *,
    kind: str = "output",
    media_type: str = "application/octet-stream",
):
    from deerflow.persistence.private_work.file_repository import PrivateFileRepository

    whole = hashlib.sha256()
    async with seed.factory() as session, session.begin():
        repository = PrivateFileRepository(session)
        staged = await repository.stage(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            kind=kind,
            logical_path="outputs/result.bin" if kind == "output" else "uploads/result.bin",
            media_type=media_type,
        )
        total = 0
        for index, chunk in enumerate(chunks):
            whole.update(chunk)
            total += len(chunk)
            await repository.append_chunk(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                file_id=staged.id,
                chunk_index=index,
                content=chunk,
                size=len(chunk),
                sha256=hashlib.sha256(chunk).hexdigest(),
            )
        return await repository.finalize(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            file_id=staged.id,
            expected_size=total,
            expected_sha256=whole.hexdigest(),
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stream_file_scopes_lookup_before_stream_and_applies_backpressure(streaming_seed, monkeypatch) -> None:
    from app.private_work.file_streaming import PrivateFileStreamer

    seed, thread_id, _run_id = streaming_seed
    ready = await _ready_file(seed, thread_id, (b"first", b"second", b"third"))
    streamer = PrivateFileStreamer(seed.factory, chunk_page_size=1)
    original_fetch = streamer._fetch_page
    fetches = 0

    async def counted_fetch(*args, **kwargs):
        nonlocal fetches
        fetches += 1
        return await original_fetch(*args, **kwargs)

    monkeypatch.setattr(streamer, "_fetch_page", counted_fetch)

    stream = await streamer.stream_file(seed.owner_a, thread_id=thread_id, file_id=ready.id)
    assert fetches == 1
    assert await anext(stream.body) == b"first"
    assert fetches == 1
    assert await anext(stream.body) == b"second"
    assert fetches == 2
    assert await anext(stream.body) == b"third"
    with pytest.raises(StopAsyncIteration):
        await anext(stream.body)

    with pytest.raises(PrivateWorkNotFound):
        await streamer.stream_file(seed.owner_b, thread_id=thread_id, file_id=ready.id)
    with pytest.raises(PrivateWorkNotFound):
        await streamer.stream_file(seed.project_b_owner_a, thread_id=thread_id, file_id=ready.id)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunk_index", "replacement"),
    [(0, b"wrong"), (1, b"broken")],
)
async def test_stream_file_first_page_tamper_fails_before_response_without_path(
    streaming_seed,
    chunk_index: int,
    replacement: bytes,
) -> None:
    from app.private_work.file_streaming import PrivateFileStreamer

    seed, thread_id, _run_id = streaming_seed
    ready = await _ready_file(seed, thread_id, (b"first", b"second"))
    async with seed.engine.begin() as connection:
        await connection.execute(
            text("UPDATE file_chunks SET content=:content WHERE file_id=:file_id AND chunk_index=:chunk_index"),
            {
                "content": replacement,
                "file_id": ready.id,
                "chunk_index": chunk_index,
            },
        )

    with pytest.raises(PrivateWorkUnavailable) as exc_info:
        await PrivateFileStreamer(seed.factory, chunk_page_size=2).stream_file(
            seed.owner_a,
            thread_id=thread_id,
            file_id=ready.id,
        )
    assert ready.logical_path not in str(exc_info.value)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_media_type",
    ["text/plain\N{RIGHT-TO-LEFT OVERRIDE}", "text/plain\N{GRINNING FACE}"],
)
async def test_stream_file_rejects_legacy_non_ascii_media_type_before_response(
    streaming_seed,
    bad_media_type: str,
) -> None:
    from app.private_work.file_streaming import PrivateFileStreamer

    seed, thread_id, _run_id = streaming_seed
    ready = await _ready_file(seed, thread_id, (b"content",), media_type="text/plain")
    async with seed.engine.begin() as connection:
        await connection.execute(
            text("UPDATE files SET media_type=:media_type WHERE id=:file_id"),
            {"media_type": bad_media_type, "file_id": ready.id},
        )
    with pytest.raises(PrivateWorkUnavailable):
        await PrivateFileStreamer(seed.factory).stream_file(
            seed.owner_a,
            thread_id=thread_id,
            file_id=ready.id,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stream_file_midstream_chunk_and_whole_hash_tamper_abort_safely(streaming_seed) -> None:
    from app.private_work.file_streaming import PrivateFileStreamer

    seed, thread_id, _run_id = streaming_seed
    ready = await _ready_file(seed, thread_id, (b"first", b"second"))
    stream = await PrivateFileStreamer(seed.factory, chunk_page_size=1).stream_file(
        seed.owner_a,
        thread_id=thread_id,
        file_id=ready.id,
    )
    assert await anext(stream.body) == b"first"
    async with seed.engine.begin() as connection:
        await connection.execute(
            text("UPDATE file_chunks SET content=:content WHERE file_id=:file_id AND chunk_index=1"),
            {"content": b"broken", "file_id": ready.id},
        )
    with pytest.raises(PrivateWorkUnavailable) as chunk_exc:
        await anext(stream.body)
    assert ready.logical_path not in str(chunk_exc.value)

    whole_ready = await _ready_file(seed, thread_id, (b"whole",), kind="upload")
    async with seed.engine.begin() as connection:
        await connection.execute(
            text("UPDATE files SET sha256=:sha256 WHERE id=:file_id"),
            {"sha256": "f" * 64, "file_id": whole_ready.id},
        )
    whole_stream = await PrivateFileStreamer(seed.factory).stream_file(
        seed.owner_a,
        thread_id=thread_id,
        file_id=whole_ready.id,
    )
    assert await anext(whole_stream.body) == b"whole"
    with pytest.raises(PrivateWorkUnavailable) as whole_exc:
        await anext(whole_stream.body)
    assert whole_ready.logical_path not in str(whole_exc.value)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle_column", ["frozen_at", "deleted_at"])
async def test_stream_file_hides_ready_bytes_when_thread_is_inactive(
    streaming_seed,
    lifecycle_column: str,
) -> None:
    from app.private_work.file_streaming import PrivateFileStreamer

    seed, thread_id, _run_id = streaming_seed
    ready = await _ready_file(seed, thread_id, (b"private",))
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(f"UPDATE threads_meta SET {lifecycle_column}=now() WHERE thread_id=:thread_id"),
            {"thread_id": thread_id},
        )
    with pytest.raises(PrivateWorkNotFound):
        await PrivateFileStreamer(seed.factory).stream_file(
            seed.owner_a,
            thread_id=thread_id,
            file_id=ready.id,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stream_file_stops_on_thread_freeze_between_chunk_pages(streaming_seed) -> None:
    from app.private_work.file_streaming import PrivateFileStreamer

    seed, thread_id, _run_id = streaming_seed
    ready = await _ready_file(seed, thread_id, (b"first", b"second"))
    stream = await PrivateFileStreamer(seed.factory, chunk_page_size=1).stream_file(
        seed.owner_a,
        thread_id=thread_id,
        file_id=ready.id,
    )
    assert await anext(stream.body) == b"first"
    async with seed.engine.begin() as connection:
        await connection.execute(
            text("UPDATE threads_meta SET frozen_at=now() WHERE thread_id=:thread_id"),
            {"thread_id": thread_id},
        )
    with pytest.raises(PrivateWorkUnavailable):
        await anext(stream.body)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stream_artifact_scopes_file_and_returns_async_chunk_iterator(streaming_seed) -> None:
    from app.private_work.file_streaming import PrivateFileStreamer

    seed, thread_id, run_id = streaming_seed
    ready = await _ready_file(seed, thread_id, (b"artifact",))
    artifact_id = uuid.uuid4()
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO artifacts
                (id,project_id,owner_user_id,thread_id,run_id,file_id,display_name,media_type,artifact_metadata)
                VALUES (:id,:project_id,:owner_user_id,:thread_id,:run_id,:file_id,:display_name,:media_type,'{}'::jsonb)"""
            ),
            {
                "id": artifact_id,
                "project_id": seed.owner_a.project_id,
                "owner_user_id": str(seed.owner_a.user_id),
                "thread_id": thread_id,
                "run_id": run_id,
                "file_id": ready.id,
                "display_name": "result.bin",
                "media_type": "application/octet-stream",
            },
        )

    stream = await PrivateFileStreamer(seed.factory).stream_artifact(
        seed.owner_a,
        thread_id=thread_id,
        artifact_id=artifact_id,
    )
    assert await anext(stream.body) == b"artifact"
    with pytest.raises(StopAsyncIteration):
        await anext(stream.body)
    with pytest.raises(PrivateWorkNotFound):
        await PrivateFileStreamer(seed.factory).stream_artifact(
            seed.owner_b,
            thread_id=thread_id,
            artifact_id=artifact_id,
        )
    async with seed.engine.begin() as connection:
        await connection.execute(
            text("UPDATE threads_meta SET deleted_at=now() WHERE thread_id=:thread_id"),
            {"thread_id": thread_id},
        )
    with pytest.raises(PrivateWorkNotFound):
        await PrivateFileStreamer(seed.factory).stream_artifact(
            seed.owner_a,
            thread_id=thread_id,
            artifact_id=artifact_id,
        )


@pytest.mark.parametrize(
    "media_type",
    [
        "text/html",
        "TEXT/HTML; charset=utf-8",
        "application/javascript",
        "text/javascript; charset=utf-8",
        "application/ecmascript",
        "text/ecmascript; charset=UTF-8",
        "application/xhtml+xml",
        "application/xml",
        "text/xml; charset=utf-8",
        "image/svg+xml; charset=utf-8",
        "application/pdf; version=1.7",
    ],
)
def test_safe_download_headers_force_attachment_for_active_content(media_type: str) -> None:
    from app.private_work.file_streaming import safe_download_headers

    headers = safe_download_headers("page.html", media_type=media_type)
    assert headers["Content-Disposition"].startswith("attachment;")


def test_safe_download_headers_encode_crlf_quotes_and_path_filename_injection() -> None:
    from app.private_work.file_streaming import safe_download_headers

    value = safe_download_headers(
        '../bad\r\nX-Evil: yes/"report".txt',
        media_type="text/plain",
        download=True,
    )["Content-Disposition"]
    assert value.startswith("attachment;")
    assert "\r" not in value and "\n" not in value
    assert "X-Evil:" not in value
    assert "../" not in value
    assert "%22report%22.txt" in value

    bidi_value = safe_download_headers(
        "safe\N{RIGHT-TO-LEFT OVERRIDE}cod.exe.txt",
        media_type="text/plain",
        download=True,
    )["Content-Disposition"]
    assert "%E2%80%AE" not in bidi_value


def test_private_streaming_response_keeps_async_body_and_safe_headers() -> None:
    from starlette.responses import StreamingResponse

    from app.private_work.file_streaming import (
        PrivateFileStream,
        private_streaming_response,
    )

    async def body():
        yield b"chunk"

    stream = PrivateFileStream(
        file=None,  # type: ignore[arg-type]
        body=body(),
        display_name="safe.txt",
        media_type="text/plain",
        headers={
            "Content-Disposition": "inline; filename*=UTF-8''safe.txt",
            "X-Content-Type-Options": "nosniff",
        },
    )
    response = private_streaming_response(stream)
    assert isinstance(response, StreamingResponse)
    assert response.body_iterator is stream.body
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_artifact_cannot_downgrade_active_file_media_type_to_inline(streaming_seed) -> None:
    from app.private_work.file_streaming import PrivateFileStreamer

    seed, thread_id, run_id = streaming_seed
    ready = await _ready_file(
        seed,
        thread_id,
        (b"<html>active</html>",),
        media_type="text/html; charset=utf-8",
    )
    artifact_id = uuid.uuid4()
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO artifacts
                (id,project_id,owner_user_id,thread_id,run_id,file_id,display_name,media_type,artifact_metadata)
                VALUES (:id,:project_id,:owner_user_id,:thread_id,:run_id,:file_id,'safe.txt','text/plain','{}'::jsonb)"""
            ),
            {
                "id": artifact_id,
                "project_id": seed.owner_a.project_id,
                "owner_user_id": str(seed.owner_a.user_id),
                "thread_id": thread_id,
                "run_id": run_id,
                "file_id": ready.id,
            },
        )
    stream = await PrivateFileStreamer(seed.factory).stream_artifact(
        seed.owner_a,
        thread_id=thread_id,
        artifact_id=artifact_id,
    )
    assert stream.media_type == ready.media_type
    assert stream.headers["Content-Disposition"].startswith("attachment;")
