from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, func, select
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
    RunSnapshotAssetStale,
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
    AssetValidationFailed,
    SkillCredentialBindingInvalid,
    SkillCredentialBindingsIncomplete,
    SkillCredentialSelectionStale,
)
from app.shared_assets.keyring import CredentialKeyring
from app.shared_assets.models import AssetKind, AssetScope, SkillArchiveFile
from app.shared_assets.skill_credential_policy import SkillCredentialBindingInput
from app.shared_assets.skill_credential_service import (
    SkillCredentialBindingService,
    SkillCredentialBindingSetView,
)
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
    candidate_version_id: uuid.UUID
    candidate_checksum: str
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
        "postgres-skill-activation-seed",
    )
    row = SkillVersionRow(
        id=version_id,
        skill_id=skill_id,
        version_number=version_number,
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
    candidate_version_id = uuid.uuid4()
    actor = ProjectContext(
        user_id=user_id,
        project_id=project_id,
        membership_id=membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="req-postgres-atomic-skill-activation",
    )
    old, old_files = _version_rows(
        skill_id,
        old_version_id,
        version_number=1,
        body="Use the old binding.",
        user_id=user_id,
    )
    candidate, candidate_files = _version_rows(
        skill_id,
        candidate_version_id,
        version_number=2,
        body="Use the new binding.",
        user_id=user_id,
    )
    candidate.supersedes_version_id = old_version_id

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
                display_name="Skill activation test",
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
            current_version_id=None,
            revision=8,
            created_by_user_id=str(user_id),
        )
        session.add(skill)
        await session.flush()
        session.add(old)
        await session.flush()
        await session.execute(
            select(
                func.set_config(
                    "deerflow.asset_version_assembly",
                    str(old_version_id),
                    True,
                )
            )
        )
        session.add_all(old_files)
        await session.flush()
        skill.current_version_id = old_version_id
        await session.flush()
        session.add(candidate)
        await session.flush()
        await session.execute(
            select(
                func.set_config(
                    "deerflow.asset_version_assembly",
                    str(candidate_version_id),
                    True,
                )
            )
        )
        session.add_all(candidate_files)
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
                source_env_field_name="API_KEY",
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
        candidate_version_id=candidate_version_id,
        candidate_checksum=candidate.payload_checksum,
        old_credential=old_credential,
        new_credential=new_credential,
    )


