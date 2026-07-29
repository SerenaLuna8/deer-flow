from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import importlib
import io
import json
import logging
import threading
import uuid
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.projects.context import ProjectContext, resolve_project_context
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.models import QuotaSourceRef
from app.quotas.service import QuotaService
from app.shared_assets.agent_service import AgentService, CreateAgent
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetStorageQuotaExceeded,
    AssetValidationFailed,
)
from app.shared_assets.models import AgentPayload, SkillArchiveFile, WorkflowStatus
from app.shared_assets.skill_review import PostgresSkillReviewService
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence import bootstrap as bootstrap_module
from deerflow.persistence.shared_assets import SkillRow, SkillVersionFileRow, SkillVersionRow


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


def _archive(
    *,
    name: str = "project-skill",
    required_secret: bool = False,
) -> tuple[SkillArchiveFile, ...]:
    secret = ""
    if required_secret:
        secret = "required-secrets:\n  - name: API_TOKEN\n    optional: false\n"
    manifest = (f"---\nname: {name}\ndescription: A project-scoped test skill\ncompatibility: deerflow>=1\n{secret}---\n\nUse the bundled script.\n").encode()
    return (
        SkillArchiveFile("SKILL.md", manifest, "text/markdown"),
        SkillArchiveFile("scripts/run.py", b"print('ok')\n", "text/x-python"),
    )


def _archive_upload(*, name: str = "uploaded-project-skill") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"{name}/SKILL.md",
            (f"---\nname: {name}\ndescription: Uploaded project Skill\n---\n\nUse the bundled script.\n").encode(),
        )
        archive.writestr(f"{name}/scripts/run.py", b"print('ok')\n")
    return buffer.getvalue()


def _quota_source_ref(payload: bytes) -> QuotaSourceRef:
    return QuotaSourceRef(
        key_id="skill-test-quota",
        hmac_hex=hmac.new(
            b"skill-test-quota-key" * 2,
            payload,
            hashlib.sha256,
        ).hexdigest(),
    )


def _service(
    service_module,
    factory: async_sessionmaker,
    *,
    storage_limit: int = 5_368_709_120,
):
    return service_module.SkillService(
        factory,
        quota=ProjectQuotaEnforcer(
            QuotaService(
                factory,
                QuotaConfig(default_storage_bytes_limit=storage_limit),
                source_ref_hasher=_quota_source_ref,
            )
        ),
    )


