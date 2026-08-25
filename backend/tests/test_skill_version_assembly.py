from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.skill_repository import SkillRepository
from app.shared_assets.skill_version_facts import skill_version_archive_facts
from deerflow.persistence.bootstrap import _install_full_schema
from deerflow.persistence.shared_assets import (
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)


class _Rows:
    def __init__(self, rows: tuple[tuple[str, str, int], ...]) -> None:
        self._rows = rows

    def all(self) -> tuple[tuple[str, str, int], ...]:
        return self._rows


class _AssemblySession:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.files: tuple[SkillVersionFileRow, ...] = ()

    def add(self, _row: object) -> None:
        self.events.append("add-parent")

    def add_all(self, rows: tuple[SkillVersionFileRow, ...]) -> None:
        self.events.append("add-files")
        self.files = tuple(rows)

    async def flush(self) -> None:
        self.events.append("flush")

    async def scalar(self, _statement: object) -> str:
        self.events.append("set-assembly-guc")
        return "unused"

    async def execute(self, _statement: object) -> _Rows:
        self.events.append("readback-facts")
        return _Rows(tuple((row.path, row.sha256, row.size_bytes) for row in sorted(self.files, key=lambda item: item.path)))


def _version_and_files() -> tuple[SkillVersionRow, tuple[SkillVersionFileRow, ...]]:
    version_id = uuid.uuid4()
    contents = (("SKILL.md", b"abc"), ("references/a.md", b"12345"))
    files = tuple(
        SkillVersionFileRow(
            skill_version_id=version_id,
            path=path,
            media_type="text/markdown",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            content=content,
        )
        for path, content in contents
    )
    facts = skill_version_archive_facts(tuple((row.path, row.sha256, row.size_bytes) for row in files))
    version = SkillVersionRow(
        id=version_id,
        skill_id=uuid.uuid4(),
        version_number=1,
        description="",
        frontmatter={},
        compatibility=None,
        secret_requirements=[],
        scan_decision="allow",
        scan_summary={},
        supersedes_version_id=None,
        payload_checksum=facts.payload_checksum,
        file_count=facts.file_count,
        content_size_bytes=facts.content_size_bytes,
        files_sealed=False,
        created_by_user_id=str(uuid.uuid4()),
    )
    return version, files


def test_skill_version_archive_facts_are_canonical_and_order_independent() -> None:
    facts = skill_version_archive_facts(
        (
            ("references/a.md", "b" * 64, 5),
            ("SKILL.md", "a" * 64, 3),
        )
    )

    assert facts.file_count == 2
    assert facts.content_size_bytes == 8
    assert facts.payload_checksum == ("4ee58bb446a95197ba8972eb76748b03adb5f8edc4ce5124419802c11ef27761")


@pytest.mark.asyncio
async def test_repository_rechecks_persisted_facts_before_sealing() -> None:
    version, files = _version_and_files()
    session = _AssemblySession()

    record = await SkillRepository(session)._create_version(  # type: ignore[arg-type]
        version,
        files,
        request_id="request-1",
    )

    assert record.row.files_sealed is True
    assert record.files == files
    assert session.events == [
        "add-parent",
        "flush",
        "set-assembly-guc",
        "add-files",
        "flush",
        "readback-facts",
        "flush",
    ]


