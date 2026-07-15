from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkForbidden,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkTooLarge,
    PrivateWorkUnavailable,
)
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef

MIB = 1024 * 1024


@pytest_asyncio.fixture()
async def file_service_seed(migrated_postgres_database_url: str):
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"file-service-{uuid.uuid4()}"
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
    try:
        yield seed, thread_id
    finally:
        await seed.engine.dispose()


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "/etc/passwd",
        "../secret",
        "a/../../b",
        "C:/secret",
        "a\x00b",
        "a\\..\\b",
        "a//b",
        "a/./b",
        "a/",
        "uploads/line\r\nbreak.txt",
        "uploads/control\x1f.txt",
        f"uploads/{'a' * 256}.txt",
        "/".join(["a" * 250] * 5),
    ],
)
def test_private_file_path_rejects_unsafe_or_ambiguous_paths(raw: str) -> None:
    from app.private_work.file_paths import normalize_private_logical_path

    with pytest.raises(PrivateWorkInvalid):
        normalize_private_logical_path(raw, request_id="req-path")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("uploads/report.pdf", "uploads/report.pdf"),
        ("workspace/报告.md", "workspace/报告.md"),
        ("one.txt", "one.txt"),
        ("uploads/%2e%2e/literal.txt", "uploads/%2e%2e/literal.txt"),
    ],
)
def test_private_file_path_preserves_valid_posix_logical_path(raw: str, expected: str) -> None:
    from app.private_work.file_paths import normalize_private_logical_path

    assert normalize_private_logical_path(raw, request_id="req-path") == expected


def test_private_file_path_normalizes_unicode_to_nfc_for_collision_safety() -> None:
    from app.private_work.file_paths import normalize_private_logical_path

    nfc = "uploads/caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt"
    nfd = "uploads/cafe\N{COMBINING ACUTE ACCENT}.txt"
    assert normalize_private_logical_path(nfd, request_id="req-path") == nfc


@pytest.mark.parametrize(
    "media_type",
    [
        "text/plain\N{RIGHT-TO-LEFT OVERRIDE}",
        "text/plain\N{GRINNING FACE}",
        "text /plain",
        "text/plain ",
        "text/plain; charset",
        "text/plain;;charset=utf-8",
        'text/plain; charset="unterminated',
        "text/plain; charset=bad value",
    ],
)
def test_private_media_type_rejects_unicode_and_ambiguous_syntax(media_type: str) -> None:
    from app.private_work.file_service import PrivateFileService

    with pytest.raises(PrivateWorkInvalid):
        PrivateFileService._media_type(media_type, "req-media")


@pytest.mark.parametrize(
    "media_type",
    [
        "text/plain",
        "TEXT/PLAIN; charset=utf-8",
        'application/vnd.example+json; profile="safe value"',
    ],
)
def test_private_media_type_accepts_ascii_type_subtype_and_parameters(media_type: str) -> None:
    from app.private_work.file_service import PrivateFileService

    assert PrivateFileService._media_type(media_type, "req-media") == media_type


async def _chunks(payload: bytes, size: int = MIB) -> AsyncIterator[bytes]:
    for offset in range(0, len(payload), size):
        await asyncio.sleep(0)
        yield payload[offset : offset + size]


def test_private_defaults_allow_one_hundred_mib_while_legacy_upload_stays_fifty_mib() -> None:
    from app.gateway.routers.uploads import DEFAULT_MAX_FILE_SIZE as LEGACY_MAX_FILE_SIZE
    from app.private_work.file_service import PrivateFileLimits

    assert PrivateFileLimits().max_file_size == 100 * MIB
    assert LEGACY_MAX_FILE_SIZE == 50 * MIB


def test_secure_conversion_open_closes_file_descriptor_when_fstat_fails(
    tmp_path,
    monkeypatch,
) -> None:
    from app.private_work.file_service import PrivateFileService

    temp_dir = tmp_path / "conversion"
    temp_dir.mkdir()
    output = temp_dir / "output.md"
    output.write_text("content", encoding="utf-8")
    original_close = os.close
    closed: list[int] = []

    def fail_fstat(_file_descriptor: int):
        raise OSError("fstat failed")

    def record_close(file_descriptor: int) -> None:
        closed.append(file_descriptor)
        original_close(file_descriptor)

    monkeypatch.setattr(os, "fstat", fail_fstat)
    monkeypatch.setattr(os, "close", record_close)
    with pytest.raises(OSError, match="fstat failed"):
        PrivateFileService._open_controlled_conversion_output(temp_dir, output)
    assert len(closed) == 2


