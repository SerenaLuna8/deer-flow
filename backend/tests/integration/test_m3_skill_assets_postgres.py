from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import logging
import threading
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.projects.context import ProjectContext, resolve_project_context
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetConflict, AssetForbidden, AssetNotFound, AssetValidationFailed
from app.shared_assets.models import SkillArchiveFile, WorkflowStatus
from deerflow.persistence.shared_assets import SkillVersionFileRow, SkillVersionRow


async def _seed_actor_and_project(
    engine: AsyncEngine,
    factory: async_sessionmaker,
    *,
    label: str,
    role: str = "editor",
) -> ProjectContext:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'user',:now,false,0)"""
            ),
            {"id": str(user_id), "email": f"{label}-{user_id}@example.com", "now": now},
        )
        await connection.execute(
            text(
                """INSERT INTO projects
                (id,slug,display_name,created_by_user_id,created_at,updated_at)
                VALUES (:id,:slug,:name,:user,:now,:now)"""
            ),
            {
                "id": project_id,
                "slug": f"{label}-{str(project_id)[:8]}",
                "name": label,
                "user": str(user_id),
                "now": now,
            },
        )
        await connection.execute(
            text(
                """INSERT INTO project_memberships
                (id,project_id,user_id,role,status,version)
                VALUES (:id,:project,:user,:role,'active',1)"""
            ),
            {
                "id": membership_id,
                "project": project_id,
                "user": str(user_id),
                "role": role,
            },
        )
    async with factory() as session:
        return await resolve_project_context(session, user_id, project_id, f"req-{label}")


async def _seed_system_admin(engine: AsyncEngine) -> SystemAssetGovernanceContext:
    user_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'system_admin',:now,false,0)"""
            ),
            {
                "id": str(user_id),
                "email": f"system-{user_id}@example.com",
                "now": datetime.now(UTC),
            },
        )
    return SystemAssetGovernanceContext(user_id=user_id, request_id="req-system")


def _archive(*, required_secret: bool = False) -> tuple[SkillArchiveFile, ...]:
    secret = ""
    if required_secret:
        secret = "required-secrets:\n  - name: API_TOKEN\n    optional: false\n"
    manifest = (f"---\nname: project-skill\ndescription: A project-scoped test skill\ncompatibility: deerflow>=1\n{secret}---\n\nUse the bundled script.\n").encode()
    return (
        SkillArchiveFile("SKILL.md", manifest, "text/markdown"),
        SkillArchiveFile("scripts/run.py", b"print('ok')\n", "text/x-python"),
    )