@pytest.mark.asyncio
async def test_repository_refuses_to_seal_when_readback_checksum_differs() -> None:
    version, files = _version_and_files()
    version.payload_checksum = "0" * 64
    session = _AssemblySession()

    with pytest.raises(AssetValidationFailed):
        await SkillRepository(session)._create_version(  # type: ignore[arg-type]
            version,
            files,
            request_id="request-2",
        )

    assert version.files_sealed is False


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_skill_version_assembly_and_seal_are_fail_closed(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    project_skill_id = uuid.uuid4()
    system_skill_id = uuid.uuid4()
    try:
        await _install_full_schema(engine)
        async with factory() as session, session.begin():
            await session.execute(
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
                    "email": "skill-facts@example.invalid",
                    "username": "skill_facts_admin",
                },
            )
            await session.execute(
                text(
                    """INSERT INTO projects (
                           id, slug, display_name, created_by_user_id
                       ) VALUES (
                           :id, 'skill-facts', 'Skill facts', :user_id
                       )"""
                ),
                {"id": project_id, "user_id": str(user_id)},
            )
            await session.execute(
                text(
                    """INSERT INTO project_memberships (
                           id, project_id, user_id, role
                       ) VALUES (
                           :id, :project_id, :user_id, 'admin'
                       )"""
                ),
                {
                    "id": membership_id,
                    "project_id": project_id,
                    "user_id": str(user_id),
                },
            )
            session.add_all(
                [
                    SkillRow(
                        id=project_skill_id,
                        scope="project",
                        project_id=project_id,
                        slug="project-skill",
                        display_name="Project Skill",
                        status="suspended",
                        current_version_id=None,
                        revision=1,
                        source_key=None,
                        created_by_user_id=str(user_id),
                    ),
                    SkillRow(
                        id=system_skill_id,
                        scope="system",
                        project_id=None,
                        slug="system-skill",
                        display_name="System Skill",
                        status="active",
                        current_version_id=None,
                        revision=1,
                        source_key="test:system-skill",
                        created_by_user_id=str(user_id),
                    ),
                ]
            )

        project_context = ProjectContext(
            user_id=user_id,
            project_id=project_id,
            membership_id=membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="pg-project-skill",
        )
        project_version, project_files = _version_and_files()
        project_version.skill_id = project_skill_id
        project_version.created_by_user_id = str(user_id)
        async with factory() as session, session.begin():
            record = await SkillRepository(session).create_project_version(
                project_context,
                project_skill_id,
                project_version,
                project_files,
            )
            assert record.row.files_sealed is True

        async with factory() as session:
            persisted = await session.get(SkillVersionRow, project_version.id)
            assert persisted is not None
            assert persisted.files_sealed is True
            actual_count, actual_size = (
                await session.execute(
                    select(
                        func.count(),
                        func.coalesce(func.sum(SkillVersionFileRow.size_bytes), 0),
                    ).where(SkillVersionFileRow.skill_version_id == project_version.id)
                )
            ).one()
            assert (actual_count, actual_size) == (
                persisted.file_count,
                persisted.content_size_bytes,
            )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                unsealed, _files = _version_and_files()
                unsealed.skill_id = project_skill_id
                unsealed.version_number = 2
                unsealed.created_by_user_id = str(user_id)
                session.add(unsealed)

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                mismatched, files = _version_and_files()
                mismatched.skill_id = project_skill_id
                mismatched.version_number = 2
                mismatched.file_count += 1
                mismatched.created_by_user_id = str(user_id)
                session.add(mismatched)
                await session.flush()
                await session.execute(
                    text("SELECT set_config('deerflow.asset_version_assembly', :version_id, true)"),
                    {"version_id": str(mismatched.id)},
                )
                session.add_all(files)
                await session.flush()
                mismatched.files_sealed = True

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(
                    text("SELECT set_config('deerflow.asset_version_assembly', :version_id, true)"),
                    {"version_id": str(project_version.id)},
                )
                session.add(
                    SkillVersionFileRow(
                        skill_version_id=project_version.id,
                        path="reused-guc.txt",
                        media_type="text/plain",
                        size_bytes=1,
                        sha256=hashlib.sha256(b"x").hexdigest(),
                        content=b"x",
                    )
                )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(
                    text("SELECT set_config('deerflow.asset_version_assembly', :version_id, true)"),
                    {"version_id": str(project_version.id)},
                )
                await session.execute(delete(SkillVersionFileRow).where(SkillVersionFileRow.skill_version_id == project_version.id))

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(update(SkillVersionRow).where(SkillVersionRow.id == project_version.id).values(files_sealed=False))

        system_version, system_files = _version_and_files()
        system_version.skill_id = system_skill_id
        system_version.created_by_user_id = str(user_id)
        governance = SystemAssetGovernanceContext(
            user_id=user_id,
            request_id="pg-system-skill",
        )
        async with factory() as session, session.begin():
            await SkillRepository(session).create_system_version(
                governance,
                system_skill_id,
                system_version,
                system_files,
            )

        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(text("SELECT set_config('deerflow.system_asset_upgrade', 'on', true)"))
                await session.execute(update(SkillVersionRow).where(SkillVersionRow.id == system_version.id).values(payload_checksum="0" * 64))
    finally:
        await engine.dispose()
