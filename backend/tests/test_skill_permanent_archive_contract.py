from __future__ import annotations

import inspect
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from support.skill_version_fixture import (
    assemble_and_seal_skill_version,
    sealed_skill_version_fixture,
)

from app.private_work.retention_purge import RetentionPurger
from app.private_work.run_service import PrivateRunService
from app.shared_assets import skill_deletion
from app.shared_assets.skill_repository import SkillRepository
from app.worker.retention import RetentionPurgeJobHandler

ROOT = Path(__file__).resolve().parents[1]


async def _seed_project_skill(
    connection: AsyncConnection,
    *,
    user_id: uuid.UUID,
    suffix: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    project_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    fixture = sealed_skill_version_fixture(
        version_id,
        name=f"permanent-archive-{suffix}",
    )
    await connection.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (
                   :project_id, :slug, :display_name, :user_id
               )"""
        ),
        {
            "project_id": project_id,
            "slug": f"permanent-archive-{suffix}",
            "display_name": f"Permanent archive {suffix}",
            "user_id": str(user_id),
        },
    )
    await connection.execute(
        text(
            """INSERT INTO skills (
                   id, scope, project_id, slug, display_name,
                   status, created_by_user_id
               ) VALUES (
                   :skill_id, 'project', :project_id, :slug, :display_name,
                   'active', :user_id
               )"""
        ),
        {
            "skill_id": skill_id,
            "project_id": project_id,
            "slug": f"permanent-archive-{suffix}",
            "display_name": f"Permanent archive {suffix}",
            "user_id": str(user_id),
        },
    )
    await connection.execute(
        text(
            """INSERT INTO skill_versions (
                   id, skill_id, version_number, scan_decision,
                   payload_checksum, file_count, content_size_bytes,
                   files_sealed, created_by_user_id
               ) VALUES (
                   :version_id, :skill_id, 1, 'allow', :payload_checksum,
                   :file_count, :content_size_bytes, false, :user_id
               )"""
        ),
        {
            "version_id": version_id,
            "skill_id": skill_id,
            "payload_checksum": fixture.payload_checksum,
            "file_count": fixture.file_count,
            "content_size_bytes": fixture.content_size_bytes,
            "user_id": str(user_id),
        },
    )
    await assemble_and_seal_skill_version(connection, fixture)
    await connection.execute(
        text(
            """UPDATE skills
               SET current_version_id=:version_id
               WHERE id=:skill_id"""
        ),
        {"version_id": version_id, "skill_id": skill_id},
    )
    return project_id, skill_id, version_id


def test_archived_skill_versions_have_no_physical_purge_interface() -> None:
    for name in (
        "ArchivedSkillPurger",
        "ArchivedSkillPurgeReconciler",
        "ArchivedSkillPurgeReport",
        "DurableArchivedSkillPurgeAuditSink",
        "archived_skill_purge_reconciler_runtime",
    ):
        assert not hasattr(skill_deletion, name)

    for name in (
        "destroy_project_asset_secrets",
        "lock_project_purge_scope",
        "list_archived_project_assets_for_purge",
        "plan_archived_project_asset_purge",
        "purge_archived_project_asset_versions",
    ):
        assert not hasattr(SkillRepository, name)

    for owner in (
        PrivateRunService,
        RetentionPurger,
        RetentionPurgeJobHandler,
    ):
        assert (
            "archived_skill_purger"
            not in inspect.signature(
                owner.__init__,
            ).parameters
        )


def test_runtime_wiring_and_schema_expose_no_archived_skill_purge_path() -> None:
    for relative_path in (
        "app/gateway/deps.py",
        "app/worker/app.py",
        "app/worker/retention.py",
        "app/private_work/run_service.py",
        "app/private_work/retention_purge.py",
        "app/shared_assets/skill_repository.py",
        "packages/harness/deerflow/persistence/shared_assets/binding_model.py",
        "packages/harness/deerflow/persistence/shared_assets/skill_model.py",
        "packages/harness/deerflow/persistence/full_schema.sql",
    ):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "archived_skill_purg" not in source.lower(), relative_path
        assert "destroy_project_asset_secrets" not in source, relative_path

    secret_model_source = (ROOT / "packages/harness/deerflow/persistence/shared_assets/skill_secret_model.py").read_text(encoding="utf-8")
    assert "reason IN ('replace', 'clear')" in secret_model_source
    assert "'skill_delete'" not in secret_model_source
    assert "'version_purge'" not in secret_model_source

    full_schema_source = (ROOT / "packages/harness/deerflow/persistence/full_schema.sql").read_text(encoding="utf-8")
    skill_reason_constraint = full_schema_source.split(
        "CONSTRAINT ck_project_skill_secret_tombstones_reason",
        maxsplit=1,
    )[1].split(")", maxsplit=1)[0]
    assert "reason IN ('replace', 'clear'" in skill_reason_constraint
    assert "'skill_delete'" not in skill_reason_constraint
    assert "'version_purge'" not in skill_reason_constraint
    assert "CONSTRAINT ck_project_mcp_secret_tombstones_reason CHECK (reason IN ('replace', 'clear', 'definition_change', 'version_purge'))" in full_schema_source


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_database_rejects_archived_skill_file_purge_bypass_but_allows_due_project_final_deletion(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.begin() as connection:
            user_id = uuid.uuid4()
            await connection.execute(
                text(
                    """INSERT INTO users (
                           id, email, username, system_role, created_at,
                           needs_setup, token_version
                       ) VALUES (
                           :id, :email, :username, 'system_admin', now(),
                           false, 1
                       )"""
                ),
                {
                    "id": str(user_id),
                    "email": "permanent-archive@example.invalid",
                    "username": "permanent_archive_admin",
                },
            )

            archived_project_id, archived_skill_id, archived_version_id = await _seed_project_skill(
                connection,
                user_id=user_id,
                suffix="archived",
            )
            await connection.execute(
                text(
                    """UPDATE skills
                       SET current_version_id=NULL, status='archived'
                       WHERE id=:skill_id"""
                ),
                {"skill_id": archived_skill_id},
            )
            await connection.execute(
                text(
                    """SELECT set_config(
                           'deerflow.archived_skill_purge_asset_id',
                           :skill_id,
                           true
                       )"""
                ),
                {"skill_id": str(archived_skill_id)},
            )

            savepoint = await connection.begin_nested()
            with pytest.raises(
                DBAPIError,
                match="Skill version files are immutable",
            ):
                await connection.execute(
                    text(
                        """DELETE FROM skill_version_files
                           WHERE skill_version_id=:version_id"""
                    ),
                    {"version_id": archived_version_id},
                )
            await savepoint.rollback()
            assert (
                await connection.scalar(
                    text(
                        """SELECT count(*) FROM skill_version_files
                           WHERE skill_version_id=:version_id"""
                    ),
                    {"version_id": archived_version_id},
                )
                == 1
            )
            assert (
                await connection.scalar(
                    text("SELECT status FROM projects WHERE id=:project_id"),
                    {"project_id": archived_project_id},
                )
                == "active"
            )

            due_project_id, due_skill_id, due_version_id = await _seed_project_skill(
                connection,
                user_id=user_id,
                suffix="due",
            )
            await connection.execute(
                text(
                    """UPDATE projects
                       SET status='pending_deletion',
                           deletion_requested_at=now() - interval '2 days',
                           deletion_effective_at=now() - interval '1 day',
                           deletion_requested_by_user_id=:user_id
                       WHERE id=:project_id"""
                ),
                {"project_id": due_project_id, "user_id": str(user_id)},
            )
            await connection.execute(
                text(
                    """DELETE FROM skill_version_files
                       WHERE skill_version_id=:version_id"""
                ),
                {"version_id": due_version_id},
            )
            await connection.execute(
                text(
                    """UPDATE skills
                       SET current_version_id=NULL, status='archived'
                       WHERE id=:skill_id"""
                ),
                {"skill_id": due_skill_id},
            )
            await connection.execute(
                text("DELETE FROM skill_versions WHERE id=:version_id"),
                {"version_id": due_version_id},
            )
            assert (
                await connection.scalar(
                    text(
                        """SELECT count(*) FROM skill_versions
                           WHERE id=:version_id"""
                    ),
                    {"version_id": due_version_id},
                )
                == 0
            )
    finally:
        await engine.dispose()