def test_conversion_dir_chmod_failure_removes_created_directory(tmp_path, monkeypatch) -> None:
    from app.private_work.file_service import PrivateFileService

    service = PrivateFileService(None, conversion_temp_root=tmp_path)  # type: ignore[arg-type]

    def fail_chmod(_path: Path, _mode: int) -> None:
        raise OSError("chmod failed")

    monkeypatch.setattr(Path, "chmod", fail_chmod)
    with pytest.raises(OSError, match="chmod failed"):
        service._create_conversion_dir()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_upload_streams_one_mib_chunks_and_commits_ready_after_whole_hash(file_service_seed) -> None:
    from app.private_work.file_service import PRIVATE_FILE_CHUNK_SIZE, PrivateFileService

    seed, thread_id = file_service_seed
    payload = b"a" * MIB + b"b" * MIB + b"c" * (MIB // 2)
    service = PrivateFileService(seed.factory)

    result = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/report.pdf",
        media_type="application/pdf",
        chunks=_chunks(payload, size=333_333),
    )

    assert PRIVATE_FILE_CHUNK_SIZE == MIB
    assert result.status == "ready"
    assert result.size == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    async with seed.engine.connect() as connection:
        rows = (
            await connection.execute(
                text("SELECT chunk_index,size FROM file_chunks WHERE file_id=:file_id ORDER BY chunk_index"),
                {"file_id": result.id},
            )
        ).all()
    assert rows == [(0, MIB), (1, MIB), (2, MIB // 2)]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_upload_empty_file_is_ready_with_empty_hash_and_zero_chunks(file_service_seed) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    result = await PrivateFileService(seed.factory).upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/empty.txt",
        media_type="text/plain",
        chunks=_chunks(b""),
    )
    assert result.status == "ready"
    assert result.size == 0
    assert result.sha256 == hashlib.sha256(b"").hexdigest()
    async with seed.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM file_chunks WHERE file_id=:file_id"), {"file_id": result.id}) == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_upload_many_enforces_count_single_and_total_limits_without_partial_rows(file_service_seed) -> None:
    from app.private_work.file_service import (
        PrivateFileLimits,
        PrivateFileService,
        PrivateUpload,
    )

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory)

    with pytest.raises(PrivateWorkTooLarge):
        await service.upload_many(
            seed.owner_a,
            thread_id=thread_id,
            uploads=(
                PrivateUpload("uploads/a.txt", "text/plain", _chunks(b"123")),
                PrivateUpload("uploads/b.txt", "text/plain", _chunks(b"456")),
            ),
            limits=PrivateFileLimits(max_files=1, max_file_size=10, max_total_size=10),
        )
    with pytest.raises(PrivateWorkTooLarge):
        await service.upload_many(
            seed.owner_a,
            thread_id=thread_id,
            uploads=(PrivateUpload("uploads/big.txt", "text/plain", _chunks(b"123456")),),
            limits=PrivateFileLimits(max_files=2, max_file_size=5, max_total_size=10),
        )
    with pytest.raises(PrivateWorkTooLarge):
        await service.upload_many(
            seed.owner_a,
            thread_id=thread_id,
            uploads=(
                PrivateUpload("uploads/first.txt", "text/plain", _chunks(b"1234")),
                PrivateUpload("uploads/second.txt", "text/plain", _chunks(b"5678")),
            ),
            limits=PrivateFileLimits(max_files=2, max_file_size=10, max_total_size=6),
        )

    async with seed.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM files WHERE thread_id=:thread_id"), {"thread_id": thread_id}) == 0
        assert await connection.scalar(text("SELECT count(*) FROM file_chunks")) == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_request_cancellation_and_database_failure_cleanup_staging_rows(file_service_seed, monkeypatch) -> None:
    from app.private_work.file_service import PrivateFileService
    from deerflow.persistence.private_work.file_repository import PrivateFileRepository

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory)

    async def cancelled() -> AsyncIterator[bytes]:
        yield b"partial"
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await service.upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="uploads/cancelled.txt",
            media_type="text/plain",
            chunks=cancelled(),
        )

    original = PrivateFileRepository.append_chunk

    async def fail_append(self, **kwargs):
        del self, kwargs
        raise OperationalError("INSERT", {}, RuntimeError("database failed"))

    monkeypatch.setattr(PrivateFileRepository, "append_chunk", fail_append)
    with pytest.raises(PrivateWorkUnavailable) as exc_info:
        await service.upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="uploads/db-failure.txt",
            media_type="text/plain",
            chunks=_chunks(b"payload"),
        )
    assert "database failed" not in str(exc_info.value)
    monkeypatch.setattr(PrivateFileRepository, "append_chunk", original)

    async with seed.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM files WHERE thread_id=:thread_id"), {"thread_id": thread_id}) == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_http_body_failure_is_sanitized_and_cleans_exact_staging_rows(file_service_seed) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory)

    async def broken_body() -> AsyncIterator[bytes]:
        yield b"x" * MIB
        raise RuntimeError("sensitive HTTP body diagnostic")

    with pytest.raises(PrivateWorkUnavailable) as exc_info:
        await service.upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="uploads/broken-body.txt",
            media_type="text/plain",
            chunks=broken_body(),
        )

    assert exc_info.value.code == "PRIVATE_WORK_UNAVAILABLE"
    assert str(exc_info.value) == "Private work is unavailable."
    assert "sensitive HTTP body diagnostic" not in str(exc_info.value)
    async with seed.engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM files WHERE thread_id=:thread_id"),
                {"thread_id": thread_id},
            )
            == 0
        )
        assert await connection.scalar(text("SELECT count(*) FROM file_chunks")) == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cancel_after_stage_commit_uses_known_ids_to_cleanup(file_service_seed, monkeypatch) -> None:
    from app.private_work.file_service import PrivateFileService
    from deerflow.persistence.private_work.file_repository import PrivateFileRepository

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory)

    async def commit_then_cancel(context, target_thread_id, uploads, file_ids=None):
        async with seed.factory() as session, session.begin():
            upload = uploads[0]
            kwargs = {}
            if file_ids is not None:
                kwargs["file_id"] = file_ids[0]
            await PrivateFileRepository(session).stage(
                scope=context.resource_scope,
                thread_id=target_thread_id,
                kind=upload.kind,
                logical_path=upload.logical_path,
                media_type=upload.media_type,
                **kwargs,
            )
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "_stage_many", commit_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        await service.upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="uploads/commit-cancel.txt",
            media_type="text/plain",
            chunks=_chunks(b"never-read"),
        )
    async with seed.engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM files WHERE thread_id=:thread_id"),
                {"thread_id": thread_id},
            )
            == 0
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cancellation_is_deferred_after_atomic_finalize_commit_point(
    file_service_seed,
    monkeypatch,
) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory)
    original_finalize = service._finalize_many
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_finalize(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_finalize(*args, **kwargs)

    monkeypatch.setattr(service, "_finalize_many", blocked_finalize)
    task = asyncio.create_task(
        service.upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="uploads/finalize-commit.txt",
            media_type="text/plain",
            chunks=_chunks(b"complete"),
        )
    )
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    result = await task
    assert result.status == "ready"
    assert (
        await service.get_ready(
            seed.owner_a,
            thread_id=thread_id,
            file_id=result.id,
        )
        == result
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conversion_uses_controlled_temp_and_creates_linked_workspace_file(file_service_seed, tmp_path) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    source = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/report.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        chunks=_chunks(b"document-content"),
    )
    observed_source: list[Path] = []

    def convert(path: Path) -> Path:
        observed_source.append(path)
        assert path.is_file()
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.read_bytes() == b"document-content"
        output = path.with_suffix(".md")
        output.write_text("# converted", encoding="utf-8")
        return output

    converted = await service.convert_upload(
        seed.owner_a,
        thread_id=thread_id,
        source_file_id=source.id,
        logical_path="workspace/report.md",
        media_type="text/markdown",
        converter=convert,
    )

    assert converted.kind == "workspace"
    assert converted.source_file_id == source.id
    assert converted.sha256 == hashlib.sha256(b"# converted").hexdigest()
    assert observed_source
    assert list(tmp_path.iterdir()) == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conversion_failure_keeps_ready_source_and_cleans_temp(file_service_seed, tmp_path) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    source = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/fail.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        chunks=_chunks(b"source-remains"),
    )

    def fail(_path: Path) -> Path:
        raise RuntimeError("converter diagnostics with path")

    with pytest.raises(PrivateWorkUnavailable) as exc_info:
        await service.convert_upload(
            seed.owner_a,
            thread_id=thread_id,
            source_file_id=source.id,
            logical_path="workspace/fail.md",
            media_type="text/markdown",
            converter=fail,
        )
    assert "converter diagnostics" not in str(exc_info.value)
    assert list(tmp_path.iterdir()) == []
    resolved = await service.get_ready(seed.owner_a, thread_id=thread_id, file_id=source.id)
    assert resolved is not None
    assert resolved.status == "ready"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conversion_source_lookup_database_failure_is_stable_and_side_effect_free(
    file_service_seed,
    tmp_path,
    monkeypatch,
) -> None:
    from app.private_work.file_service import PrivateFileService
    from deerflow.persistence.private_work.file_repository import PrivateFileRepository

    seed, thread_id = file_service_seed
    converter_called = False

    async def fail_lookup(self, **kwargs):
        del self, kwargs
        raise OperationalError("SELECT", {}, RuntimeError("sensitive database diagnostic"))

    def converter(path: Path) -> Path:
        nonlocal converter_called
        converter_called = True
        return path

    monkeypatch.setattr(PrivateFileRepository, "get_ready", fail_lookup)
    with pytest.raises(PrivateWorkUnavailable) as exc_info:
        await PrivateFileService(seed.factory, conversion_temp_root=tmp_path).convert_upload(
            seed.owner_a,
            thread_id=thread_id,
            source_file_id=uuid.uuid4(),
            logical_path="workspace/unavailable.md",
            media_type="text/markdown",
            converter=converter,
        )
    assert "sensitive database diagnostic" not in str(exc_info.value)
    assert not converter_called
    assert list(tmp_path.iterdir()) == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_service_denies_cross_scope_and_conflicting_active_logical_path(file_service_seed) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory)
    created = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/unique.txt",
        media_type="text/plain",
        chunks=_chunks(b"first"),
    )
    assert await service.get_ready(seed.owner_b, thread_id=thread_id, file_id=created.id) is None
    assert await service.get_ready(seed.project_b_owner_a, thread_id=thread_id, file_id=created.id) is None
    with pytest.raises(PrivateWorkConflict):
        await service.upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="uploads/unique.txt",
            media_type="text/plain",
            chunks=_chunks(b"second"),
        )

    await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt",
        media_type="text/plain",
        chunks=_chunks(b"nfc"),
    )
    with pytest.raises(PrivateWorkConflict):
        await service.upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="uploads/cafe\N{COMBINING ACUTE ACCENT}.txt",
            media_type="text/plain",
            chunks=_chunks(b"nfd"),
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_slow_upload_does_not_hold_governance_lock_and_revocation_cleans_staging(file_service_seed) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    first_chunk_seen = asyncio.Event()
    release_source = asyncio.Event()

    async def blocked_source() -> AsyncIterator[bytes]:
        yield b"first"
        first_chunk_seen.set()
        await release_source.wait()
        yield b"second"

    task = asyncio.create_task(
        PrivateFileService(seed.factory).upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="uploads/revoked.txt",
            media_type="text/plain",
            chunks=blocked_source(),
        )
    )
    await first_chunk_seen.wait()
    try:
        async with asyncio.timeout(1):
            async with seed.engine.begin() as connection:
                await connection.execute(
                    text(
                        """UPDATE project_memberships
                        SET status='left', version=version+1
                        WHERE id=:membership_id"""
                    ),
                    {"membership_id": seed.owner_a.membership_id},
                )
        release_source.set()
        with pytest.raises(PrivateWorkNotFound) as exc_info:
            await task
        assert exc_info.value.request_id == seed.owner_a.request_id
    finally:
        release_source.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async with seed.engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM files WHERE thread_id=:thread_id"), {"thread_id": thread_id}) == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_slow_upload_rechecks_create_capability_after_viewer_downgrade(file_service_seed) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    first_input_seen = asyncio.Event()
    release_source = asyncio.Event()

    async def blocked_source() -> AsyncIterator[bytes]:
        yield b"first"
        first_input_seen.set()
        await release_source.wait()
        yield b"second"

    task = asyncio.create_task(
        PrivateFileService(seed.factory).upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="uploads/viewer-downgrade.txt",
            media_type="text/plain",
            chunks=blocked_source(),
        )
    )
    await first_input_seen.wait()
    try:
        async with seed.engine.begin() as connection:
            await connection.execute(
                text("UPDATE project_memberships SET role='viewer' WHERE id=:membership_id"),
                {"membership_id": seed.owner_a.membership_id},
            )
        release_source.set()
        with pytest.raises(PrivateWorkForbidden):
            await task
    finally:
        release_source.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async with seed.engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM files WHERE thread_id=:thread_id"),
                {"thread_id": thread_id},
            )
            == 0
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_converter_cancellation_waits_for_worker_before_temp_cleanup(file_service_seed, tmp_path) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    source = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/cancel-convert.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        chunks=_chunks(b"source"),
    )
    started = threading.Event()
    release = threading.Event()
    parent_exists_after_release: list[bool] = []

    def converter(path: Path) -> Path:
        started.set()
        release.wait(timeout=5)
        parent_exists_after_release.append(path.parent.exists())
        output = path.with_suffix(".md")
        output.write_text("converted", encoding="utf-8")
        return output

    task = asyncio.create_task(
        service.convert_upload(
            seed.owner_a,
            thread_id=thread_id,
            source_file_id=source.id,
            logical_path="workspace/cancel-convert.md",
            media_type="text/markdown",
            converter=converter,
        )
    )
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    assert any(tmp_path.iterdir())
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert parent_exists_after_release == [True]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conversion_rejects_symlink_output_and_cleans_temp(file_service_seed, tmp_path) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    source = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/link.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        chunks=_chunks(b"source"),
    )
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")

    def converter(path: Path) -> Path:
        output = path.with_suffix(".md")
        output.symlink_to(outside)
        return output

    with pytest.raises(PrivateWorkUnavailable):
        await service.convert_upload(
            seed.owner_a,
            thread_id=thread_id,
            source_file_id=source.id,
            logical_path="workspace/link.md",
            media_type="text/markdown",
            converter=converter,
        )
    assert [item.name for item in tmp_path.iterdir()] == ["outside.md"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conversion_rejects_symlink_ancestor_and_cleans_temp(file_service_seed, tmp_path) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    source = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/link-parent.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        chunks=_chunks(b"source"),
    )
    outside = tmp_path / "outside"
    outside.mkdir()

    def converter(path: Path) -> Path:
        linked_parent = path.parent / "linked-parent"
        linked_parent.symlink_to(outside, target_is_directory=True)
        output = linked_parent / "converted.md"
        output.write_text("outside", encoding="utf-8")
        return output

    with pytest.raises(PrivateWorkUnavailable):
        await service.convert_upload(
            seed.owner_a,
            thread_id=thread_id,
            source_file_id=source.id,
            logical_path="workspace/link-parent.md",
            media_type="text/markdown",
            converter=converter,
        )
    assert (outside / "converted.md").read_text(encoding="utf-8") == "outside"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["outside"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conversion_rejects_hardlinked_output(file_service_seed, tmp_path) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    source = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/hardlink.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        chunks=_chunks(b"source"),
    )
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")

    def converter(path: Path) -> Path:
        output = path.with_suffix(".md")
        os.link(outside, output)
        return output

    with pytest.raises(PrivateWorkUnavailable):
        await service.convert_upload(
            seed.owner_a,
            thread_id=thread_id,
            source_file_id=source.id,
            logical_path="workspace/hardlink.md",
            media_type="text/markdown",
            converter=converter,
        )
    assert outside.read_text(encoding="utf-8") == "outside"
    assert [item.name for item in tmp_path.iterdir()] == ["outside.md"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conversion_rejects_fifo_without_blocking(file_service_seed, tmp_path) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    source = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/fifo.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        chunks=_chunks(b"source"),
    )

    def converter(path: Path) -> Path:
        output = path.with_suffix(".fifo")
        os.mkfifo(output)
        return output

    async with asyncio.timeout(2):
        with pytest.raises(PrivateWorkUnavailable):
            await service.convert_upload(
                seed.owner_a,
                thread_id=thread_id,
                source_file_id=source.id,
                logical_path="workspace/fifo.md",
                media_type="text/markdown",
                converter=converter,
            )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_converter_cancelled_error_does_not_spin_and_cleans_temp(file_service_seed, tmp_path) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    source = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/converter-cancel.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        chunks=_chunks(b"source"),
    )

    def converter(_path: Path) -> Path:
        raise asyncio.CancelledError

    async with asyncio.timeout(2):
        with pytest.raises(asyncio.CancelledError):
            await service.convert_upload(
                seed.owner_a,
                thread_id=thread_id,
                source_file_id=source.id,
                logical_path="workspace/converter-cancel.md",
                media_type="text/markdown",
                converter=converter,
            )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conversion_streams_from_verified_fd_when_path_is_swapped_after_open(
    file_service_seed,
    tmp_path,
    monkeypatch,
) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    source = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/swap.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        chunks=_chunks(b"source"),
    )
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside-secret")

    def converter(path: Path) -> Path:
        output = path.with_suffix(".md")
        output.write_bytes(b"verified-inside")
        return output

    original_open = service._open_controlled_conversion_output

    def open_then_swap(temp_dir: Path, output: Path) -> int:
        output_fd = original_open(temp_dir, output)
        output.unlink()
        output.symlink_to(outside)
        return output_fd

    monkeypatch.setattr(service, "_open_controlled_conversion_output", open_then_swap)
    converted = await service.convert_upload(
        seed.owner_a,
        thread_id=thread_id,
        source_file_id=source.id,
        logical_path="workspace/swap.md",
        media_type="text/markdown",
        converter=converter,
    )
    assert converted.sha256 == hashlib.sha256(b"verified-inside").hexdigest()
    assert converted.size == len(b"verified-inside")
    assert outside.read_bytes() == b"outside-secret"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conversion_cancel_during_temp_creation_waits_and_cleans(
    file_service_seed,
    tmp_path,
    monkeypatch,
) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    source = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/cancel-create.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        chunks=_chunks(b"source"),
    )
    original_create = service._create_conversion_dir
    started = threading.Event()
    release = threading.Event()

    def blocked_create() -> Path:
        started.set()
        release.wait(timeout=5)
        return original_create()

    monkeypatch.setattr(service, "_create_conversion_dir", blocked_create)
    task = asyncio.create_task(
        service.convert_upload(
            seed.owner_a,
            thread_id=thread_id,
            source_file_id=source.id,
            logical_path="workspace/cancel-create.md",
            media_type="text/markdown",
            converter=lambda path: path,
        )
    )
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    try:
        await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conversion_cancel_during_source_open_closes_handle_and_cleans(
    file_service_seed,
    tmp_path,
    monkeypatch,
) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    source = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/cancel-open.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        chunks=_chunks(b"source"),
    )
    original_open = Path.open
    started = threading.Event()
    release = threading.Event()
    opened = []

    def blocked_open(path: Path, *args, **kwargs):
        if args and args[0] == "wb" and path.name.startswith("source-"):
            started.set()
            release.wait(timeout=5)
            handle = original_open(path, *args, **kwargs)
            opened.append(handle)
            return handle
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", blocked_open)
    task = asyncio.create_task(
        service.convert_upload(
            seed.owner_a,
            thread_id=thread_id,
            source_file_id=source.id,
            logical_path="workspace/cancel-open.md",
            media_type="text/markdown",
            converter=lambda path: path,
        )
    )
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    try:
        await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert opened and all(handle.closed for handle in opened)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conversion_cancel_during_source_write_joins_close_and_cleanup(
    file_service_seed,
    tmp_path,
    monkeypatch,
) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    source = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/cancel-write.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        chunks=_chunks(b"source"),
    )
    original_open = Path.open
    started = threading.Event()
    release = threading.Event()
    wrappers = []

    class BlockingWriter:
        def __init__(self, handle):
            self.handle = handle

        def write(self, content):
            started.set()
            release.wait(timeout=5)
            return self.handle.write(content)

        def flush(self):
            return self.handle.flush()

        def fileno(self):
            return self.handle.fileno()

        def close(self):
            return self.handle.close()

        @property
        def closed(self):
            return self.handle.closed

    def wrapped_open(path: Path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if args and args[0] == "wb" and path.name.startswith("source-"):
            wrapper = BlockingWriter(handle)
            wrappers.append(wrapper)
            return wrapper
        return handle

    monkeypatch.setattr(Path, "open", wrapped_open)
    task = asyncio.create_task(
        service.convert_upload(
            seed.owner_a,
            thread_id=thread_id,
            source_file_id=source.id,
            logical_path="workspace/cancel-write.md",
            media_type="text/markdown",
            converter=lambda path: path,
        )
    )
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    try:
        await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wrappers and all(wrapper.closed for wrapper in wrappers)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_repeated_cancel_during_output_close_still_removes_temp_dir(
    file_service_seed,
    tmp_path,
    monkeypatch,
) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    source = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/repeated-cancel.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        chunks=_chunks(b"source"),
    )
    original_run = service._run_sync_to_completion
    original_close = os.close
    close_started = threading.Event()
    close_release = threading.Event()

    def blocking_close(file_descriptor: int) -> None:
        close_started.set()
        close_release.wait(timeout=5)
        original_close(file_descriptor)

    async def controlled_run(operation, *args, cleanup_on_cancel=None):
        if operation is os.close:
            return await original_run(
                blocking_close,
                *args,
                cleanup_on_cancel=cleanup_on_cancel,
            )
        return await original_run(
            operation,
            *args,
            cleanup_on_cancel=cleanup_on_cancel,
        )

    monkeypatch.setattr(service, "_run_sync_to_completion", controlled_run)

    def converter(path: Path) -> Path:
        output = path.with_suffix(".md")
        output.write_text("converted", encoding="utf-8")
        return output

    task = asyncio.create_task(
        service.convert_upload(
            seed.owner_a,
            thread_id=thread_id,
            source_file_id=source.id,
            logical_path="workspace/repeated-cancel.md",
            media_type="text/markdown",
            converter=converter,
        )
    )
    await asyncio.to_thread(close_started.wait, 3)
    task.cancel()
    task.cancel()
    try:
        await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_service_lists_and_soft_deletes_ready_file_without_removing_chunks(file_service_seed) -> None:
    from app.private_work.file_service import PrivateFileService

    seed, thread_id = file_service_seed
    service = PrivateFileService(seed.factory)
    assert await service.list_ready(seed.owner_a, thread_id=thread_id) == ()
    ready = await service.upload(
        seed.owner_a,
        thread_id=thread_id,
        logical_path="uploads/delete-me.txt",
        media_type="text/plain",
        chunks=_chunks(b"keep-the-chunks"),
    )

    assert [item.id for item in await service.list_ready(seed.owner_a, thread_id=thread_id)] == [ready.id]
    with pytest.raises(PrivateWorkNotFound):
        await service.list_ready(seed.owner_b, thread_id=thread_id)
    with pytest.raises(PrivateWorkNotFound):
        await service.list_ready(seed.project_b_owner_a, thread_id=thread_id)
    with pytest.raises(PrivateWorkNotFound):
        await service.delete_ready(seed.owner_b, thread_id=thread_id, file_id=ready.id)
    with pytest.raises(PrivateWorkNotFound):
        await service.delete_ready(seed.project_b_owner_a, thread_id=thread_id, file_id=ready.id)

    deleted = await service.delete_ready(seed.owner_a, thread_id=thread_id, file_id=ready.id)
    assert deleted.status == "deleted"
    assert await service.get_ready(seed.owner_a, thread_id=thread_id, file_id=ready.id) is None
    assert await service.list_ready(seed.owner_a, thread_id=thread_id) == ()
    with pytest.raises(PrivateWorkNotFound):
        await service.delete_ready(seed.owner_a, thread_id=thread_id, file_id=ready.id)
    async with seed.engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM file_chunks WHERE file_id=:file_id"),
                {"file_id": ready.id},
            )
            == 1
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_branch_authority_copies_chunk_payload_server_side_without_returning_content(
    file_service_seed,
) -> None:
    from sqlalchemy import event

    from app.private_work.file_service import PrivateFileService

    seed, source_thread_id = file_service_seed
    target_thread_id = f"branch-copy-{uuid.uuid4()}"
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=target_thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
    source = await PrivateFileService(seed.factory).upload(
        seed.owner_a,
        thread_id=source_thread_id,
        logical_path="workspace/large.bin",
        media_type="application/octet-stream",
        chunks=_chunks(b"a" * MIB + b"tail"),
    )
    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(seed.engine.sync_engine, "before_cursor_execute", capture_statement)
    try:
        await PrivateFileService(seed.factory).copy_thread_files(
            seed.owner_a_scope,
            source_thread_id,
            target_thread_id,
        )
    finally:
        event.remove(seed.engine.sync_engine, "before_cursor_execute", capture_statement)

    normalized = [" ".join(statement.lower().split()) for statement in statements]
    assert not any(statement.startswith("select") and "file_chunks.content" in statement for statement in normalized)
    assert any(statement.startswith("insert into file_chunks") and "select" in statement and "file_chunks.content" in statement for statement in normalized)
    async with seed.engine.connect() as connection:
        copied = (
            await connection.execute(
                text("SELECT id,size,sha256,status,created_by_run_id FROM files WHERE project_id=:project_id AND owner_user_id=:owner AND thread_id=:thread_id AND logical_path='workspace/large.bin'"),
                {
                    "project_id": seed.owner_a.project_id,
                    "owner": seed.owner_a_scope.owner_user_id,
                    "thread_id": target_thread_id,
                },
            )
        ).one()
        chunk_summary = (
            await connection.execute(
                text("SELECT count(*),sum(size),min(chunk_index),max(chunk_index) FROM file_chunks WHERE file_id=:file_id"),
                {"file_id": copied.id},
            )
        ).one()
        artifact_count = await connection.scalar(
            text("SELECT count(*) FROM artifacts WHERE thread_id=:thread_id"),
            {"thread_id": target_thread_id},
        )
        run_count = await connection.scalar(
            text("SELECT count(*) FROM runs WHERE thread_id=:thread_id"),
            {"thread_id": target_thread_id},
        )
    assert copied[1:] == (source.size, source.sha256, "ready", None)
    assert chunk_summary == (2, source.size, 0, 1)
    assert artifact_count == 0
    assert run_count == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_service_viewer_can_list_and_delete_own_ready_files_but_cannot_create(
    file_service_seed,
    tmp_path,
) -> None:
    from app.private_work.file_service import PrivateFileService
    from deerflow.persistence.private_work.file_repository import PrivateFileRepository

    seed, _thread_id = file_service_seed
    viewer_thread_id = f"viewer-file-{uuid.uuid4()}"
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.viewer.resource_scope,
            thread_id=viewer_thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        repository = PrivateFileRepository(session)
        staged = await repository.stage(
            scope=seed.viewer.resource_scope,
            thread_id=viewer_thread_id,
            kind="upload",
            logical_path="uploads/viewer.txt",
            media_type="text/plain",
        )
        ready = await repository.finalize(
            scope=seed.viewer.resource_scope,
            thread_id=viewer_thread_id,
            file_id=staged.id,
            expected_size=0,
            expected_sha256=hashlib.sha256(b"").hexdigest(),
        )

    service = PrivateFileService(seed.factory, conversion_temp_root=tmp_path)
    assert [item.id for item in await service.list_ready(seed.viewer, thread_id=viewer_thread_id)] == [ready.id]
    converter_called = False

    def converter(path: Path) -> Path:
        nonlocal converter_called
        converter_called = True
        return path

    with pytest.raises(PrivateWorkForbidden):
        await service.convert_upload(
            seed.viewer,
            thread_id=viewer_thread_id,
            source_file_id=ready.id,
            logical_path="workspace/viewer.md",
            media_type="text/markdown",
            converter=converter,
        )
    assert not converter_called
    assert list(tmp_path.iterdir()) == []

    deleted = await service.delete_ready(
        seed.viewer,
        thread_id=viewer_thread_id,
        file_id=ready.id,
    )
    assert deleted.status == "deleted"
    with pytest.raises(PrivateWorkForbidden):
        await service.upload(
            seed.viewer,
            thread_id=viewer_thread_id,
            logical_path="uploads/new.txt",
            media_type="text/plain",
            chunks=_chunks(b"blocked"),
        )
