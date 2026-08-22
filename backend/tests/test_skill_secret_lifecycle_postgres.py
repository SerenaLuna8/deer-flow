from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import func, select
from support.private_thread_seed import seed_private_thread_database

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetConflict
from app.shared_assets.skill_secret_service import (
    SkillSecretService,
    copy_compatible_skill_secrets_in_transaction,
)
from app.shared_assets.skill_secret_store import SkillSecretStore
from deerflow.persistence.shared_assets import SkillRow, SkillVersionRow
from deerflow.persistence.shared_assets.skill_secret_model import (
    ProjectSkillSecretGenerationRow,
    ProjectSkillSecretStateRow,
    ProjectSkillSecretTombstoneRow,
)


@pytest.mark.asyncio
async def test_historical_skill_secret_can_be_cleared_but_not_replaced(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        base64.b64encode(b"historical-skill-secret-key-32by").decode(),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    actor = ProjectContext(
        user_id=seed.owner_a.user_id,
        project_id=seed.owner_a.project_id,
        membership_id=seed.owner_a.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=seed.owner_a.membership_version,
        request_id="historical-skill-secret",
    )
    skill_id = uuid.uuid4()
    historical_version_id = uuid.uuid4()
    current_version_id = uuid.uuid4()
    requirements = [{"name": "api_key", "target_env": "API_KEY", "optional": False}]
    try:
        async with seed.factory() as session, session.begin():
            skill = SkillRow(
                id=skill_id,
                scope="project",
                project_id=actor.project_id,
                slug=f"historical-secret-{skill_id.hex[:12]}",
                display_name="Historical secret lifecycle",
                status="active",
                revision=1,
                created_by_user_id=str(actor.user_id),
            )
            session.add(skill)
            session.add_all(
                (
                    SkillVersionRow(
                        id=historical_version_id,
                        skill_id=skill_id,
                        version_number=1,
                        description="Historical",
                        frontmatter={"name": "historical-secret"},
                        compatibility=None,
                        secret_requirements=requirements,
                        scan_decision="allow",
                        scan_summary={"rule_ids": []},
                        payload_checksum="a" * 64,
                        created_by_user_id=str(actor.user_id),
                    ),
                    SkillVersionRow(
                        id=current_version_id,
                        skill_id=skill_id,
                        version_number=2,
                        supersedes_version_id=historical_version_id,
                        description="Current",
                        frontmatter={"name": "historical-secret"},
                        compatibility=None,
                        secret_requirements=requirements,
                        scan_decision="allow",
                        scan_summary={"rule_ids": []},
                        payload_checksum="b" * 64,
                        created_by_user_id=str(actor.user_id),
                    ),
                )
            )
            await session.flush()
            skill.current_version_id = current_version_id
            await SkillSecretStore(session).replace_values(
                project_id=actor.project_id,
                skill_id=skill_id,
                skill_version_id=historical_version_id,
                requirements=(("api_key", False),),
                values={"api_key": "historical-value"},
                actor_user_id=str(actor.user_id),
                request_id=actor.request_id,
            )

        service = SkillSecretService(seed.factory)
        with pytest.raises(AssetConflict):
            await service.replace_for_version(
                actor,
                skill_id,
                historical_version_id,
                {"api_key": "replacement-must-fail"},
            )

        result = await service.clear(
            actor,
            skill_id,
            historical_version_id,
            "api_key",
            confirmed=True,
        )

        assert result.readiness == "unready"
        assert result.requirements[0].configured is False
        async with seed.factory() as session:
            state = await session.scalar(
                select(ProjectSkillSecretStateRow).where(
                    ProjectSkillSecretStateRow.project_id == actor.project_id,
                    ProjectSkillSecretStateRow.skill_id == skill_id,
                    ProjectSkillSecretStateRow.skill_version_id == historical_version_id,
                    ProjectSkillSecretStateRow.secret_name == "api_key",
                )
            )
            generation_count = await session.scalar(
                select(func.count())
                .select_from(ProjectSkillSecretGenerationRow)
                .where(
                    ProjectSkillSecretGenerationRow.project_id == actor.project_id,
                    ProjectSkillSecretGenerationRow.skill_id == skill_id,
                    ProjectSkillSecretGenerationRow.skill_version_id == historical_version_id,
                )
            )
            tombstone_count = await session.scalar(
                select(func.count())
                .select_from(ProjectSkillSecretTombstoneRow)
                .where(
                    ProjectSkillSecretTombstoneRow.project_id == actor.project_id,
                    ProjectSkillSecretTombstoneRow.skill_id == skill_id,
                    ProjectSkillSecretTombstoneRow.skill_version_id == historical_version_id,
                    ProjectSkillSecretTombstoneRow.reason == "clear",
                )
            )
        assert state is not None
        assert state.current_generation_id is None
        assert generation_count == 0
        assert tombstone_count == 1
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_candidate_copy_reencrypts_only_compatible_skill_secrets(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        base64.b64encode(b"s" * 32).decode(),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    actor = ProjectContext(
        user_id=seed.owner_a.user_id,
        project_id=seed.owner_a.project_id,
        membership_id=seed.owner_a.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=seed.owner_a.membership_version,
        request_id="candidate-skill-secret-copy",
    )
    editor = ProjectContext(
        user_id=seed.owner_b.user_id,
        project_id=seed.owner_b.project_id,
        membership_id=seed.owner_b.membership_id,
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=seed.owner_b.membership_version,
        request_id="candidate-skill-secret-copy-editor",
    )
    skill_id = uuid.uuid4()
    version_ids = tuple(uuid.uuid4() for _ in range(4))
    requirement = [
        {
            "name": "provider_key",
            "target_env": "TARGET_API_KEY",
            "optional": False,
        }
    ]
    try:
        async with seed.factory() as session, session.begin():
            skill = SkillRow(
                id=skill_id,
                scope="project",
                project_id=actor.project_id,
                slug=f"candidate-copy-{skill_id.hex[:12]}",
                display_name="Candidate secret copy",
                status="active",
                revision=1,
                created_by_user_id=str(actor.user_id),
            )
            session.add(skill)
            versions = tuple(
                SkillVersionRow(
                    id=version_id,
                    skill_id=skill_id,
                    version_number=index,
                    supersedes_version_id=(None if index == 1 else version_ids[index - 2]),
                    description=f"Version {index}",
                    frontmatter={"name": "candidate-copy"},
                    compatibility=None,
                    secret_requirements=(
                        [
                            {
                                "name": "provider_key",
                                "target_env": "OTHER_API_KEY",
                                "optional": False,
                            }
                        ]
                        if index == 3
                        else requirement
                    ),
                    scan_decision="allow",
                    scan_summary={"rule_ids": []},
                    payload_checksum=f"{index}" * 64,
                    created_by_user_id=str(actor.user_id),
                )
                for index, version_id in enumerate(version_ids, start=1)
            )
            session.add_all(versions)
            await session.flush()
            skill.current_version_id = versions[0].id
            source_state = (
                await SkillSecretStore(session).replace_values(
                    project_id=actor.project_id,
                    skill_id=skill_id,
                    skill_version_id=versions[0].id,
                    requirements=(("provider_key", False),),
                    values={"provider_key": "candidate-copy-test-value"},
                    actor_user_id=str(actor.user_id),
                    request_id=actor.request_id,
                )
            )[0]
            copied = await copy_compatible_skill_secrets_in_transaction(
                session,
                actor,
                skill,
                versions[0],
                versions[1],
            )
            incompatible = await copy_compatible_skill_secrets_in_transaction(
                session,
                actor,
                skill,
                versions[1],
                versions[2],
            )
            unauthorized = await copy_compatible_skill_secrets_in_transaction(
                session,
                editor,
                skill,
                versions[1],
                versions[3],
            )

            assert len(copied) == 1
            assert incompatible == ()
            assert unauthorized == ()
            source_material = (
                await SkillSecretStore(session).load_materials(
                    project_id=actor.project_id,
                    skill_id=skill_id,
                    skill_version_id=versions[0].id,
                    requirements=(("provider_key", False),),
                    require_required=True,
                    for_update=True,
                    request_id=actor.request_id,
                )
            )[0]
            target_material = (
                await SkillSecretStore(session).load_materials(
                    project_id=actor.project_id,
                    skill_id=skill_id,
                    skill_version_id=versions[1].id,
                    requirements=(("provider_key", False),),
                    require_required=True,
                    for_update=True,
                    request_id=actor.request_id,
                )
            )[0]
            store = SkillSecretStore(session)
            assert source_state.current_generation_id == source_material.generation_id
            assert source_material.generation_id != target_material.generation_id
            assert source_material.envelope != target_material.envelope
            assert store.materialize(
                source_material,
                request_id=actor.request_id,
            ) == store.materialize(
                target_material,
                request_id=actor.request_id,
            )
    finally:
        await seed.engine.dispose()
