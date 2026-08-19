from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.audit.service import AuditService
from app.private_work.context import PrivateWorkContext
from app.private_work.snapshot_repository import (
    RunAssetSnapshot,
    RunSkillCredentialSnapshot,
    RunSnapshotRepository,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.owner_refs import AuditHmacKeyring
from app.shared_assets import skill_service as skill_service_module
from app.shared_assets.audit import DurableSharedAssetGovernanceEventSink
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.credential_service import CredentialReplacementView, CredentialService
from app.shared_assets.errors import (
    SkillCredentialBindingInvalid,
    SkillCredentialBindingsIncomplete,
    SkillCredentialSelectionStale,
)
from app.shared_assets.keyring import CredentialKeyring
from app.shared_assets.models import AssetKind, AssetScope, SkillArchiveFile, WorkflowStatus
from app.shared_assets.skill_credential_policy import SkillCredentialBindingInput
from app.shared_assets.skill_credential_service import SkillCredentialBindingService
from app.shared_assets.skill_service import SkillVersionView
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialRow,
    CredentialVersionRow,
    ProjectSkillCredentialBindingRow,
    ProjectSkillCredentialConfigRow,
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.persistence.user.model import UserRow


@dataclass(frozen=True)
class _CredentialSeed:
    credential_id: uuid.UUID
    version_id: uuid.UUID


@dataclass(frozen=True)
class _Seed:
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    actor: ProjectContext
    skill_id: uuid.UUID
    old_version_id: uuid.UUID
    old_checksum: str
    draft_version_id: uuid.UUID
    draft_checksum: str
    old_credential: _CredentialSeed
    new_credential: _CredentialSeed


def _package(body: str) -> tuple[SkillArchiveFile, ...]:
    return (
        SkillArchiveFile(
            "SKILL.md",
            (f"---\nname: postgres-secret-skill\ndescription: PostgreSQL atomic Skill\nrequired-secrets:\n  - name: API_KEY\n    optional: false\n---\n\n{body}\n").encode(),
            "text/markdown",
        ),
    )


def _version_rows(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    version_number: int,
    body: str,
    user_id: uuid.UUID,
) -> tuple[SkillVersionRow, tuple[SkillVersionFileRow, ...]]:
    files = _package(body)
    preview = skill_service_module._analyze_skill_files(  # noqa: SLF001
        files,
        "postgres-skill-publish-seed",
    )
    row = SkillVersionRow(
        id=version_id,
        skill_id=skill_id,
        version_number=version_number,
        workflow_status="draft",
        description=preview.description,
        frontmatter=dict(preview.frontmatter),
        compatibility=preview.compatibility,
        secret_requirements=[{"name": item.name, "optional": item.optional} for item in preview.secret_requirements],
        scan_decision=preview.scan_decision,
        scan_summary=dict(preview.scan_summary),
        supersedes_version_id=None,
        payload_checksum=preview.checksum,
        created_by_user_id=str(user_id),
    )
    child_rows = tuple(
        SkillVersionFileRow(
            skill_version_id=version_id,
            path=file.path,
            media_type=file.media_type,
            size_bytes=len(file.content),
            sha256=view.sha256,
            content=file.content,
        )
        for file, view in zip(files, preview.file_views, strict=True)
    )
    return row, child_rows


async def _add_credential(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    env: tuple[str, ...] = ("API_KEY",),
    envelope_active: bool = True,
) -> _CredentialSeed:
    credential_id = uuid.uuid4()
    version_id = uuid.uuid4()
    credential = CredentialRow(
        id=credential_id,
        scope="project",
        project_id=project_id,
        name=name,
        display_name=name.replace("-", " ").title(),
        credential_type="skill_auth",
        status="active",
        is_delete=False,
        current_version_id=None,
        version=1,
        created_by_user_id=str(user_id),
    )
    session.add(credential)
    await session.flush()
    session.add(
        CredentialVersionRow(
            id=version_id,
            credential_id=credential_id,
            version_number=1,
            status="active",
            payload_schema_version=1,
            payload_schema={"env": list(env)},
            created_by_user_id=str(user_id),
        )
    )
    await session.flush()
    session.add(
        CredentialEnvelopeRow(
            credential_version_id=version_id,
            envelope_generation=1,
            key_id="test-key",
            nonce=b"n" * 12,
            ciphertext=b"c" * 32,
            is_active=envelope_active,
            created_by_user_id=str(user_id),
        )
    )
    credential.current_version_id = version_id
    await session.flush()
    return _CredentialSeed(credential_id, version_id)


async def _seed(
    database_url: str,
    *,
    new_credential_env: tuple[str, ...] = ("API_KEY",),
    new_credential_envelope_active: bool = True,
) -> _Seed:
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    skill_id = uuid.uuid4()
    old_version_id = uuid.uuid4()
    draft_version_id = uuid.uuid4()
    actor = ProjectContext(
        user_id=user_id,
        project_id=project_id,
        membership_id=membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="req-postgres-atomic-skill-publish",
    )
    old, old_files = _version_rows(
        skill_id,
        old_version_id,
        version_number=1,
        body="Use the old binding.",
        user_id=user_id,
    )
    draft, draft_files = _version_rows(
        skill_id,
        draft_version_id,
        version_number=2,
        body="Use the new binding.",
        user_id=user_id,
    )
    draft.supersedes_version_id = old_version_id

    async with factory() as session, session.begin():
        session.add(
            UserRow(
                id=str(user_id),
                email=f"{user_id}@example.test",
                username=f"u{user_id.hex[:8]}",
                password_hash="test-password-hash",
                system_role="user",
                needs_setup=False,
                token_version=0,
            )
        )
        await session.flush()
        session.add(
            ProjectRow(
                id=project_id,
                slug=f"p-{project_id.hex[:12]}",
                display_name="Skill publish test",
                created_by_user_id=str(user_id),
            )
        )
        await session.flush()
        session.add(
            ProjectMembershipRow(
                id=membership_id,
                project_id=project_id,
                user_id=str(user_id),
                role="admin",
                status="active",
                version=1,
            )
        )
        skill = SkillRow(
            id=skill_id,
            scope="project",
            project_id=project_id,
            slug="postgres-secret-skill",
            display_name="PostgreSQL secret Skill",
            status="active",
            current_published_version_id=None,
            version=8,
            created_by_user_id=str(user_id),
        )
        session.add(skill)
        await session.flush()
        session.add(old)
        await session.flush()
        session.add_all(old_files)
        await session.flush()
        old.workflow_status = "published"
        skill.current_published_version_id = old_version_id
        await session.flush()
        session.add(draft)
        await session.flush()
        session.add_all(draft_files)
        await session.flush()

        old_credential = await _add_credential(
            session,
            project_id=project_id,
            user_id=user_id,
            name=f"old-{uuid.uuid4().hex[:8]}",
        )
        new_credential = await _add_credential(
            session,
            project_id=project_id,
            user_id=user_id,
            name=f"new-{uuid.uuid4().hex[:8]}",
            env=new_credential_env,
            envelope_active=new_credential_envelope_active,
        )
        config = ProjectSkillCredentialConfigRow(
            project_id=project_id,
            skill_id=skill_id,
            skill_version_id=old_version_id,
            revision=1,
            created_by_user_id=str(user_id),
            updated_by_user_id=str(user_id),
        )
        session.add(config)
        await session.flush()
        session.add(
            ProjectSkillCredentialBindingRow(
                project_id=project_id,
                skill_id=skill_id,
                skill_version_id=old_version_id,
                secret_name="API_KEY",
                credential_id=old_credential.credential_id,
                credential_version_id=old_credential.version_id,
                config_revision=1,
                created_by_user_id=str(user_id),
            )
        )

    return _Seed(
        engine=engine,
        factory=factory,
        actor=actor,
        skill_id=skill_id,
        old_version_id=old_version_id,
        old_checksum=old.payload_checksum,
        draft_version_id=draft_version_id,
        draft_checksum=draft.payload_checksum,
        old_credential=old_credential,
        new_credential=new_credential,
    )


async def _published_state(seed: _Seed):
    async with seed.factory() as session:
        skill = await session.get(SkillRow, seed.skill_id)
        version = await session.get(SkillVersionRow, seed.draft_version_id)
        config = await session.get(
            ProjectSkillCredentialConfigRow,
            (
                seed.actor.project_id,
                seed.skill_id,
                seed.draft_version_id,
            ),
        )
        bindings = tuple(
            (
                await session.execute(
                    select(ProjectSkillCredentialBindingRow).where(
                        ProjectSkillCredentialBindingRow.project_id == seed.actor.project_id,
                        ProjectSkillCredentialBindingRow.skill_id == seed.skill_id,
                        ProjectSkillCredentialBindingRow.skill_version_id == seed.draft_version_id,
                        ProjectSkillCredentialBindingRow.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        return skill, version, config, bindings


async def _run_skill_credentials(
    seed: _Seed,
    *,
    version_id: uuid.UUID,
    payload_checksum: str,
) -> tuple[RunSkillCredentialSnapshot, ...]:
    context = PrivateWorkContext.from_project(seed.actor)
    snapshot = RunAssetSnapshot(
        asset_kind=AssetKind.SKILL.value,
        dependency_order=0,
        asset_scope=AssetScope.PROJECT.value,
        asset_id=seed.skill_id,
        version_id=version_id,
        payload_checksum=payload_checksum,
        catalog_generation=0,
    )
    async with seed.factory() as session, session.begin():
        return await RunSnapshotRepository(seed.factory).current_skill_credentials_in_session(
            session,
            context,
            (snapshot,),
        )


def _durable_audit_sink() -> DurableSharedAssetGovernanceEventSink:
    return DurableSharedAssetGovernanceEventSink(
        AuditService(
            None,
            AuditHmacKeyring(
                active_key_id="skill-publish-test",
                _keys={"skill-publish-test": b"a" * 32},
            ),
        )
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_publish_atomically_switches_exact_binding_closure(
    migrated_postgres_database_url: str,
) -> None:
    seed = await _seed(migrated_postgres_database_url)
    try:
        old_before = await _run_skill_credentials(
            seed,
            version_id=seed.old_version_id,
            payload_checksum=seed.old_checksum,
        )
        assert old_before[0].credential_version_id == seed.old_credential.version_id

        published = await skill_service_module.SkillService(
            seed.factory,
            governance_sink=_durable_audit_sink(),
        ).publish(
            seed.actor,
            seed.skill_id,
            seed.draft_version_id,
            expected_asset_version=8,
            expected_payload_checksum=seed.draft_checksum,
            expected_binding_revision=0,
            credential_bindings=(
                SkillCredentialBindingInput(
                    "API_KEY",
                    seed.new_credential.version_id,
                ),
            ),
        )

        assert published.workflow_status is WorkflowStatus.PUBLISHED
        skill, version, config, bindings = await _published_state(seed)
        assert skill is not None
        assert version is not None
        assert skill.current_published_version_id == seed.draft_version_id
        assert version.workflow_status == "published"
        assert config is not None and config.revision == 1
        assert len(bindings) == 1
        assert bindings[0].credential_version_id == seed.new_credential.version_id

        old_after = await _run_skill_credentials(
            seed,
            version_id=seed.old_version_id,
            payload_checksum=seed.old_checksum,
        )
        new_after = await _run_skill_credentials(
            seed,
            version_id=seed.draft_version_id,
            payload_checksum=seed.draft_checksum,
        )
        assert old_after[0].credential_version_id == seed.old_credential.version_id
        assert new_after[0].credential_version_id == seed.new_credential.version_id

        async with seed.factory() as session:
            audit_rows = tuple((await session.execute(select(AuditLogRow).order_by(AuditLogRow.occurred_at, AuditLogRow.id))).scalars().all())
        assert [row.metadata_json["operation"] for row in audit_rows] == [
            "skill.credential_bindings.configure",
            "skill.publish",
        ]
        assert all(set(row.metadata_json) == {"asset_kind", "operation"} for row in audit_rows)
        audit_payload = repr([row.metadata_json for row in audit_rows])
        assert str(seed.new_credential.credential_id) not in audit_payload
        assert str(seed.new_credential.version_id) not in audit_payload
        assert "API_KEY" not in audit_payload

        with pytest.raises(SkillCredentialBindingsIncomplete):
            await SkillCredentialBindingService(seed.factory).replace(
                seed.actor,
                seed.skill_id,
                (),
                expected_revision=1,
            )
        _skill, _version, config_after, bindings_after = await _published_state(seed)
        assert config_after is not None and config_after.revision == 1
        assert len(bindings_after) == 1
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_admin_project_override_validates_existing_bindings_without_membership(
    migrated_postgres_database_url: str,
) -> None:
    seed = await _seed(migrated_postgres_database_url)
    admin_override = SystemAssetGovernanceContext(
        user_id=seed.actor.user_id,
        project_id=seed.actor.project_id,
        request_id="req-postgres-admin-override-skill-publish",
    )
    try:
        async with seed.factory() as session, session.begin():
            await session.execute(
                delete(ProjectMembershipRow).where(
                    ProjectMembershipRow.id == seed.actor.membership_id,
                )
            )

        service = skill_service_module.SkillService(seed.factory)
        with pytest.raises(SkillCredentialBindingsIncomplete):
            await service.publish(
                admin_override,
                seed.skill_id,
                seed.draft_version_id,
                expected_asset_version=8,
            )

        skill, version, config, bindings = await _published_state(seed)
        assert skill is not None
        assert version is not None
        assert skill.current_published_version_id == seed.old_version_id
        assert skill.version == 8
        assert version.workflow_status == "draft"
        assert config is None
        assert bindings == ()

        async with seed.factory() as session, session.begin():
            session.add(
                ProjectSkillCredentialConfigRow(
                    project_id=seed.actor.project_id,
                    skill_id=seed.skill_id,
                    skill_version_id=seed.draft_version_id,
                    revision=1,
                    created_by_user_id=str(seed.actor.user_id),
                    updated_by_user_id=str(seed.actor.user_id),
                )
            )
            await session.flush()
            session.add(
                ProjectSkillCredentialBindingRow(
                    project_id=seed.actor.project_id,
                    skill_id=seed.skill_id,
                    skill_version_id=seed.draft_version_id,
                    secret_name="API_KEY",
                    credential_id=seed.new_credential.credential_id,
                    credential_version_id=seed.new_credential.version_id,
                    config_revision=1,
                    created_by_user_id=str(seed.actor.user_id),
                )
            )

        published = await service.publish(
            admin_override,
            seed.skill_id,
            seed.draft_version_id,
            expected_asset_version=8,
        )

        assert published.workflow_status is WorkflowStatus.PUBLISHED
        skill, version, config, bindings = await _published_state(seed)
        assert skill is not None
        assert version is not None
        assert skill.current_published_version_id == seed.draft_version_id
        assert skill.version == 9
        assert version.workflow_status == "published"
        assert config is not None and config.revision == 1
        assert len(bindings) == 1
        assert bindings[0].credential_version_id == seed.new_credential.version_id
    finally:
        await seed.engine.dispose()


class _FailingPublishAudit:
    def __init__(self) -> None:
        self._delegate = _durable_audit_sink()

    async def append_project(self, session, **kwargs) -> None:
        await self._delegate.append_project(session, **kwargs)
        if kwargs["action"] == "skill.publish":
            raise RuntimeError("publish audit failed")


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["missing_required", "schema_mismatch", "envelope_missing", "audit"],
)
async def test_postgres_publish_failure_rolls_back_pointer_version_and_binding(
    migrated_postgres_database_url: str,
    failure: str,
) -> None:
    seed = await _seed(
        migrated_postgres_database_url,
        new_credential_env=(("OTHER_API_KEY",) if failure == "schema_mismatch" else ("API_KEY",)),
        new_credential_envelope_active=failure != "envelope_missing",
    )
    try:
        service = skill_service_module.SkillService(
            seed.factory,
            governance_sink=(_FailingPublishAudit() if failure == "audit" else None),
        )
        expected_error = {
            "audit": RuntimeError,
            "envelope_missing": SkillCredentialSelectionStale,
            "missing_required": SkillCredentialBindingsIncomplete,
            "schema_mismatch": SkillCredentialBindingInvalid,
        }[failure]
        selected = (
            (
                SkillCredentialBindingInput(
                    "API_KEY",
                    seed.new_credential.version_id,
                ),
            )
            if failure != "missing_required"
            else ()
        )
        with pytest.raises(expected_error):
            await service.publish(
                seed.actor,
                seed.skill_id,
                seed.draft_version_id,
                expected_asset_version=8,
                expected_payload_checksum=seed.draft_checksum,
                expected_binding_revision=0,
                credential_bindings=selected,
            )

        skill, version, config, bindings = await _published_state(seed)
        assert skill is not None
        assert version is not None
        assert skill.current_published_version_id == seed.old_version_id
        assert skill.version == 8
        assert version.workflow_status == "draft"
        assert config is None
        assert bindings == ()
        if failure == "audit":
            async with seed.factory() as session:
                assert await session.scalar(select(AuditLogRow.id).limit(1)) is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_publish_and_credential_rotation_never_deadlock_or_partially_publish(
    migrated_postgres_database_url: str,
) -> None:
    seed = await _seed(migrated_postgres_database_url)
    try:
        publish = skill_service_module.SkillService(seed.factory).publish(
            seed.actor,
            seed.skill_id,
            seed.draft_version_id,
            expected_asset_version=8,
            expected_payload_checksum=seed.draft_checksum,
            expected_binding_revision=0,
            credential_bindings=(
                SkillCredentialBindingInput(
                    "API_KEY",
                    seed.new_credential.version_id,
                ),
            ),
        )
        rotate = CredentialService(
            seed.factory,
            keyring=CredentialKeyring("test-key", {"test-key": b"k" * 32}),
        ).replace(
            seed.actor,
            seed.new_credential.credential_id,
            {"env": {"API_KEY": "rotated-test-value"}},
            expected_credential_version=1,
        )

        publish_result, rotate_result = await asyncio.wait_for(
            asyncio.gather(publish, rotate, return_exceptions=True),
            timeout=15,
        )

        assert isinstance(rotate_result, CredentialReplacementView)
        assert isinstance(
            publish_result,
            (SkillVersionView, SkillCredentialSelectionStale),
        )
        skill, version, config, bindings = await _published_state(seed)
        assert skill is not None
        assert version is not None
        if isinstance(publish_result, SkillVersionView):
            assert skill.current_published_version_id == seed.draft_version_id
            assert version.workflow_status == "published"
            assert config is not None
            assert len(bindings) == 1
        else:
            assert publish_result.code == "SKILL_CREDENTIAL_SELECTION_STALE"
            assert skill.current_published_version_id == seed.old_version_id
            assert version.workflow_status == "draft"
            assert config is None
            assert bindings == ()
    finally:
        await seed.engine.dispose()