@pytest.mark.asyncio
async def test_project_skill_template_create_persists_one_suspended_draft_snapshot(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(
        engine,
        factory,
        label="skill-template-create",
    )
    service = _service(service_module, factory)
    try:
        created = await service.create_project_with_template(
            editor,
            service_module.CreateSkill(
                slug="meeting-brief",
                display_name="Meeting Brief",
            ),
        )

        assert created.status == "suspended"
        assert created.current_published_version_id is None
        assert created.version == 2

        async with factory() as session:
            version = (
                await session.execute(
                    select(SkillVersionRow).where(
                        SkillVersionRow.skill_id == created.id,
                    )
                )
            ).scalar_one()
            file = (
                await session.execute(
                    select(SkillVersionFileRow).where(
                        SkillVersionFileRow.skill_version_id == version.id,
                    )
                )
            ).scalar_one()
            reserved = await session.scalar(
                text(
                    """SELECT reserved FROM project_usage_counters
                       WHERE project_id=:project_id
                         AND dimension='storage_bytes'
                         AND period_key='lifetime'"""
                ),
                {"project_id": editor.project_id},
            )

        assert version.version_number == 1
        assert version.workflow_status == WorkflowStatus.DRAFT.value
        assert version.supersedes_version_id is None
        assert file.path == "SKILL.md"
        assert file.media_type == "text/markdown"
        assert file.content.decode() == ("---\nname: meeting-brief\ndescription: Describe when and how to use this skill.\n---\n\n# meeting-brief\n\nAdd instructions for this skill here.\n")
        assert file.size_bytes == len(file.content)
        assert file.sha256 == hashlib.sha256(file.content).hexdigest()
        assert reserved == len(file.content)
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_counter_reserved", [0, 37])
async def test_project_skill_delete_settles_unattributed_storage_once(
    postgres_database_url: str,
    legacy_counter_reserved: int,
) -> None:
    """A Skill with pre-attribution storage remains deletable exactly once."""

    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    content = b"x" * 37
    sha256 = hashlib.sha256(content).hexdigest()
    try:
        await bootstrap_module.bootstrap_schema(engine)
        editor = await _seed_actor_and_project(
            engine,
            factory,
            label=f"legacy-skill-{legacy_counter_reserved}",
        )
        async with factory() as session, session.begin():
            session.add(
                SkillRow(
                    id=skill_id,
                    scope="project",
                    project_id=editor.project_id,
                    slug=f"legacy-skill-{legacy_counter_reserved}",
                    display_name="Legacy Skill",
                    created_by_user_id=str(editor.user_id),
                )
            )
            session.add(
                SkillVersionRow(
                    id=version_id,
                    skill_id=skill_id,
                    version_number=1,
                    workflow_status="draft",
                    description="legacy",
                    frontmatter={},
                    secret_requirements=[],
                    scan_decision="allow",
                    scan_summary={},
                    payload_checksum="0" * 64,
                    created_by_user_id=str(editor.user_id),
                )
            )
            await session.flush()
            session.add(
                SkillVersionFileRow(
                    skill_version_id=version_id,
                    path="SKILL.md",
                    media_type="text/markdown",
                    size_bytes=len(content),
                    sha256=sha256,
                    content=content,
                )
            )
            if legacy_counter_reserved:
                await session.execute(
                    text(
                        """INSERT INTO project_usage_counters
                           (project_id, dimension, bucket, used, reserved, version)
                           VALUES (:project_id, 'storage_bytes', 'lifetime', 0, :reserved, 1)"""
                    ),
                    {
                        "project_id": editor.project_id,
                        "reserved": legacy_counter_reserved,
                    },
                )

        service = _service(service_module, factory)
        await service.delete(
            editor,
            skill_id,
            expected_asset_version=1,
        )

        async with factory() as session:
            assert await session.get(SkillRow, skill_id) is None
            counter = await session.scalar(
                text(
                    """SELECT reserved FROM project_usage_counters
                       WHERE project_id=:project_id
                         AND dimension='storage_bytes'
                         AND bucket='lifetime'"""
                ),
                {"project_id": editor.project_id},
            )
            assert counter == 0
            ledger_count = await session.scalar(
                text(
                    """SELECT count(*) FROM project_usage_ledger
                       WHERE project_id=:project_id
                         AND dimension='storage_bytes'"""
                ),
                {"project_id": editor.project_id},
            )
            assert ledger_count == (1 if legacy_counter_reserved else 0)
            assert (
                await session.scalar(
                    text(
                        """SELECT count(*) FROM project_usage_ledger
                           WHERE project_id=:project_id
                             AND source_kind IN ('release', 'release_threshold')"""
                    ),
                    {"project_id": editor.project_id},
                )
                == 0
            )

        with pytest.raises(AssetNotFound):
            await service.delete(
                editor,
                skill_id,
                expected_asset_version=1,
            )
        async with factory() as session:
            assert (
                await session.scalar(
                    text(
                        """SELECT count(*) FROM project_usage_ledger
                           WHERE project_id=:project_id
                             AND dimension='storage_bytes'"""
                    ),
                    {"project_id": editor.project_id},
                )
                == ledger_count
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_skill_publishes_complete_snapshot_and_hides_cross_project(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(
        engine,
        factory,
        label="skill-first",
        role="admin",
    )
    outsider = await _seed_actor_and_project(engine, factory, label="skill-other")
    service = _service(service_module, factory)
    try:
        asset = await service.create_asset(editor, service_module.CreateSkill("project-skill", "Project Skill"))
        assert asset.status == "suspended"
        draft = await service.create_version_from_archive(
            editor,
            asset.id,
            _archive(),
            expected_asset_version=1,
        )
        with pytest.raises(AssetNotFound):
            await service.load_version_files(editor, asset.id, draft.id)
        published = await service.publish(editor, asset.id, draft.id, expected_asset_version=2)
        with pytest.raises(AssetNotFound):
            await service.load_version_files(editor, asset.id, published.id)
        activated = await service.activate(
            editor,
            asset.id,
            expected_asset_version=3,
        )

        assert published.workflow_status is WorkflowStatus.PUBLISHED
        assert activated.status == "active"
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
        for marker in (None, uuid.uuid4()):
            with pytest.raises(DBAPIError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            """UPDATE skills
                                  SET status='archived',
                                      current_published_version_id=NULL
                                WHERE id=:asset_id"""
                        ),
                        {"asset_id": asset.id},
                    )
                    if marker is not None:
                        await connection.scalar(
                            text(
                                """SELECT set_config(
                                    'deerflow.skill_hard_delete_asset_id',
                                    :marker,
                                    true
                                )"""
                            ),
                            {"marker": str(marker)},
                        )
                    await connection.execute(
                        text(
                            """DELETE FROM skill_version_files
                                WHERE skill_version_id=:version_id"""
                        ),
                        {"version_id": published.id},
                    )
        with pytest.raises(dataclasses.FrozenInstanceError):
            published.payload_checksum = "0" * 64
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_skill_review_reads_only_exact_authorized_version(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(
        engine,
        factory,
        label="skill-review-owner",
        role="admin",
    )
    outsider = await _seed_actor_and_project(
        engine,
        factory,
        label="skill-review-outsider",
    )
    skill_service = _service(service_module, factory)
    review_service = PostgresSkillReviewService(factory)
    try:
        asset = await skill_service.create_asset(
            editor,
            service_module.CreateSkill(
                "review-exact-skill",
                "Review Exact Skill",
            ),
        )
        version = await skill_service.create_version_from_archive(
            editor,
            asset.id,
            _archive(name="review-exact-skill"),
            expected_asset_version=1,
        )

        result = await review_service.review(
            editor,
            skill_id=asset.id,
            version_id=version.id,
            expected_checksum=version.payload_checksum,
        )

        assert result.facts["subject"]["skill_id"] == str(asset.id)
        assert result.facts["subject"]["version_id"] == str(version.id)
        assert result.facts["subject"]["payload_checksum"] == version.payload_checksum
        serialized = json.dumps(
            {
                "facts": result.facts,
                "report": result.report,
            },
            sort_keys=True,
        )
        assert str(editor.project_id) not in serialized
        assert str(editor.user_id) not in serialized

        with pytest.raises(AssetNotFound):
            await review_service.review(
                outsider,
                skill_id=asset.id,
                version_id=version.id,
                expected_checksum=version.payload_checksum,
            )
        with pytest.raises(AssetNotFound):
            await review_service.review(
                editor,
                skill_id=asset.id,
                version_id=uuid.uuid4(),
                expected_checksum=version.payload_checksum,
            )
        with pytest.raises(AssetConflict):
            await review_service.review(
                editor,
                skill_id=asset.id,
                version_id=version.id,
                expected_checksum="0" * 64,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_skill_versions_reserve_full_storage_and_reject_over_limit_atomically(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(engine, factory, label="skill-quota")
    archive = _archive(name="quota-skill")
    archive_size = sum(len(file.content) for file in archive)
    service = _service(
        service_module,
        factory,
        storage_limit=archive_size,
    )
    try:
        asset = await service.create_asset(
            editor,
            service_module.CreateSkill("quota-skill", "Quota Skill"),
        )
        first = await service.create_version_from_archive(
            editor,
            asset.id,
            archive,
            expected_asset_version=1,
        )

        with pytest.raises(AssetStorageQuotaExceeded) as exc_info:
            await service.create_version_from_archive(
                editor,
                asset.id,
                archive,
                expected_asset_version=2,
            )
        assert exc_info.value.request_id == editor.request_id

        async with factory() as session:
            reserved = await session.scalar(
                text(
                    """SELECT reserved FROM project_usage_counters
                       WHERE project_id=:project_id
                         AND dimension='storage_bytes'
                         AND bucket='lifetime'"""
                ),
                {"project_id": editor.project_id},
            )
            version_count = await session.scalar(select(func.count()).select_from(SkillVersionRow).where(SkillVersionRow.skill_id == asset.id))
            persisted_bytes = await session.scalar(select(func.coalesce(func.sum(SkillVersionFileRow.size_bytes), 0)).where(SkillVersionFileRow.skill_version_id == first.id))
        assert reserved == archive_size
        assert version_count == 1
        assert persisted_bytes == archive_size
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
    service = _service(service_module, factory)
    try:
        async with engine.connect() as connection:
            credential_count_before = (await connection.execute(text("SELECT count(*) FROM credentials"))).scalar_one()
            grant_count_before = (await connection.execute(text("SELECT count(*) FROM credential_grants"))).scalar_one()

        asset = await service.create_asset(editor, service_module.CreateSkill("secret-skill", "Secret Skill"))
        draft = await service.create_version_from_archive(
            editor,
            asset.id,
            _archive(name="secret-skill", required_secret=True),
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
    service = _service(service_module, factory)
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
    service = _service(service_module, factory)
    try:
        asset = await service.create_asset(editor, service_module.CreateSkill("revalidate-skill", "Revalidate Skill"))
        draft = await service.create_version_from_archive(
            editor,
            asset.id,
            _archive(name="revalidate-skill"),
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
async def test_project_skill_delete_removes_every_version_and_suspended_snapshot_fails_closed(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_actor_and_project(engine, factory, label="skill-lifecycle", role="admin")
    editor = await _seed_actor_and_project(engine, factory, label="skill-editor")
    service = _service(service_module, factory)
    try:
        deleted_asset = await service.create_asset(admin, service_module.CreateSkill("deleted-skill", "Deleted Skill"))
        deleted_draft = await service.create_version_from_archive(
            admin,
            deleted_asset.id,
            _archive(name="deleted-skill"),
            expected_asset_version=1,
        )
        deleted_version = await service.publish(admin, deleted_asset.id, deleted_draft.id, expected_asset_version=2)
        await service.create_version_from_archive(
            admin,
            deleted_asset.id,
            _archive(name="deleted-skill"),
            expected_asset_version=3,
        )

        await service.delete(
            admin,
            deleted_asset.id,
            expected_asset_version=4,
        )

        with pytest.raises(AssetNotFound):
            await service.get(admin, deleted_asset.id)
        with pytest.raises(AssetNotFound):
            await service.load_version_files(
                admin,
                deleted_asset.id,
                deleted_version.id,
            )
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(SkillRow).where(SkillRow.id == deleted_asset.id)) == 0
            assert await session.scalar(select(func.count()).select_from(SkillVersionRow).where(SkillVersionRow.skill_id == deleted_asset.id)) == 0
            reserved = await session.scalar(
                text(
                    """SELECT reserved FROM project_usage_counters
                       WHERE project_id=:project_id
                         AND dimension='storage_bytes'
                         AND bucket='lifetime'"""
                ),
                {"project_id": admin.project_id},
            )
            assert reserved == 0

        suspended_asset = await service.create_asset(admin, service_module.CreateSkill("suspended-skill", "Suspended Skill"))
        suspended_draft = await service.create_version_from_archive(
            admin,
            suspended_asset.id,
            _archive(name="suspended-skill"),
            expected_asset_version=1,
        )
        suspended_version = await service.publish(admin, suspended_asset.id, suspended_draft.id, expected_asset_version=2)
        assert (await service.get(admin, suspended_asset.id)).status == "suspended"
        with pytest.raises(AssetNotFound):
            await service.load_version_files(admin, suspended_asset.id, suspended_version.id)
        activated = await service.activate(
            admin,
            suspended_asset.id,
            expected_asset_version=3,
        )
        assert activated.status == "active"
        assert await service.load_version_files(
            admin,
            suspended_asset.id,
            suspended_version.id,
        ) == _archive(name="suspended-skill")
        suspended = await service.suspend(admin, suspended_asset.id, expected_asset_version=4)
        assert suspended.status == "suspended"
        with pytest.raises(AssetNotFound):
            await service.load_version_files(admin, suspended_asset.id, suspended_version.id)
        with pytest.raises(AssetForbidden):
            await service.suspend(editor, uuid.uuid4(), expected_asset_version=1)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_skill_delete_rejects_immutable_agent_version_reference(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    editor = await _seed_actor_and_project(
        engine,
        factory,
        label="skill-delete-agent-ref",
        role="admin",
    )
    skills = _service(service_module, factory)
    agents = AgentService(factory)
    try:
        skill = await skills.create_asset(
            editor,
            service_module.CreateSkill("referenced-skill", "Referenced Skill"),
        )
        draft = await skills.create_version_from_archive(
            editor,
            skill.id,
            _archive(name="referenced-skill"),
            expected_asset_version=1,
        )
        published = await skills.publish(
            editor,
            skill.id,
            draft.id,
            expected_asset_version=2,
        )
        await skills.activate(
            editor,
            skill.id,
            expected_asset_version=3,
        )
        agent = await agents.create_asset(
            editor,
            CreateAgent("referencing-agent", "Referencing Agent"),
        )
        await agents.create_version(
            editor,
            agent.id,
            AgentPayload(
                description="References the immutable Skill version",
                soul="Use the selected Skill.",
                model_ref="test-model",
                tool_groups=(),
                skill_version_ids=(published.id,),
                mcp_version_ids=(),
            ),
            expected_asset_version=1,
        )

        with pytest.raises(AssetConflict) as exc_info:
            await skills.delete(
                editor,
                skill.id,
                expected_asset_version=4,
            )
        assert exc_info.value.request_id == editor.request_id
        assert (await skills.get(editor, skill.id)).id == skill.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bound_system_skill_snapshot_is_visible_only_in_bound_project(
    migrated_postgres_database_url: str,
) -> None:
    bootstrap_module = importlib.import_module("app.shared_assets.bootstrap")
    catalog_module = importlib.import_module("app.shared_assets.bootstrap.catalog")
    archive_module = importlib.import_module("app.shared_assets.bootstrap.skill_archive")
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    project = await _seed_actor_and_project(engine, factory, label="skill-bound")
    outsider = await _seed_actor_and_project(engine, factory, label="skill-unbound")
    service = _service(service_module, factory)
    try:
        await bootstrap_module.bootstrap_system_assets(factory)
        async with factory() as session:
            asset = (await session.execute(select(SkillRow).where(SkillRow.source_key == "builtin:skill:academic-paper-review"))).scalar_one()
        catalog = catalog_module.load_bootstrap_catalog()
        entry = next(item for item in catalog.entries if item.source_key == "builtin:skill:academic-paper-review")
        expected_files = archive_module.load_skill_archive(catalog_module.catalog_payload(catalog, entry))
        published_version_id = asset.current_published_version_id
        assert published_version_id is not None
        with pytest.raises(AssetNotFound):
            await service.load_version_files(
                project,
                asset.id,
                published_version_id,
            )

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
                    "version": published_version_id,
                    "user": str(project.user_id),
                },
            )

        assert (
            await service.load_version_files(
                project,
                asset.id,
                published_version_id,
            )
            == expected_files
        )
        with pytest.raises(AssetNotFound):
            await service.load_version_files(
                outsider,
                asset.id,
                published_version_id,
            )
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
    service = _service(service_module, factory)
    try:
        asset = await service.create_asset(editor, service_module.CreateSkill("race-skill", "Race Skill"))
        draft = await service.create_version_from_archive(
            editor,
            asset.id,
            _archive(name="race-skill"),
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
async def test_project_skill_archive_upload_is_atomic_per_project_and_reusable_across_projects(
    migrated_postgres_database_url: str,
) -> None:
    service_module = importlib.import_module("app.shared_assets.skill_service")
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    first_editor = await _seed_actor_and_project(
        engine,
        factory,
        label="skill-upload-first",
    )
    second_editor = await _seed_actor_and_project(
        engine,
        factory,
        label="skill-upload-second",
    )
    service = _service(service_module, factory)
    payload = _archive_upload()
    try:
        same_project_results = await asyncio.gather(
            service.create_project_from_archive_upload(
                first_editor,
                payload,
                filename="uploaded-project-skill.zip",
            ),
            service.create_project_from_archive_upload(
                first_editor,
                payload,
                filename="uploaded-project-skill.zip",
            ),
            return_exceptions=True,
        )

        created = [
            result
            for result in same_project_results
            if isinstance(
                result,
                service_module.ProjectSkillArchiveCreateResult,
            )
        ]
        assert len(created) == 1
        assert sum(isinstance(result, AssetConflict) for result in same_project_results) == 1
        assert created[0].asset.status == "suspended"
        assert created[0].asset.current_published_version_id == (created[0].version.id)
        assert created[0].version.workflow_status is WorkflowStatus.PUBLISHED

        other_project = await service.create_project_from_archive_upload(
            second_editor,
            payload,
            filename="uploaded-project-skill.zip",
        )
        assert other_project.asset.slug == created[0].asset.slug
        assert other_project.asset.project_id == second_editor.project_id

        async with factory() as session:
            first_skill_count = await session.scalar(
                select(func.count())
                .select_from(SkillRow)
                .where(
                    SkillRow.project_id == first_editor.project_id,
                    SkillRow.slug == "uploaded-project-skill",
                )
            )
            first_version_count = await session.scalar(
                select(func.count())
                .select_from(SkillVersionRow)
                .join(SkillRow, SkillRow.id == SkillVersionRow.skill_id)
                .where(
                    SkillRow.project_id == first_editor.project_id,
                    SkillRow.slug == "uploaded-project-skill",
                )
            )
        assert first_skill_count == 1
        assert first_version_count == 1
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
    service = _service(service_module, factory)
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
            _archive(name="load-lock-skill"),
            expected_asset_version=1,
        )
        published = await service.publish(admin, asset.id, draft.id, expected_asset_version=2)
        await service.activate(
            admin,
            asset.id,
            expected_asset_version=3,
        )
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
        assert await asyncio.wait_for(load_task, timeout=2) == _archive(
            name="load-lock-skill",
        )
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
    editor = await _seed_actor_and_project(
        engine,
        factory,
        label=f"skill-offload-{operation}",
        role="admin",
    )
    service = _service(service_module, factory)
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
            _archive(name=f"offload-{operation}"),
            expected_asset_version=1,
        )
        version = draft
        if operation == "load":
            version = await service.publish(editor, asset.id, draft.id, expected_asset_version=2)
            await service.activate(
                editor,
                asset.id,
                expected_asset_version=3,
            )

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
