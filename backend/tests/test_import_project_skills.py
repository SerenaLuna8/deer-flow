from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import scripts.import_project_skills as importer
from app.audit.service import AuditService
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext, resolve_project_context
from app.projects.models import ProjectRole
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.reliability.owner_refs import AuditHmacKeyring
from app.shared_assets.audit import DurableSharedAssetGovernanceEventSink
from app.shared_assets.bootstrap import bootstrap_system_assets
from app.shared_assets.errors import AssetStorageQuotaExceeded
from app.shared_assets.models import AssetScope
from app.shared_assets.skill_service import ProjectSkillArchiveImport, SkillService
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.shared_assets import SkillRow
from deerflow.persistence.user.model import UserRow
from scripts.import_project_skills import (
    ProjectSkillImportError,
    ProjectSkillImportSummary,
    import_project_skills,
    load_project_skill_sources,
)


def _write_skill(root: Path, directory: str, *, name: str, body: str = "Use this skill.\n") -> None:
    skill_root = root / directory
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Project Skill importer test\n---\n\n{body}",
        encoding="utf-8",
    )
    scripts = skill_root / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text("print('ok')\n", encoding="utf-8")


def _rewrite_skill_manifest(
    root: Path,
    directory: str,
    *,
    name: str,
    description: str,
    body: str = "Updated.\n",
) -> None:
    (root / directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
        encoding="utf-8",
    )


def _editor_context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="req-project-skill-import-source",
    )


@pytest.mark.asyncio
async def test_repository_public_skills_are_complete_regular_archives() -> None:
    source_root = Path(__file__).resolve().parents[2] / "skills" / "public"
    sources = load_project_skill_sources(source_root)
    service = SkillService(lambda: None)  # type: ignore[arg-type,return-value]
    previews = [await service.preview_archive(_editor_context(), source.files) for source in sources]

    assert len(sources) == 21
    assert len({str(preview.frontmatter["name"]).casefold() for preview in previews}) == len(previews)
    assert all(any(file.path == "SKILL.md" for file in preview.files) for preview in previews)


@pytest.mark.parametrize("invalid_tree", ["root-file", "missing-manifest", "empty-directory"])
def test_source_tree_rejects_incomplete_layout(tmp_path: Path, invalid_tree: str) -> None:
    if invalid_tree == "root-file":
        (tmp_path / "README.md").write_text("not a skill", encoding="utf-8")
    elif invalid_tree == "missing-manifest":
        (tmp_path / "missing").mkdir()
        (tmp_path / "missing" / "notes.md").write_text("missing SKILL.md", encoding="utf-8")
    else:
        _write_skill(tmp_path, "valid", name="valid")
        (tmp_path / "valid" / "empty").mkdir()

    with pytest.raises(ProjectSkillImportError, match="source tree is invalid"):
        load_project_skill_sources(tmp_path)


def test_source_tree_rejects_symlinks(tmp_path: Path) -> None:
    _write_skill(tmp_path, "valid", name="valid")
    outside = tmp_path.parent / f"outside-{uuid.uuid4().hex}.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "valid" / "outside.txt").symlink_to(outside)

    with pytest.raises(ProjectSkillImportError, match="source tree is invalid"):
        load_project_skill_sources(tmp_path)


@pytest.mark.parametrize(
    ("limit_name", "limit_value"),
    [
        ("MAX_PROJECT_SKILL_BATCH_ITEMS", 1),
        ("MAX_PROJECT_SKILL_BATCH_FILES", 2),
        ("MAX_PROJECT_SKILL_BATCH_BYTES", "first-skill-bytes"),
    ],
)
def test_source_loader_enforces_batch_bounds_before_reading_next_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit_value: int | str,
) -> None:
    _write_skill(tmp_path, "01-first", name="loader-first")
    _write_skill(tmp_path, "02-second", name="loader-second")
    first_skill_bytes = sum(path.stat().st_size for path in (tmp_path / "01-first").rglob("*") if path.is_file())
    selected_limit = first_skill_bytes if limit_value == "first-skill-bytes" else limit_value
    monkeypatch.setattr(importer, limit_name, selected_limit)
    reads: list[Path] = []
    original_read = importer._read_regular_file

    def recording_read(path: Path, *, expected_size: int) -> bytes:
        reads.append(path)
        return original_read(path, expected_size=expected_size)

    monkeypatch.setattr(importer, "_read_regular_file", recording_read)

    with pytest.raises(ProjectSkillImportError, match="source tree is invalid"):
        load_project_skill_sources(tmp_path)

    assert reads
    assert all("01-first" in path.parts for path in reads)


