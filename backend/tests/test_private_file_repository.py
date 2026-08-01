from __future__ import annotations

import hashlib
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef


@pytest_asyncio.fixture()
async def private_files(migrated_postgres_database_url: str):
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_ids = {
        "owner_a": f"file-a-{uuid.uuid4()}",
        "owner_b": f"file-b-{uuid.uuid4()}",
        "project_b": f"file-pb-{uuid.uuid4()}",
    }
    async with seed.factory() as session, session.begin():
        repository = PrivateThreadRepository(session)
        await repository.create(
            scope=seed.owner_a_scope,
            thread_id=thread_ids["owner_a"],
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        await repository.create(
            scope=seed.owner_b_scope,
            thread_id=thread_ids["owner_b"],
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        await repository.create(
            scope=seed.project_b_owner_a_scope,
            thread_id=thread_ids["project_b"],
            agent=ThreadAgentRef(seed.project_b_agent_id, "project"),
        )
    try:
        yield seed, thread_ids
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_repository_stages_appends_and_finalizes_verified_chunks(private_files) -> None:
    from deerflow.persistence.private_work.file_repository import PrivateFileRepository

    seed, threads = private_files
    chunks = (b"alpha", b"beta", b"gamma")
    whole = b"".join(chunks)
    async with seed.factory() as session, session.begin():
        repository = PrivateFileRepository(session)
        staged = await repository.stage(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            kind="upload",
            logical_path="uploads/report.txt",
            media_type="text/plain",
        )
        assert staged.status == "staging"
        assert staged.sha256 == hashlib.sha256(b"").hexdigest()
        for index, chunk in enumerate(chunks):
            await repository.append_chunk(
                scope=seed.owner_a_scope,
                thread_id=threads["owner_a"],
                file_id=staged.id,
                chunk_index=index,
                content=chunk,
                size=len(chunk),
                sha256=hashlib.sha256(chunk).hexdigest(),
            )
        ready = await repository.finalize(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            file_id=staged.id,
            expected_size=len(whole),
            expected_sha256=hashlib.sha256(whole).hexdigest(),
        )

        assert ready.status == "ready"
        assert ready.size == len(whole)
        assert ready.sha256 == hashlib.sha256(whole).hexdigest()
        assert [
            item.size
            for item in await repository.fetch_chunk_page(
                scope=seed.owner_a_scope,
                thread_id=threads["owner_a"],
                file_id=ready.id,
                after_index=-1,
                limit=10,
            )
        ] == [5, 4, 5]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_repository_rejects_chunk_metadata_and_whole_file_tamper(private_files) -> None:
    from deerflow.persistence.private_work.file_repository import (
        PrivateFileIntegrityError,
        PrivateFileRepository,
    )

    seed, threads = private_files
    async with seed.factory() as session, session.begin():
        repository = PrivateFileRepository(session)
        staged = await repository.stage(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            kind="upload",
            logical_path="uploads/tamper.bin",
            media_type="application/octet-stream",
        )
        with pytest.raises(PrivateFileIntegrityError):
            await repository.append_chunk(
                scope=seed.owner_a_scope,
                thread_id=threads["owner_a"],
                file_id=staged.id,
                chunk_index=0,
                content=b"payload",
                size=6,
                sha256=hashlib.sha256(b"payload").hexdigest(),
            )
        await repository.append_chunk(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            file_id=staged.id,
            chunk_index=0,
            content=b"payload",
            size=7,
            sha256=hashlib.sha256(b"payload").hexdigest(),
        )
        await session.execute(
            text("UPDATE file_chunks SET content=:content WHERE file_id=:file_id"),
            {"content": b"changed", "file_id": staged.id},
        )
        with pytest.raises(PrivateFileIntegrityError):
            await repository.finalize(
                scope=seed.owner_a_scope,
                thread_id=threads["owner_a"],
                file_id=staged.id,
                expected_size=7,
                expected_sha256=hashlib.sha256(b"payload").hexdigest(),
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_repository_hides_files_across_project_owner_and_thread(private_files) -> None:
    from deerflow.persistence.private_work.file_repository import PrivateFileRepository

    seed, threads = private_files
    async with seed.factory() as session, session.begin():
        repository = PrivateFileRepository(session)
        staged = await repository.stage(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            kind="upload",
            logical_path="uploads/private.txt",
            media_type="text/plain",
        )
        assert await repository.get(scope=seed.owner_b_scope, thread_id=threads["owner_a"], file_id=staged.id) is None
        assert await repository.get(scope=seed.project_b_owner_a_scope, thread_id=threads["owner_a"], file_id=staged.id) is None
        assert await repository.get(scope=seed.owner_a_scope, thread_id="other-thread", file_id=staged.id) is None
        assert not await repository.abort(scope=seed.owner_b_scope, thread_id=threads["owner_a"], file_id=staged.id)
        assert not await repository.abort(scope=seed.owner_a_scope, thread_id="other-thread", file_id=staged.id)
        assert await repository.get(scope=seed.owner_a_scope, thread_id=threads["owner_a"], file_id=staged.id) is not None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_repository_abort_is_idempotent_and_ready_rows_are_immutable(private_files) -> None:
    from deerflow.persistence.private_work.file_repository import (
        PrivateFileConflict,
        PrivateFileRepository,
    )

    seed, threads = private_files
    async with seed.factory() as session, session.begin():
        repository = PrivateFileRepository(session)
        abandoned = await repository.stage(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            kind="upload",
            logical_path="uploads/abandoned.txt",
            media_type="text/plain",
        )
        assert await repository.abort(scope=seed.owner_a_scope, thread_id=threads["owner_a"], file_id=abandoned.id)
        assert not await repository.abort(scope=seed.owner_a_scope, thread_id=threads["owner_a"], file_id=abandoned.id)

        staged = await repository.stage(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            kind="upload",
            logical_path="uploads/ready.txt",
            media_type="text/plain",
        )
        await repository.append_chunk(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            file_id=staged.id,
            chunk_index=0,
            content=b"ready",
            size=5,
            sha256=hashlib.sha256(b"ready").hexdigest(),
        )
        ready = await repository.finalize(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            file_id=staged.id,
            expected_size=5,
            expected_sha256=hashlib.sha256(b"ready").hexdigest(),
        )
        with pytest.raises(PrivateFileConflict):
            await repository.append_chunk(
                scope=seed.owner_a_scope,
                thread_id=threads["owner_a"],
                file_id=ready.id,
                chunk_index=1,
                content=b"late",
                size=4,
                sha256=hashlib.sha256(b"late").hexdigest(),
            )
        with pytest.raises(PrivateFileConflict):
            await repository.finalize(
                scope=seed.owner_a_scope,
                thread_id=threads["owner_a"],
                file_id=ready.id,
                expected_size=5,
                expected_sha256=ready.sha256,
            )
        with pytest.raises(PrivateFileConflict):
            await repository.stage(
                scope=seed.owner_a_scope,
                thread_id=threads["owner_a"],
                kind="upload",
                logical_path=ready.logical_path,
                media_type="text/plain",
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_repository_source_relation_requires_same_scope_ready_nonself_source(private_files) -> None:
    from deerflow.persistence.private_work.file_repository import (
        PrivateFileConflict,
        PrivateFileRepository,
    )

    seed, threads = private_files
    async with seed.factory() as session, session.begin():
        repository = PrivateFileRepository(session)
        source = await repository.stage(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            kind="upload",
            logical_path="uploads/source.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        await repository.append_chunk(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            file_id=source.id,
            chunk_index=0,
            content=b"source",
            size=6,
            sha256=hashlib.sha256(b"source").hexdigest(),
        )
        source = await repository.finalize(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            file_id=source.id,
            expected_size=6,
            expected_sha256=hashlib.sha256(b"source").hexdigest(),
        )
        converted = await repository.stage(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            kind="workspace",
            logical_path="workspace/source.md",
            media_type="text/markdown",
            source_file_id=source.id,
        )
        assert converted.source_file_id == source.id

        with pytest.raises(PrivateFileConflict):
            await repository.stage(
                scope=seed.owner_a_scope,
                thread_id=threads["owner_a"],
                kind="upload",
                logical_path="uploads/invalid-source-link.txt",
                media_type="text/plain",
                source_file_id=source.id,
            )

        foreign_source = await repository.stage(
            scope=seed.owner_b_scope,
            thread_id=threads["owner_b"],
            kind="upload",
            logical_path="uploads/foreign.txt",
            media_type="text/plain",
        )
        with pytest.raises(PrivateFileConflict):
            await repository.stage(
                scope=seed.owner_a_scope,
                thread_id=threads["owner_a"],
                kind="workspace",
                logical_path="workspace/foreign.md",
                media_type="text/markdown",
                source_file_id=foreign_source.id,
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_repository_lists_all_ready_kinds_with_bounded_stable_keyset(private_files) -> None:
    from deerflow.persistence.private_work.file_repository import (
        PrivateFileConflict,
        PrivateFileRepository,
    )

    seed, threads = private_files
    async with seed.factory() as session, session.begin():
        repository = PrivateFileRepository(session)
        ready = []
        for kind, logical_path in (
            ("workspace", "b/workspace.md"),
            ("output", "c/output.txt"),
            ("upload", "a/upload.txt"),
        ):
            staged = await repository.stage(
                scope=seed.owner_a_scope,
                thread_id=threads["owner_a"],
                kind=kind,
                logical_path=logical_path,
                media_type="text/plain",
            )
            ready.append(
                await repository.finalize(
                    scope=seed.owner_a_scope,
                    thread_id=threads["owner_a"],
                    file_id=staged.id,
                    expected_size=0,
                    expected_sha256=hashlib.sha256(b"").hexdigest(),
                )
            )
        await repository.stage(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            kind="upload",
            logical_path="d/staging.txt",
            media_type="text/plain",
        )

        first_page = await repository.list_ready(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            limit=2,
        )
        second_page = await repository.list_ready(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            after=(first_page[-1].logical_path, first_page[-1].version, first_page[-1].id),
            limit=2,
        )
        assert [(item.logical_path, item.kind) for item in (*first_page, *second_page)] == [
            ("a/upload.txt", "upload"),
            ("b/workspace.md", "workspace"),
            ("c/output.txt", "output"),
        ]
        offset_page = await repository.list_ready(
            scope=seed.owner_a_scope,
            thread_id=threads["owner_a"],
            offset=2,
            limit=2,
        )
        assert [(item.logical_path, item.kind) for item in offset_page] == [
            ("c/output.txt", "output"),
        ]
        assert (
            await repository.list_ready(
                scope=seed.owner_b_scope,
                thread_id=threads["owner_a"],
                limit=100,
            )
            == ()
        )
        assert (
            await repository.list_ready(
                scope=seed.project_b_owner_a_scope,
                thread_id=threads["owner_a"],
                limit=100,
            )
            == ()
        )
        with pytest.raises(PrivateFileConflict):
            await repository.list_ready(
                scope=seed.owner_a_scope,
                thread_id=threads["owner_a"],
                limit=101,
            )
        for invalid_offset in (-1, 1 << 63):
            with pytest.raises(PrivateFileConflict):
                await repository.list_ready(
                    scope=seed.owner_a_scope,
                    thread_id=threads["owner_a"],
                    offset=invalid_offset,
                )
        with pytest.raises(PrivateFileConflict):
            await repository.list_ready(
                scope=seed.owner_a_scope,
                thread_id=threads["owner_a"],
                after=(
                    first_page[-1].logical_path,
                    first_page[-1].version,
                    first_page[-1].id,
                ),
                offset=1,
            )