@pytest.mark.asyncio
async def test_project_skill_publishes_complete_snapshot_and_hides_cross_project(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="skill-first")
    outsider = await _seed_actor_and_project(engine, factory, label="skill-other")
    service = service_module.SkillService(factory)
    try:
        asset = await service.create_asset(editor, service_module.CreateSkill("project-skill", "Project Skill"))
        draft = await service.create_version_from_archive(
            editor,
            asset.id,
            _archive(),
            expected_asset_version=1,
        )
        with pytest.raises(AssetNotFound):
            await service.load_version_files(editor, asset.id, draft.id)
        published = await service.publish(editor, asset.id, draft.id, expected_asset_version=2)

        assert published.workflow_status is WorkflowStatus.PUBLISHED
        assert (await service.get(editor, asset.id)).current_published_version_id == published.id
        assert (await service.get_version_history(editor, asset.id)) == (published,)
        loaded = await service.load_version_files(editor, asset.id, published.id)
        assert loaded == _archive()
        with pytest.raises(AssetNotFound):
            await service.load_version_files(outsider, asset.id, published.id)

        async with factory() as session:
            rows = (await session.execute(select(SkillVersionFileRow).where(SkillVersionFileRow.skill_version_id == published.id).order_by(SkillVersionFileRow.path))).scalars().all()
        assert [row.path for row in rows] == ["SKILL.md", "scripts/run.py"]
        assert all(row.sha256 == hashlib.sha256(row.content).hexdigest() for row in rows)

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE skill_version_files SET content='changed' WHERE skill_version_id=:id"),
                    {"id": published.id},
                )
        with pytest.raises(dataclasses.FrozenInstanceError):
            published.payload_checksum = "0" * 64
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_skill_secret_requirement_is_sanitized_without_credential_materialization(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="skill-secret")
    service = service_module.SkillService(factory)
    try:
        async with engine.connect() as connection:
            credential_count_before = (await connection.execute(text("SELECT count(*) FROM credentials"))).scalar_one()
            grant_count_before = (await connection.execute(text("SELECT count(*) FROM credential_grants"))).scalar_one()

        asset = await service.create_asset(editor, service_module.CreateSkill("secret-skill", "Secret Skill"))
        draft = await service.create_version_from_archive(
            editor,
            asset.id,
            _archive(required_secret=True),
            expected_asset_version=1,
        )

        assert [(item.name, item.optional) for item in draft.secret_requirements] == [("API_TOKEN", False)]
        async with factory() as session:
            row = await session.get(SkillVersionRow, draft.id)
            assert row is not None
            assert row.secret_requirements == [{"name": "API_TOKEN", "optional": False}]
            assert row.frontmatter["required-secrets"] == [{"name": "API_TOKEN", "optional": False}]
            raw_manifest = await session.scalar(
                select(SkillVersionFileRow.content).where(
                    SkillVersionFileRow.skill_version_id == draft.id,
                    SkillVersionFileRow.path == "SKILL.md",
                )
            )
            assert raw_manifest is not None
            assert b"value:" not in raw_manifest
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT count(*) FROM credentials"))).scalar_one() == credential_count_before
            assert (await connection.execute(text("SELECT count(*) FROM credential_grants"))).scalar_one() == grant_count_before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_secret_key_is_rejected_before_version_persistence_without_log_leak(
    migrated_postgres_database_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="skill-duplicate-secret")
    service = service_module.SkillService(factory)
    raw_value = "raw-" + "super-secret"
    manifest = (
        "---\n"
        "name: duplicate-secret-skill\n"
        "description: Duplicate secret key must fail closed\n"
        "required-secrets:\n"
        "  - name: API_TOKEN\n"
        f"    value: {raw_value}\n"
        "required-secrets:\n"
        "  - name: API_TOKEN\n"
        "    optional: false\n"
        "---\n\n"
        "Never persist the shadowed declaration.\n"
    ).encode()
    caplog.set_level(logging.WARNING)
    try:
        asset = await service.create_asset(
            editor,
            service_module.CreateSkill("duplicate-secret-skill", "Duplicate Secret Skill"),
        )

        with pytest.raises(AssetValidationFailed) as exc_info:
            await service.create_version_from_archive(
                editor,
                asset.id,
                (SkillArchiveFile("SKILL.md", manifest, "text/markdown"),),
                expected_asset_version=1,
            )

        assert raw_value not in caplog.text
        assert raw_value not in str(exc_info.value)
        async with engine.connect() as connection:
            version_count = (
                await connection.execute(
                    text("SELECT count(*) FROM skill_versions WHERE skill_id=:skill"),
                    {"skill": asset.id},
                )
            ).scalar_one()
            file_count = (
                await connection.execute(
                    text(
                        """SELECT count(*) FROM skill_version_files AS files
                        JOIN skill_versions AS versions ON versions.id=files.skill_version_id
                        WHERE versions.skill_id=:skill"""
                    ),
                    {"skill": asset.id},
                )
            ).scalar_one()
        assert version_count == 0
        assert file_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publish_revalidates_draft_file_semantics_and_preserves_request_id(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="skill-revalidate")
    service = service_module.SkillService(factory)
    try:
        asset = await service.create_asset(editor, service_module.CreateSkill("revalidate-skill", "Revalidate Skill"))
        draft = await service.create_version_from_archive(
            editor,
            asset.id,
            _archive(),
            expected_asset_version=1,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """DELETE FROM skill_version_files
                    WHERE skill_version_id=:version AND path='scripts/run.py'"""
                ),
                {"version": draft.id},
            )
            content = b"print('ok')\n"
            await connection.execute(
                text(
                    """INSERT INTO skill_version_files
                    (skill_version_id,path,media_type,size_bytes,sha256,content)
                    VALUES (:version,'scripts/run.py','inode/symlink',:size,:sha,:content)"""
                ),
                {
                    "version": draft.id,
                    "size": len(content),
                    "sha": hashlib.sha256(content).hexdigest(),
                    "content": content,
                },
            )

        with pytest.raises(AssetValidationFailed) as exc_info:
            await service.publish(editor, asset.id, draft.id, expected_asset_version=2)
        assert exc_info.value.request_id == editor.request_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_archived_snapshot_remains_loadable_while_suspended_snapshot_fails_closed(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_actor_and_project(engine, factory, label="skill-lifecycle", role="admin")
    editor = await _seed_actor_and_project(engine, factory, label="skill-editor")
    service = service_module.SkillService(factory)
    try:
        archived_asset = await service.create_asset(admin, service_module.CreateSkill("archived-skill", "Archived Skill"))
        archived_draft = await service.create_version_from_archive(
            admin,
            archived_asset.id,
            _archive(),
            expected_asset_version=1,
        )
        archived_version = await service.publish(admin, archived_asset.id, archived_draft.id, expected_asset_version=2)
        archived = await service.archive(admin, archived_asset.id, expected_asset_version=3)
        assert archived.status == "archived"
        assert await service.load_version_files(admin, archived_asset.id, archived_version.id) == _archive()
        with pytest.raises(AssetConflict):
            await service.create_version_from_archive(
                admin,
                archived_asset.id,
                _archive(),
                expected_asset_version=4,
            )

        suspended_asset = await service.create_asset(admin, service_module.CreateSkill("suspended-skill", "Suspended Skill"))
        suspended_draft = await service.create_version_from_archive(
            admin,
            suspended_asset.id,
            _archive(),
            expected_asset_version=1,
        )
        suspended_version = await service.publish(admin, suspended_asset.id, suspended_draft.id, expected_asset_version=2)
        suspended = await service.suspend(admin, suspended_asset.id, expected_asset_version=3)
        assert suspended.status == "suspended"
        with pytest.raises(AssetNotFound):
            await service.load_version_files(admin, suspended_asset.id, suspended_version.id)
        with pytest.raises(AssetForbidden):
            await service.suspend(editor, uuid.uuid4(), expected_asset_version=1)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bound_system_skill_snapshot_is_visible_only_in_bound_project(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    project = await _seed_actor_and_project(engine, factory, label="skill-bound")
    outsider = await _seed_actor_and_project(engine, factory, label="skill-unbound")
    system = await _seed_system_admin(engine)
    service = service_module.SkillService(factory)
    try:
        asset = await service.create_asset(system, service_module.CreateSkill("system-skill", "System Skill"))
        draft = await service.create_version_from_archive(
            system,
            asset.id,
            _archive(),
            expected_asset_version=1,
        )
        with pytest.raises(AssetNotFound):
            await service.load_version_files(system, asset.id, draft.id)
        published = await service.publish(system, asset.id, draft.id, expected_asset_version=2)
        with pytest.raises(AssetNotFound):
            await service.load_version_files(project, asset.id, published.id)

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO project_system_skill_bindings
                    (project_id,system_skill_id,skill_version_id,created_by_user_id,updated_by_user_id)
                    VALUES (:project,:asset,:version,:user,:user)"""
                ),
                {
                    "project": project.project_id,
                    "asset": asset.id,
                    "version": published.id,
                    "user": str(project.user_id),
                },
            )

        assert await service.load_version_files(project, asset.id, published.id) == _archive()
        with pytest.raises(AssetNotFound):
            await service.load_version_files(outsider, asset.id, published.id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_skill_publish_has_one_optimistic_winner(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="skill-race")
    service = service_module.SkillService(factory)
    try:
        asset = await service.create_asset(editor, service_module.CreateSkill("race-skill", "Race Skill"))
        draft = await service.create_version_from_archive(
            editor,
            asset.id,
            _archive(),
            expected_asset_version=1,
        )

        results = await asyncio.gather(
            service.publish(editor, asset.id, draft.id, expected_asset_version=2),
            service.publish(editor, asset.id, draft.id, expected_asset_version=2),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, AssetConflict) for result in results) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_loading_snapshot_holds_asset_lock_against_concurrent_suspend(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    repository_module = importlib.import_module("app.shared_assets.skill_repository")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_actor_and_project(engine, factory, label="skill-load-lock", role="admin")
    service = service_module.SkillService(factory)
    release = asyncio.Event()
    entered = asyncio.Event()
    original = repository_module.SkillRepository.load_project_version

    async def gated_load(repository, context, asset_id, version_id):
        record = await original(repository, context, asset_id, version_id)
        entered.set()
        await release.wait()
        return record

    monkeypatch.setattr(repository_module.SkillRepository, "load_project_version", gated_load)
    try:
        asset = await service.create_asset(admin, service_module.CreateSkill("load-lock-skill", "Load Lock Skill"))
        draft = await service.create_version_from_archive(
            admin,
            asset.id,
            _archive(),
            expected_asset_version=1,
        )
        published = await service.publish(admin, asset.id, draft.id, expected_asset_version=2)
        load_task = asyncio.create_task(service.load_version_files(admin, asset.id, published.id))
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            with pytest.raises(DBAPIError):
                async with engine.begin() as connection:
                    await connection.execute(text("SET LOCAL lock_timeout = '250ms'"))
                    await connection.execute(
                        text("UPDATE skills SET status='suspended' WHERE id=:asset"),
                        {"asset": asset.id},
                    )
        finally:
            release.set()
        assert await asyncio.wait_for(load_task, timeout=2) == _archive()
    finally:
        release.set()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["publish", "load"])
async def test_skill_snapshot_hashing_yields_to_event_loop(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label=f"skill-offload-{operation}")
    service = service_module.SkillService(factory)
    entered = threading.Event()
    release = threading.Event()
    hash_threads: list[int] = []
    main_thread_id = threading.get_ident()
    real_sha256 = service_module.hashlib.sha256

    def gated_sha256(data=b""):
        hash_threads.append(threading.get_ident())
        if not entered.is_set():
            entered.set()
            if not release.wait(timeout=0.5):
                raise RuntimeError("hashing blocked the event loop")
        return real_sha256(data)

    async def heartbeat() -> None:
        while not entered.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()

    try:
        asset = await service.create_asset(
            editor,
            service_module.CreateSkill(f"offload-{operation}", f"Offload {operation}"),
        )
        draft = await service.create_version_from_archive(
            editor,
            asset.id,
            _archive(),
            expected_asset_version=1,
        )
        version = draft
        if operation == "load":
            version = await service.publish(editor, asset.id, draft.id, expected_asset_version=2)

        monkeypatch.setattr(
            service_module,
            "hashlib",
            SimpleNamespace(sha256=gated_sha256),
        )
        if operation == "publish":
            snapshot_operation = service.publish(
                editor,
                asset.id,
                draft.id,
                expected_asset_version=2,
            )
        else:
            snapshot_operation = service.load_version_files(editor, asset.id, version.id)

        await asyncio.wait_for(
            asyncio.gather(snapshot_operation, heartbeat()),
            timeout=2,
        )
        assert hash_threads
        assert all(thread_id != main_thread_id for thread_id in hash_threads)
    finally:
        release.set()
        await engine.dispose()