async def _activation_state(seed: _Seed):
    async with seed.factory() as session:
        skill = await session.get(SkillRow, seed.skill_id)
        version = await session.get(SkillVersionRow, seed.candidate_version_id)
        config = await session.get(
            ProjectSkillCredentialConfigRow,
            (
                seed.actor.project_id,
                seed.skill_id,
                seed.candidate_version_id,
            ),
        )
        bindings = tuple(
            (
                await session.execute(
                    select(ProjectSkillCredentialBindingRow).where(
                        ProjectSkillCredentialBindingRow.project_id == seed.actor.project_id,
                        ProjectSkillCredentialBindingRow.skill_id == seed.skill_id,
                        ProjectSkillCredentialBindingRow.skill_version_id == seed.candidate_version_id,
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
        snapshot_json={},
    )
    async with seed.factory() as session, session.begin():
        return await RunSnapshotRepository(seed.factory).current_skill_credentials_in_session(
            session,
            context,
            (snapshot,),
        )


async def _lock_admitted_skill_credentials(
    seed: _Seed,
    persisted: tuple[RunSkillCredentialSnapshot, ...],
):
    context = PrivateWorkContext.from_project(seed.actor)
    async with seed.factory() as session, session.begin():
        return await RunSnapshotRepository(seed.factory).lock_admitted_skill_credentials_in_session(
            session,
            context,
            persisted,
            declared_targets=frozenset(
                {(seed.old_version_id, "API_KEY")},
            ),
            required_targets=frozenset(
                {(seed.old_version_id, "API_KEY")},
            ),
        )


def _durable_audit_sink() -> DurableSharedAssetGovernanceEventSink:
    return DurableSharedAssetGovernanceEventSink(
        AuditService(
            None,
            AuditHmacKeyring(
                active_key_id="skill-activation-test",
                _keys={"skill-activation-test": b"a" * 32},
            ),
        )
    )


async def _configure_candidate(
    seed: _Seed,
    *,
    source_env_field_name: str = "API_KEY",
    durable_audit: bool = False,
) -> SkillCredentialBindingSetView:
    return await SkillCredentialBindingService(
        seed.factory,
        governance_sink=(_durable_audit_sink() if durable_audit else None),
    ).replace_for_version(
        seed.actor,
        seed.skill_id,
        seed.candidate_version_id,
        (
            SkillCredentialBindingInput(
                "API_KEY",
                seed.new_credential.version_id,
                source_env_field_name,
            ),
        ),
        expected_revision=0,
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_activation_uses_preconfigured_alias_mapping_and_exact_cas(
    migrated_postgres_database_url: str,
) -> None:
    seed = await _seed(
        migrated_postgres_database_url,
        new_credential_env=("PROJECT_API_TOKEN",),
    )
    try:
        old_before = await _run_skill_credentials(
            seed,
            version_id=seed.old_version_id,
            payload_checksum=seed.old_checksum,
        )
        assert old_before[0].credential_version_id == seed.old_credential.version_id
        assert old_before[0].source_env_field_name == "API_KEY"
        with pytest.raises(RunSnapshotAssetStale):
            await _lock_admitted_skill_credentials(seed, ())

        configured = await _configure_candidate(
            seed,
            source_env_field_name="PROJECT_API_TOKEN",
            durable_audit=True,
        )
        assert configured.revision == 1
        assert configured.requirements[0].source_env_field_name == ("PROJECT_API_TOKEN")

        activated = await skill_service_module.SkillService(
            seed.factory,
            governance_sink=_durable_audit_sink(),
        ).activate_version(
            seed.actor,
            seed.skill_id,
            seed.candidate_version_id,
            expected_asset_version=8,
            expected_payload_checksum=seed.candidate_checksum,
            expected_binding_revision=1,
        )

        assert activated.relation.value == "current"
        skill, version, config, bindings = await _activation_state(seed)
        assert skill is not None
        assert version is not None
        assert skill.current_version_id == seed.candidate_version_id
        assert config is not None and config.revision == 1
        assert len(bindings) == 1
        assert bindings[0].credential_version_id == seed.new_credential.version_id
        assert bindings[0].source_env_field_name == "PROJECT_API_TOKEN"

        with pytest.raises(RunSnapshotAssetStale):
            await _run_skill_credentials(
                seed,
                version_id=seed.old_version_id,
                payload_checksum=seed.old_checksum,
            )
        admitted_old_after = await _lock_admitted_skill_credentials(
            seed,
            old_before,
        )
        assert admitted_old_after[0].skill_version_id == seed.old_version_id
        assert admitted_old_after[0].credential_version_id == seed.old_credential.version_id
        new_after = await _run_skill_credentials(
            seed,
            version_id=seed.candidate_version_id,
            payload_checksum=seed.candidate_checksum,
        )
        assert new_after[0].credential_version_id == seed.new_credential.version_id
        assert new_after[0].source_env_field_name == "PROJECT_API_TOKEN"

        async with seed.factory() as session:
            audit_rows = tuple((await session.execute(select(AuditLogRow).order_by(AuditLogRow.occurred_at, AuditLogRow.id))).scalars().all())
        assert [row.metadata_json["operation"] for row in audit_rows] == [
            "skill.credential_bindings.configure",
            "skill.version.activate",
        ]
        assert set(audit_rows[0].metadata_json) == {"asset_kind", "operation"}
        assert audit_rows[1].metadata_json == {
            "asset_kind": "skill",
            "operation": "skill.version.activate",
            "version_number": 2,
        }
        audit_payload = repr([row.metadata_json for row in audit_rows])
        assert str(seed.new_credential.credential_id) not in audit_payload
        assert str(seed.new_credential.version_id) not in audit_payload
        assert "API_KEY" not in audit_payload

        with pytest.raises(SkillCredentialBindingsIncomplete):
            await SkillCredentialBindingService(seed.factory).replace(
                seed.actor,
                seed.skill_id,
                (),
                expected_skill_version_id=seed.candidate_version_id,
                expected_revision=1,
            )
        _skill, _version, config_after, bindings_after = await _activation_state(seed)
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
        request_id="req-postgres-admin-override-skill-activation",
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
            await service.activate_version(
                admin_override,
                seed.skill_id,
                seed.candidate_version_id,
                expected_asset_version=8,
                expected_payload_checksum=seed.candidate_checksum,
                expected_binding_revision=0,
            )

        skill, version, config, bindings = await _activation_state(seed)
        assert skill is not None
        assert version is not None
        assert skill.current_version_id == seed.old_version_id
        assert skill.revision == 8
        assert config is None
        assert bindings == ()

        async with seed.factory() as session, session.begin():
            session.add(
                ProjectSkillCredentialConfigRow(
                    project_id=seed.actor.project_id,
                    skill_id=seed.skill_id,
                    skill_version_id=seed.candidate_version_id,
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
                    skill_version_id=seed.candidate_version_id,
                    secret_name="API_KEY",
                    source_env_field_name="API_KEY",
                    credential_id=seed.new_credential.credential_id,
                    credential_version_id=seed.new_credential.version_id,
                    config_revision=1,
                    created_by_user_id=str(seed.actor.user_id),
                )
            )

        activated = await service.activate_version(
            admin_override,
            seed.skill_id,
            seed.candidate_version_id,
            expected_asset_version=8,
            expected_payload_checksum=seed.candidate_checksum,
            expected_binding_revision=1,
        )

        assert activated.relation.value == "current"
        skill, version, config, bindings = await _activation_state(seed)
        assert skill is not None
        assert version is not None
        assert skill.current_version_id == seed.candidate_version_id
        assert skill.revision == 9
        assert config is not None and config.revision == 1
        assert len(bindings) == 1
        assert bindings[0].credential_version_id == seed.new_credential.version_id
    finally:
        await seed.engine.dispose()


class _FailingActivationAudit:
    def __init__(self) -> None:
        self._delegate = _durable_audit_sink()

    async def append_project(self, session, **kwargs) -> None:
        await self._delegate.append_project(session, **kwargs)
        if kwargs["action"] == "skill.version.activate":
            raise RuntimeError("activation audit failed")


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["missing_required", "schema_mismatch", "envelope_missing", "audit"],
)
async def test_postgres_activation_failure_rolls_back_pointer_version_and_binding(
    migrated_postgres_database_url: str,
    failure: str,
) -> None:
    seed = await _seed(
        migrated_postgres_database_url,
        new_credential_env=(("OTHER_API_KEY",) if failure == "schema_mismatch" else ("API_KEY",)),
        new_credential_envelope_active=failure != "envelope_missing",
    )
    try:
        if failure in {"schema_mismatch", "envelope_missing"}:
            expected_error = SkillCredentialBindingInvalid if failure == "schema_mismatch" else SkillCredentialSelectionStale
            with pytest.raises(expected_error):
                await _configure_candidate(seed)
        else:
            binding_revision = 0
            if failure == "audit":
                await _configure_candidate(seed, durable_audit=True)
                binding_revision = 1
            service = skill_service_module.SkillService(
                seed.factory,
                governance_sink=(_FailingActivationAudit() if failure == "audit" else None),
            )
            expected_error = RuntimeError if failure == "audit" else SkillCredentialBindingsIncomplete
            with pytest.raises(expected_error):
                await service.activate_version(
                    seed.actor,
                    seed.skill_id,
                    seed.candidate_version_id,
                    expected_asset_version=8,
                    expected_payload_checksum=seed.candidate_checksum,
                    expected_binding_revision=binding_revision,
                )

        skill, version, config, bindings = await _activation_state(seed)
        assert skill is not None
        assert version is not None
        assert skill.current_version_id == seed.old_version_id
        assert skill.revision == 8
        if failure == "audit":
            assert config is not None and config.revision == 1
            assert len(bindings) == 1
        else:
            assert config is None
            assert bindings == ()
        if failure == "audit":
            async with seed.factory() as session:
                rows = tuple((await session.execute(select(AuditLogRow).order_by(AuditLogRow.occurred_at))).scalars().all())
                assert [row.metadata_json["operation"] for row in rows] == ["skill.credential_bindings.configure"]
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_activation_rejects_stale_preflight_revision(
    migrated_postgres_database_url: str,
) -> None:
    seed = await _seed(migrated_postgres_database_url)
    try:
        await _configure_candidate(seed)
        with pytest.raises(SkillCredentialSelectionStale):
            await skill_service_module.SkillService(seed.factory).activate_version(
                seed.actor,
                seed.skill_id,
                seed.candidate_version_id,
                expected_asset_version=8,
                expected_payload_checksum=seed.candidate_checksum,
                expected_binding_revision=0,
            )
        skill, version, config, bindings = await _activation_state(seed)
        assert skill is not None
        assert version is not None
        assert skill.current_version_id == seed.old_version_id
        assert skill.revision == 8
        assert config is not None and config.revision == 1
        assert len(bindings) == 1
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_credential_rotation_preserves_alias_source_field(
    migrated_postgres_database_url: str,
) -> None:
    seed = await _seed(
        migrated_postgres_database_url,
        new_credential_env=("PROJECT_API_TOKEN", "UNRELATED_TOKEN"),
    )
    try:
        await _configure_candidate(
            seed,
            source_env_field_name="PROJECT_API_TOKEN",
        )
        await skill_service_module.SkillService(seed.factory).activate_version(
            seed.actor,
            seed.skill_id,
            seed.candidate_version_id,
            expected_asset_version=8,
            expected_payload_checksum=seed.candidate_checksum,
            expected_binding_revision=1,
        )
        credentials = CredentialService(
            seed.factory,
            keyring=CredentialKeyring("test-key", {"test-key": b"k" * 32}),
        )
        replacement = await credentials.replace(
            seed.actor,
            seed.new_credential.credential_id,
            {
                "env": {
                    "PROJECT_API_TOKEN": "rotated-provider-token",
                    "UNRELATED_TOKEN": "must-not-be-mapped",
                },
            },
            expected_credential_version=1,
        )
        assert replacement.pending_migration is not None
        assert replacement.pending_migration.total == 1
        assert replacement.pending_migration.mcp_grant_count == 0
        assert replacement.pending_migration.skill_binding_count == 1
        assert replacement.pending_migration.system_model_count == 0
        assert len(replacement.pending_migration.references) == 1
        reference = replacement.pending_migration.references[0]
        assert reference.kind == "skill_binding"
        assert reference.reference_name == "API_KEY"
        assert reference.source_name == "PROJECT_API_TOKEN"
        assert replacement.pending_migration.current_reference_count == 0
        assert replacement.pending_migration.current_references == ()

        migrated = await credentials.migrate_grants(
            seed.actor,
            seed.new_credential.credential_id,
            expected_credential_version=2,
        )

        assert migrated.migrated_count == 1
        status = await credentials.migration_status(
            seed.actor,
            seed.new_credential.credential_id,
        )
        assert status.total == 0
        assert status.current_reference_count == 1
        assert status.current_references[0].reference_name == "API_KEY"
        assert status.current_references[0].source_name == "PROJECT_API_TOKEN"
        async with seed.factory() as session:
            config = await session.get(
                ProjectSkillCredentialConfigRow,
                (
                    seed.actor.project_id,
                    seed.skill_id,
                    seed.candidate_version_id,
                ),
            )
            rows = tuple(
                (
                    await session.execute(
                        select(ProjectSkillCredentialBindingRow)
                        .where(
                            ProjectSkillCredentialBindingRow.project_id == seed.actor.project_id,
                            ProjectSkillCredentialBindingRow.skill_id == seed.skill_id,
                            ProjectSkillCredentialBindingRow.skill_version_id == seed.candidate_version_id,
                        )
                        .order_by(
                            ProjectSkillCredentialBindingRow.config_revision,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert config is not None and config.revision == 2
        assert [row.status for row in rows] == ["revoked", "active"]
        assert [row.source_env_field_name for row in rows] == [
            "PROJECT_API_TOKEN",
            "PROJECT_API_TOKEN",
        ]
        assert rows[0].credential_version_id == seed.new_credential.version_id
        assert rows[1].credential_version_id == replacement.version.id
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_credential_rotation_rejects_missing_alias_source_field(
    migrated_postgres_database_url: str,
) -> None:
    seed = await _seed(
        migrated_postgres_database_url,
        new_credential_env=("PROJECT_API_TOKEN", "UNRELATED_TOKEN"),
    )
    try:
        await _configure_candidate(
            seed,
            source_env_field_name="PROJECT_API_TOKEN",
        )
        credentials = CredentialService(
            seed.factory,
            keyring=CredentialKeyring("test-key", {"test-key": b"k" * 32}),
        )
        await credentials.replace(
            seed.actor,
            seed.new_credential.credential_id,
            {"env": {"RENAMED_TOKEN": "incompatible-rotation"}},
            expected_credential_version=1,
        )

        with pytest.raises(AssetValidationFailed):
            await credentials.migrate_grants(
                seed.actor,
                seed.new_credential.credential_id,
                expected_credential_version=2,
            )

        async with seed.factory() as session:
            config = await session.get(
                ProjectSkillCredentialConfigRow,
                (
                    seed.actor.project_id,
                    seed.skill_id,
                    seed.candidate_version_id,
                ),
            )
            rows = tuple(
                (
                    await session.execute(
                        select(ProjectSkillCredentialBindingRow).where(
                            ProjectSkillCredentialBindingRow.project_id == seed.actor.project_id,
                            ProjectSkillCredentialBindingRow.skill_id == seed.skill_id,
                            ProjectSkillCredentialBindingRow.skill_version_id == seed.candidate_version_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert config is not None and config.revision == 1
        assert len(rows) == 1
        assert rows[0].status == "active"
        assert rows[0].source_env_field_name == "PROJECT_API_TOKEN"
        assert rows[0].credential_version_id == seed.new_credential.version_id
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_activation_and_credential_rotation_never_deadlock_or_partially_activate(
    migrated_postgres_database_url: str,
) -> None:
    seed = await _seed(migrated_postgres_database_url)
    try:
        await _configure_candidate(seed)
        activation = skill_service_module.SkillService(seed.factory).activate_version(
            seed.actor,
            seed.skill_id,
            seed.candidate_version_id,
            expected_asset_version=8,
            expected_payload_checksum=seed.candidate_checksum,
            expected_binding_revision=1,
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

        activation_result, rotate_result = await asyncio.wait_for(
            asyncio.gather(activation, rotate, return_exceptions=True),
            timeout=15,
        )

        assert isinstance(rotate_result, CredentialReplacementView)
        assert isinstance(
            activation_result,
            (SkillVersionView, SkillCredentialSelectionStale),
        )
        skill, version, config, bindings = await _activation_state(seed)
        assert skill is not None
        assert version is not None
        if isinstance(activation_result, SkillVersionView):
            assert skill.current_version_id == seed.candidate_version_id
            assert config is not None
            assert len(bindings) == 1
        else:
            assert activation_result.code == "SKILL_CREDENTIAL_SELECTION_STALE"
            assert skill.current_version_id == seed.old_version_id
            assert config is not None and config.revision == 1
            assert len(bindings) == 1
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_activation_and_binding_replace_never_retarget_stale_version(
    migrated_postgres_database_url: str,
) -> None:
    seed = await _seed(migrated_postgres_database_url)
    try:
        await _configure_candidate(seed)
        activation = skill_service_module.SkillService(seed.factory).activate_version(
            seed.actor,
            seed.skill_id,
            seed.candidate_version_id,
            expected_asset_version=8,
            expected_payload_checksum=seed.candidate_checksum,
            expected_binding_revision=1,
        )
        replace = SkillCredentialBindingService(seed.factory).replace(
            seed.actor,
            seed.skill_id,
            (
                SkillCredentialBindingInput(
                    "API_KEY",
                    seed.old_credential.version_id,
                ),
            ),
            expected_skill_version_id=seed.old_version_id,
            expected_revision=1,
        )

        activation_result, replace_result = await asyncio.wait_for(
            asyncio.gather(activation, replace, return_exceptions=True),
            timeout=15,
        )

        assert isinstance(activation_result, SkillVersionView)
        assert isinstance(
            replace_result,
            (SkillCredentialBindingSetView, SkillCredentialSelectionStale),
        )
        if isinstance(replace_result, SkillCredentialBindingSetView):
            assert replace_result.skill_version_id == seed.old_version_id
        else:
            assert replace_result.code == "SKILL_CREDENTIAL_SELECTION_STALE"

        skill, version, config, bindings = await _activation_state(seed)
        assert skill is not None
        assert version is not None
        assert skill.current_version_id == seed.candidate_version_id
        assert config is not None and config.skill_version_id == seed.candidate_version_id
        assert len(bindings) == 1
        assert bindings[0].credential_version_id == seed.new_credential.version_id
    finally:
        await seed.engine.dispose()
