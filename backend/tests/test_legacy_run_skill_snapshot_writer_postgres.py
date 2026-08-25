from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.private_work.legacy_run_skill_snapshot_writer as legacy_writer_module
from app.private_work.errors import LegacyAdmissionBusy, PrivateWorkTooLarge
from app.private_work.legacy_run_skill_snapshot_writer import (
    LEGACY_ADMISSION_POLICY,
    LegacyAdmissionByteGate,
    LegacyRunSkillSnapshotWriter,
)
from app.shared_assets.models import (
    AssetKind,
    AssetScope,
    ResolvedSkillSnapshot,
    ResolvedSkillVersionSnapshot,
)
from app.shared_assets.run_snapshot_codec import decode_run_asset_snapshot
from app.shared_assets.skill_version_facts import skill_version_archive_facts
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.shared_assets import SkillRow, SkillVersionRow


@dataclass(frozen=True, slots=True)
class _SkillScope:
    project_id: uuid.UUID
    skill_id: uuid.UUID
    version_id: uuid.UUID
    checksum: str
    file_count: int
    content_size_bytes: int


async def _seed_skill(
    session: AsyncSession,
    *,
    content: bytes = b"---\nname: legacy-writer\ndescription: Legacy writer.\n---\n",
) -> _SkillScope:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    sha256 = hashlib.sha256(content).hexdigest()
    facts = skill_version_archive_facts((("SKILL.md", sha256, len(content)),))
    await session.execute(
        text(
            """INSERT INTO users (
                   id, email, username, system_role, created_at,
                   needs_setup, token_version
               ) VALUES (
                   :id, :email, :username, 'user', now(), false, 1
               )"""
        ),
        {
            "id": str(user_id),
            "email": f"legacy-{user_id}@example.invalid",
            "username": f"legacy_{user_id.hex[:12]}",
        },
    )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (:id, :slug, 'Legacy writer', :user_id)"""
        ),
        {
            "id": project_id,
            "slug": f"legacy-{project_id.hex[:12]}",
            "user_id": str(user_id),
        },
    )
    await session.execute(
        text(
            """INSERT INTO skills (
                   id, scope, project_id, slug, display_name, status,
                   current_version_id, created_by_user_id
               ) VALUES (
                   :id, 'project', :project_id, 'legacy-writer',
                   'Legacy writer', 'active', NULL, :user_id
               )"""
        ),
        {
            "id": skill_id,
            "project_id": project_id,
            "user_id": str(user_id),
        },
    )
    await session.execute(
        text(
            """INSERT INTO skill_versions (
                   id, skill_id, version_number, scan_decision,
                   payload_checksum, file_count, content_size_bytes,
                   files_sealed, created_by_user_id
               ) VALUES (
                   :id, :skill_id, 1, 'allow', :checksum, 1, :size,
                   false, :user_id
               )"""
        ),
        {
            "id": version_id,
            "skill_id": skill_id,
            "checksum": facts.payload_checksum,
            "size": len(content),
            "user_id": str(user_id),
        },
    )
    await session.execute(
        text("SELECT set_config('deerflow.asset_version_assembly', :version_id, true)"),
        {"version_id": str(version_id)},
    )
    await session.execute(
        text(
            """INSERT INTO skill_version_files (
                   skill_version_id, path, media_type, size_bytes,
                   sha256, content
               ) VALUES (
                   :version_id, 'SKILL.md', 'text/markdown', :size,
                   :sha256, :content
               )"""
        ),
        {
            "version_id": version_id,
            "size": len(content),
            "sha256": sha256,
            "content": content,
        },
    )
    await session.execute(
        text("UPDATE skill_versions SET files_sealed=true WHERE id=:version_id"),
        {"version_id": version_id},
    )
    await session.execute(
        text(
            """UPDATE skills SET current_version_id=:version_id
               WHERE id=:skill_id"""
        ),
        {"version_id": version_id, "skill_id": skill_id},
    )
    return _SkillScope(
        project_id=project_id,
        skill_id=skill_id,
        version_id=version_id,
        checksum=facts.payload_checksum,
        file_count=1,
        content_size_bytes=len(content),
    )


async def _locked_skill(
    session: AsyncSession,
    scope: _SkillScope,
) -> tuple[SkillRow, SkillVersionRow]:
    return (
        await session.execute(select(SkillRow, SkillVersionRow).join(SkillVersionRow, SkillVersionRow.skill_id == SkillRow.id).where(SkillVersionRow.id == scope.version_id).with_for_update(read=True, of=[SkillRow, SkillVersionRow]))
    ).one()


def _snapshot(
    scope: _SkillScope,
    *,
    content_size_bytes: int | None = None,
) -> ResolvedSkillVersionSnapshot:
    return ResolvedSkillVersionSnapshot(
        kind=AssetKind.SKILL,
        scope=AssetScope.PROJECT,
        asset_id=scope.skill_id,
        version_id=scope.version_id,
        checksum=scope.checksum,
        catalog_generation=7,
        dependency_version_ids=(),
        file_count=scope.file_count,
        content_size_bytes=(scope.content_size_bytes if content_size_bytes is None else content_size_bytes),
        secret_requirements=(),
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_admission_gate_is_database_wide_fail_fast_and_transactional(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    gate = LegacyAdmissionByteGate()
    try:
        async with factory() as holder:
            async with holder.begin():
                await gate.acquire(holder, request_id="holder")
                async with factory() as contender, contender.begin():
                    with pytest.raises(LegacyAdmissionBusy) as busy:
                        await gate.acquire(contender, request_id="contender")
                    assert busy.value.request_id == "contender"

            async with factory() as after_commit, after_commit.begin():
                await gate.acquire(after_commit, request_id="after-commit")

        async with factory() as rollback_holder:
            transaction = await rollback_holder.begin()
            await gate.acquire(rollback_holder, request_id="rollback-holder")
            await transaction.rollback()
        async with factory() as after_rollback, after_rollback.begin():
            await gate.acquire(after_rollback, request_id="after-rollback")

        disconnected = factory()
        await disconnected.begin()
        disconnected_pid = int(await disconnected.scalar(text("SELECT pg_backend_pid()")))
        await gate.acquire(disconnected, request_id="disconnect-holder")
        async with factory() as terminator:
            assert (
                await terminator.scalar(
                    text("SELECT pg_terminate_backend(:backend_pid)"),
                    {"backend_pid": disconnected_pid},
                )
                is True
            )
            async with asyncio.timeout(5):
                while await terminator.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid=:backend_pid)"),
                    {"backend_pid": disconnected_pid},
                ):
                    await asyncio.sleep(0)
        await disconnected.invalidate()
        async with factory() as after_disconnect, after_disconnect.begin():
            await gate.acquire(after_disconnect, request_id="after-disconnect")
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cancelled_admission_transaction_releases_legacy_gate(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    gate = LegacyAdmissionByteGate()
    acquired = asyncio.Event()
    remain_active = asyncio.Event()

    async def hold_until_cancelled() -> None:
        async with factory() as session, session.begin():
            await gate.acquire(session, request_id="cancel-holder")
            acquired.set()
            await remain_active.wait()

    task = asyncio.create_task(hold_until_cancelled())
    try:
        await acquired.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        async with factory() as contender, contender.begin():
            await gate.acquire(contender, request_id="after-cancel")
    finally:
        remain_active.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_writer_preflights_then_reads_one_exact_skill_after_gate(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_skill(session)

        def capture(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with factory() as session, session.begin():
            locked = await _locked_skill(session, scope)
            prepared = await LegacyRunSkillSnapshotWriter().prepare(
                session,
                request_id="legacy-writer-success",
                locked_skills=(locked,),
                snapshots=(_snapshot(scope),),
            )

        assert prepared.policy_digest == LEGACY_ADMISSION_POLICY.canonical_digest()
        assert prepared.actual_encoded_bytes <= prepared.encoded_upper_bound_bytes
        assert len(prepared.snapshot_jsons) == 1
        decoded = decode_run_asset_snapshot(prepared.snapshot_jsons[0])
        assert type(decoded) is ResolvedSkillSnapshot
        assert decoded.version_id == scope.version_id
        assert [item.path for item in decoded.files] == ["SKILL.md"]

        gate_index = next(index for index, statement in enumerate(statements) if "pg_try_advisory_xact_lock" in statement)
        content_indexes = [index for index, statement in enumerate(statements) if "skill_version_files.content" in statement]
        assert len(content_indexes) == 1
        assert gate_index < content_indexes[0]
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_writer_busy_never_selects_content(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []
    gate = LegacyAdmissionByteGate()
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_skill(session)

        def capture(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with factory() as holder, holder.begin():
            await gate.acquire(holder, request_id="holder")
            async with factory() as contender, contender.begin():
                locked = await _locked_skill(contender, scope)
                with pytest.raises(LegacyAdmissionBusy):
                    await LegacyRunSkillSnapshotWriter().prepare(
                        contender,
                        request_id="busy-writer",
                        locked_skills=(locked,),
                        snapshots=(_snapshot(scope),),
                    )

        assert not any("skill_version_files.content" in statement for statement in statements)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_writer_known_oversize_attempts_no_gate_or_content(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements: list[str] = []
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_skill(session)

        def capture(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        async with factory() as session, session.begin():
            asset, version = await _locked_skill(session, scope)
            session.expunge(version)
            version.content_size_bytes = 64 * 1024 * 1024
            version.file_count = 1
            oversized = _snapshot(
                scope,
                content_size_bytes=version.content_size_bytes,
            )
            with pytest.raises(PrivateWorkTooLarge):
                await LegacyRunSkillSnapshotWriter().prepare(
                    session,
                    request_id="known-oversize",
                    locked_skills=((asset, version),),
                    snapshots=(oversized,),
                )

        assert not any("pg_try_advisory_xact_lock" in statement or "skill_version_files.content" in statement for statement in statements)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_writer_cancel_joins_codec_before_releasing_transaction_gate(
    postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    codec_started = threading.Event()
    release_codec = threading.Event()
    codec_finished = threading.Event()
    original = legacy_writer_module._encode_exact_legacy_skill

    def controlled_codec(*args, **kwargs):
        codec_started.set()
        release_codec.wait()
        try:
            return original(*args, **kwargs)
        finally:
            codec_finished.set()

    monkeypatch.setattr(
        legacy_writer_module,
        "_encode_exact_legacy_skill",
        controlled_codec,
    )
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            scope = await _seed_skill(session)

        async def prepare() -> None:
            async with factory() as session, session.begin():
                locked = await _locked_skill(session, scope)
                await LegacyRunSkillSnapshotWriter().prepare(
                    session,
                    request_id="cancel-codec",
                    locked_skills=(locked,),
                    snapshots=(_snapshot(scope),),
                )

        task = asyncio.create_task(prepare())
        assert await asyncio.to_thread(codec_started.wait, 2)
        task.cancel()
        assert not task.done()
        release_codec.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert codec_finished.is_set()

        async with factory() as contender, contender.begin():
            await LegacyAdmissionByteGate().acquire(
                contender,
                request_id="after-codec-cancel",
            )
    finally:
        release_codec.set()
        await engine.dispose()