def test_cli_success_output_is_summary_only(monkeypatch, capsys) -> None:
    email = "private-user@example.com"
    project_slug = "private-project"
    monkeypatch.setenv("DATABASE_URL", "postgresql://private.invalid/private_database")

    def fake_run(coroutine):
        coroutine.close()
        return ProjectSkillImportSummary(
            mode="dry-run",
            discovered_count=2,
            planned_create_count=2,
            planned_replace_count=0,
            unchanged_count=0,
            created_count=0,
            replaced_count=0,
        )

    monkeypatch.setattr(importer.asyncio, "run", fake_run)
    assert (
        importer.main(
            [
                "--email",
                email,
                "--project-slug",
                project_slug,
                "--dry-run",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ('{"created_count":0,"discovered_count":2,"mode":"dry-run","planned_create_count":2,"planned_replace_count":0,"replaced_count":0,"unchanged_count":0}\n')
    assert email not in captured.out
    assert project_slug not in captured.out


def test_cli_execute_uses_only_compatibility_quota_shape(monkeypatch, capsys) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setenv("DATABASE_URL", "postgresql://private.invalid/private_database")

    async def fake_import_project_skills(
        database_url: str,
        **kwargs: object,
    ) -> ProjectSkillImportSummary:
        observed["database_url"] = database_url
        observed.update(kwargs)
        return ProjectSkillImportSummary(
            mode="execute",
            discovered_count=1,
            planned_create_count=1,
            planned_replace_count=0,
            unchanged_count=0,
            created_count=1,
            replaced_count=0,
        )

    monkeypatch.setattr(importer, "import_project_skills", fake_import_project_skills)

    assert (
        importer.main(
            [
                "--email",
                "operator@example.com",
                "--project-slug",
                "private-project",
                "--execute",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert type(observed["quota_config"]) is QuotaConfig


@pytest.mark.asyncio
async def test_import_project_skills_maps_storage_quota_exceeded_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_import(*args: object, **kwargs: object) -> ProjectSkillImportSummary:
        raise AssetStorageQuotaExceeded("private-request-id")

    monkeypatch.setattr(importer, "_run_import", fail_import)

    with pytest.raises(ProjectSkillImportError, match="project Skill storage quota exceeded") as exc_info:
        await import_project_skills(
            "postgresql://private.invalid/private_database",
            source_root="/private/source",
            user_email="private-user@example.com",
            project_slug="private-project",
            execute=True,
            replace=False,
            quota_config=QuotaConfig(default_storage_bytes_limit=1),
        )

    assert "private-request-id" not in str(exc_info.value)
    assert "private-user@example.com" not in str(exc_info.value)


async def _seed_project_actor(database_url: str, *, role: str) -> tuple[str, str, uuid.UUID]:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    email = f"skill-import-{role}-{user_id}@example.com"
    slug = f"skill-import-{role}-{str(project_id)[:8]}"
    now = datetime.now(UTC)
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'user',:now,false,0)"""
                ),
                {"id": str(user_id), "email": email, "now": now},
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                    (id,slug,display_name,created_by_user_id,created_at,updated_at)
                    VALUES (:id,:slug,:display_name,:user_id,:now,:now)"""
                ),
                {
                    "id": project_id,
                    "slug": slug,
                    "display_name": "Skill import test",
                    "user_id": str(user_id),
                    "now": now,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                    (id,project_id,user_id,role,status,version)
                    VALUES (:id,:project_id,:user_id,:role,'active',1)"""
                ),
                {
                    "id": uuid.uuid4(),
                    "project_id": project_id,
                    "user_id": str(user_id),
                    "role": role,
                },
            )
    finally:
        await engine.dispose()
    return email, slug, project_id


async def _resolve_seeded_actor(
    database_url: str,
    *,
    email: str,
    project_slug: str,
) -> ProjectContext:
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user_id = await session.scalar(select(UserRow.id).where(UserRow.email == email))
        assert user_id is not None
        async with factory() as session:
            return await resolve_project_context(
                session,
                uuid.UUID(user_id),
                project_slug,
                "req-atomic-project-skill-import",
            )
    finally:
        await engine.dispose()


class _InjectedGovernanceFailure:
    def __init__(
        self,
        delegate: DurableSharedAssetGovernanceEventSink,
        *,
        action: str,
        occurrence: int,
    ) -> None:
        self._delegate = delegate
        self._action = action
        self._occurrence = occurrence
        self._seen = 0

    async def append_project(self, session, **kwargs) -> None:
        await self._delegate.append_project(session, **kwargs)
        if kwargs.get("action") == self._action:
            self._seen += 1
            if self._seen == self._occurrence:
                raise RuntimeError("injected second project Skill failure")


def _batch_sources(source_root: Path) -> tuple[ProjectSkillArchiveImport, ...]:
    return tuple(ProjectSkillArchiveImport(files=source.files) for source in load_project_skill_sources(source_root))


def _quota(
    factory,
    keyring: AuditHmacKeyring,
) -> ProjectQuotaEnforcer:
    return ProjectQuotaEnforcer(
        QuotaService(
            factory,
            QuotaConfig(),
            source_ref_hasher=keyring,
        )
    )


@pytest.mark.asyncio
async def test_atomic_batch_rolls_back_first_skill_when_second_skill_fails(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    email, project_slug, project_id = await _seed_project_actor(
        migrated_postgres_database_url,
        role="editor",
    )
    _write_skill(tmp_path, "01-first", name="atomic-first")
    _write_skill(tmp_path, "02-second", name="atomic-second")
    actor = await _resolve_seeded_actor(
        migrated_postgres_database_url,
        email=email,
        project_slug=project_slug,
    )
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    keyring = AuditHmacKeyring.from_environment()
    durable = DurableSharedAssetGovernanceEventSink(AuditService(factory, keyring))
    service = SkillService(
        factory,
        governance_sink=_InjectedGovernanceFailure(
            durable,
            action="skill.create",
            occurrence=2,
        ),
        quota=_quota(factory, keyring),
    )
    try:
        with pytest.raises(RuntimeError, match="injected second project Skill failure"):
            await service.import_project_archives_atomic(
                actor,
                _batch_sources(tmp_path),
                execute=True,
                replace=False,
            )

        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        """SELECT count(*) FROM skills
                        WHERE scope='project' AND project_id=:project_id"""
                    ),
                    {"project_id": project_id},
                )
            ) == 0
            assert await connection.scalar(text("SELECT count(*) FROM skill_versions")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM audit_logs")) == 0
            assert (
                await connection.scalar(
                    text(
                        """SELECT count(*) FROM project_usage_ledger
                           WHERE project_id=:project_id
                             AND dimension='storage_bytes'"""
                    ),
                    {"project_id": project_id},
                )
                == 0
            )
            assert (
                await connection.scalar(
                    text(
                        """SELECT coalesce(sum(reserved), 0)
                           FROM project_usage_counters
                           WHERE project_id=:project_id
                             AND dimension='storage_bytes'"""
                    ),
                    {"project_id": project_id},
                )
                == 0
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_atomic_batch_replace_rolls_back_and_preserves_unchanged_skill(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    email, project_slug, project_id = await _seed_project_actor(
        migrated_postgres_database_url,
        role="editor",
    )
    _write_skill(tmp_path, "01-alpha", name="atomic-alpha")
    _write_skill(tmp_path, "02-beta", name="atomic-beta")
    actor = await _resolve_seeded_actor(
        migrated_postgres_database_url,
        email=email,
        project_slug=project_slug,
    )
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    keyring = AuditHmacKeyring.from_environment()
    durable = DurableSharedAssetGovernanceEventSink(AuditService(factory, keyring))
    quota = _quota(factory, keyring)
    try:
        created = await SkillService(
            factory,
            governance_sink=durable,
            quota=quota,
        ).import_project_archives_atomic(
            actor,
            _batch_sources(tmp_path),
            execute=True,
            replace=False,
        )
        assert created.created_count == 2

        async with factory() as session:
            original_rows = (
                await session.execute(
                    select(SkillRow.slug, SkillRow.current_published_version_id)
                    .where(
                        SkillRow.scope == "project",
                        SkillRow.project_id == project_id,
                    )
                    .order_by(SkillRow.slug)
                )
            ).all()
        original_published = dict(original_rows)

        _rewrite_skill_manifest(
            tmp_path,
            "01-alpha",
            name="atomic-alpha",
            description="Updated alpha",
        )
        _rewrite_skill_manifest(
            tmp_path,
            "02-beta",
            name="atomic-beta",
            description="Updated beta",
        )
        failing_service = SkillService(
            factory,
            governance_sink=_InjectedGovernanceFailure(
                durable,
                action="skill.version.create",
                occurrence=2,
            ),
            quota=quota,
        )
        with pytest.raises(RuntimeError, match="injected second project Skill failure"):
            await failing_service.import_project_archives_atomic(
                actor,
                _batch_sources(tmp_path),
                execute=True,
                replace=True,
            )

        async with factory() as session:
            rolled_back_rows = (
                await session.execute(
                    select(SkillRow.slug, SkillRow.current_published_version_id)
                    .where(
                        SkillRow.scope == "project",
                        SkillRow.project_id == project_id,
                    )
                    .order_by(SkillRow.slug)
                )
            ).all()
            assert dict(rolled_back_rows) == original_published
            assert await session.scalar(text("SELECT count(*) FROM skill_versions")) == 2
            assert await session.scalar(text("SELECT count(*) FROM audit_logs")) == 6

        _rewrite_skill_manifest(
            tmp_path,
            "02-beta",
            name="atomic-beta",
            description="Project Skill importer test",
            body="Use this skill.\n",
        )
        completed = await SkillService(
            factory,
            governance_sink=durable,
            quota=quota,
        ).import_project_archives_atomic(
            actor,
            _batch_sources(tmp_path),
            execute=True,
            replace=True,
        )
        assert completed.created_count == 0
        assert completed.replaced_count == 1
        assert completed.unchanged_count == 1
        async with factory() as session:
            assert await session.scalar(text("SELECT count(*) FROM skill_versions")) == 3
            assert await session.scalar(text("SELECT count(*) FROM audit_logs")) == 8
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_import_project_skills_dry_run_create_replace_and_audit(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    email, project_slug, project_id = await _seed_project_actor(migrated_postgres_database_url, role="editor")
    _write_skill(tmp_path, "directory-name-can-differ", name="imported-skill")

    dry_run = await import_project_skills(
        migrated_postgres_database_url,
        source_root=tmp_path,
        user_email=email,
        project_slug=project_slug,
        execute=False,
        replace=False,
        quota_config=None,
    )
    assert dry_run.mode == "dry-run"
    assert dry_run.discovered_count == 1
    assert dry_run.planned_create_count == 1
    assert dry_run.created_count == 0

    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM skills")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM audit_logs")) == 0

        created = await import_project_skills(
            migrated_postgres_database_url,
            source_root=tmp_path,
            user_email=email,
            project_slug=project_slug,
            execute=True,
            replace=False,
            quota_config=QuotaConfig(),
        )
        assert created.mode == "execute"
        assert created.created_count == 1
        assert created.replaced_count == 0

        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """SELECT id,scope,project_id,status,current_published_version_id,version
                        FROM skills WHERE slug='imported-skill'"""
                    )
                )
            ).one()
            first_version_id = row.current_published_version_id
            assert row.scope == AssetScope.PROJECT.value
            assert row.project_id == project_id
            assert row.status == "suspended"
            assert row.version == 3
            assert await connection.scalar(text("SELECT count(*) FROM skill_versions")) == 1
            assert await connection.scalar(text("SELECT count(*) FROM audit_logs")) == 3

        with pytest.raises(ProjectSkillImportError, match="slug conflict"):
            await import_project_skills(
                migrated_postgres_database_url,
                source_root=tmp_path,
                user_email=email,
                project_slug=project_slug,
                execute=True,
                replace=False,
                quota_config=QuotaConfig(),
            )

        (tmp_path / "directory-name-can-differ" / "SKILL.md").write_text(
            "---\nname: imported-skill\ndescription: Updated importer test\n---\n\nUpdated.\n",
            encoding="utf-8",
        )
        replaced = await import_project_skills(
            migrated_postgres_database_url,
            source_root=tmp_path,
            user_email=email,
            project_slug=project_slug,
            execute=True,
            replace=True,
            quota_config=QuotaConfig(),
        )
        assert replaced.created_count == 0
        assert replaced.replaced_count == 1

        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """SELECT current_published_version_id,version
                        FROM skills WHERE slug='imported-skill'"""
                    )
                )
            ).one()
            assert row.current_published_version_id != first_version_id
            assert row.version == 5
            assert await connection.scalar(text("SELECT count(*) FROM skill_versions")) == 2
            assert await connection.scalar(text("SELECT count(*) FROM audit_logs")) == 5
            assert (
                await connection.scalar(
                    text(
                        """SELECT supersedes_version_id FROM skill_versions
                        WHERE id=:version_id"""
                    ),
                    {"version_id": row.current_published_version_id},
                )
            ) == first_version_id

        unchanged = await import_project_skills(
            migrated_postgres_database_url,
            source_root=tmp_path,
            user_email=email,
            project_slug=project_slug,
            execute=True,
            replace=True,
            quota_config=QuotaConfig(),
        )
        assert unchanged.unchanged_count == 1
        assert unchanged.replaced_count == 0
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM skill_versions")) == 2
            assert await connection.scalar(text("SELECT count(*) FROM audit_logs")) == 5
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_import_project_skills_requires_edit_capability(
    migrated_postgres_database_url: str,
    tmp_path: Path,
) -> None:
    email, project_slug, _ = await _seed_project_actor(migrated_postgres_database_url, role="viewer")
    _write_skill(tmp_path, "viewer-skill", name="viewer-skill")

    with pytest.raises(ProjectSkillImportError, match="actor is not allowed"):
        await import_project_skills(
            migrated_postgres_database_url,
            source_root=tmp_path,
            user_email=email,
            project_slug=project_slug,
            execute=False,
            replace=False,
            quota_config=None,
        )

    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM skills")) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_repository_public_skills_import_into_fresh_setup_project(
    migrated_postgres_database_url: str,
) -> None:
    source_root = Path(__file__).resolve().parents[2] / "skills" / "public"
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await bootstrap_system_assets(factory)
        async with engine.connect() as connection:
            system_skill_count = await connection.scalar(text("SELECT count(*) FROM skills WHERE scope='system'"))
        email, project_slug, project_id = await _seed_project_actor(
            migrated_postgres_database_url,
            role="editor",
        )

        summary = await import_project_skills(
            migrated_postgres_database_url,
            source_root=source_root,
            user_email=email,
            project_slug=project_slug,
            execute=True,
            replace=False,
            quota_config=QuotaConfig(),
        )

        assert summary.discovered_count == 21
        assert summary.created_count == 21
        assert summary.replaced_count == 0
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        """SELECT count(*) FROM skills
                        WHERE scope='project' AND project_id=:project_id"""
                    ),
                    {"project_id": project_id},
                )
            ) == 21
            assert (
                await connection.scalar(
                    text(
                        """SELECT count(*) FROM skill_versions v
                        JOIN skills s ON s.id=v.skill_id
                        WHERE s.scope='project' AND s.project_id=:project_id
                        AND v.workflow_status='published'"""
                    ),
                    {"project_id": project_id},
                )
            ) == 21
            assert await connection.scalar(text("SELECT count(*) FROM skills WHERE scope='system'")) == system_skill_count
            assert await connection.scalar(text("SELECT count(*) FROM audit_logs")) == 63
    finally:
        await engine.dispose()
