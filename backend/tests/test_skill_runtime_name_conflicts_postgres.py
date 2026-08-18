from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import pytest
from sqlalchemy import exists, select
from support.private_thread_seed import PrivateThreadSeed, seed_private_thread_database

from app.projects.context import ProjectContext
from app.shared_assets.binding_service import BindingService
from app.shared_assets.errors import SkillRuntimeNameConflict
from app.shared_assets.models import AssetKind, AssetSelection
from app.shared_assets.skill_service import SkillService
from deerflow.persistence.shared_assets import (
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionRow,
)


@dataclass(frozen=True)
class _SkillPair:
    project_skill_id: uuid.UUID
    project_version_id: uuid.UUID
    system_skill_id: uuid.UUID
    system_version_id: uuid.UUID


def _project_context(seed: PrivateThreadSeed) -> ProjectContext:
    source = seed.owner_a
    return ProjectContext(
        user_id=source.user_id,
        project_id=source.project_id,
        membership_id=source.membership_id,
        role=source.role,
        capabilities=source.capabilities,
        membership_version=source.membership_version,
        request_id="skill-runtime-name-postgres",
    )


async def _seed_skill_pairs(
    seed: PrivateThreadSeed,
    slugs: Sequence[str],
) -> dict[str, _SkillPair]:
    pairs: dict[str, _SkillPair] = {}
    async with seed.factory() as session, session.begin():
        for slug in slugs:
            project_skill = SkillRow(
                scope="project",
                project_id=seed.owner_a.project_id,
                slug=slug,
                display_name=f"Project {slug}",
                status="suspended",
                version=1,
                created_by_user_id=str(seed.owner_a.user_id),
            )
            system_skill = SkillRow(
                scope="system",
                project_id=None,
                slug=slug,
                display_name=f"System {slug}",
                status="active",
                version=1,
                created_by_user_id=str(seed.owner_a.user_id),
            )
            session.add_all((project_skill, system_skill))
            await session.flush()

            project_version = SkillVersionRow(
                skill_id=project_skill.id,
                version_number=1,
                workflow_status="published",
                description="Project runtime-name conflict fixture",
                frontmatter={"name": slug},
                compatibility=None,
                secret_requirements=[],
                scan_decision="allow",
                scan_summary={"rule_ids": []},
                payload_checksum="a" * 64,
                created_by_user_id=str(seed.owner_a.user_id),
            )
            system_version = SkillVersionRow(
                skill_id=system_skill.id,
                version_number=1,
                workflow_status="published",
                description="System runtime-name conflict fixture",
                frontmatter={"name": slug},
                compatibility=None,
                secret_requirements=[],
                scan_decision="allow",
                scan_summary={"rule_ids": []},
                payload_checksum="b" * 64,
                created_by_user_id=str(seed.owner_a.user_id),
            )
            session.add_all((project_version, system_version))
            await session.flush()
            project_skill.current_published_version_id = project_version.id
            system_skill.current_published_version_id = system_version.id
            pairs[slug] = _SkillPair(
                project_skill.id,
                project_version.id,
                system_skill.id,
                system_version.id,
            )
    return pairs


async def _runtime_visibility(
    seed: PrivateThreadSeed,
    pair: _SkillPair,
) -> tuple[bool, bool]:
    async with seed.factory() as session:
        project_visible = bool(
            await session.scalar(
                select(
                    exists(
                        select(1)
                        .select_from(SkillRow)
                        .join(
                            SkillVersionRow,
                            SkillVersionRow.skill_id == SkillRow.id,
                        )
                        .where(
                            SkillRow.id == pair.project_skill_id,
                            SkillRow.scope == "project",
                            SkillRow.project_id == seed.owner_a.project_id,
                            SkillRow.status == "active",
                            SkillRow.current_published_version_id == pair.project_version_id,
                            SkillVersionRow.id == pair.project_version_id,
                            SkillVersionRow.workflow_status == "published",
                            SkillVersionRow.revoked_at.is_(None),
                        )
                    )
                )
            )
        )
        system_visible = bool(
            await session.scalar(
                select(
                    exists(
                        select(1)
                        .select_from(ProjectSystemSkillBindingRow)
                        .join(
                            SkillRow,
                            SkillRow.id == ProjectSystemSkillBindingRow.system_skill_id,
                        )
                        .join(
                            SkillVersionRow,
                            SkillVersionRow.id == ProjectSystemSkillBindingRow.skill_version_id,
                        )
                        .where(
                            ProjectSystemSkillBindingRow.project_id == seed.owner_a.project_id,
                            ProjectSystemSkillBindingRow.system_skill_id == pair.system_skill_id,
                            ProjectSystemSkillBindingRow.skill_version_id == pair.system_version_id,
                            ProjectSystemSkillBindingRow.enabled.is_(True),
                            SkillRow.scope == "system",
                            SkillRow.project_id.is_(None),
                            SkillRow.status == "active",
                            SkillRow.current_published_version_id == pair.system_version_id,
                            SkillVersionRow.skill_id == pair.system_skill_id,
                            SkillVersionRow.workflow_status == "published",
                            SkillVersionRow.revoked_at.is_(None),
                        )
                    )
                )
            )
        )
    return project_visible, system_visible


async def _activate_project_skill(
    service: SkillService,
    actor: ProjectContext,
    pair: _SkillPair,
) -> None:
    await service.activate(
        actor,
        pair.project_skill_id,
        expected_asset_version=1,
    )


async def _enable_system_skill(
    service: BindingService,
    actor: ProjectContext,
    pair: _SkillPair,
) -> None:
    await service.enable(
        actor,
        AssetSelection(
            AssetKind.SKILL,
            pair.system_skill_id,
            pair.system_version_id,
        ),
    )


async def _race_operation(
    label: str,
    start: asyncio.Event,
    operation: Callable[[], Awaitable[None]],
) -> str:
    await start.wait()
    try:
        await operation()
    except SkillRuntimeNameConflict:
        return "conflict"
    return label


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_and_system_skill_runtime_name_conflicts_are_serialized(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    actor = _project_context(seed)
    skills = SkillService(seed.factory)
    bindings = BindingService(seed.factory)
    pairs = await _seed_skill_pairs(
        seed,
        (
            "runtime-project-first",
            "runtime-system-first",
            "runtime-concurrent",
        ),
    )

    try:
        project_first = pairs["runtime-project-first"]
        await _activate_project_skill(skills, actor, project_first)
        with pytest.raises(SkillRuntimeNameConflict):
            await _enable_system_skill(bindings, actor, project_first)
        assert await _runtime_visibility(seed, project_first) == (True, False)

        system_first = pairs["runtime-system-first"]
        await _enable_system_skill(bindings, actor, system_first)
        with pytest.raises(SkillRuntimeNameConflict):
            await _activate_project_skill(skills, actor, system_first)
        assert await _runtime_visibility(seed, system_first) == (False, True)

        concurrent = pairs["runtime-concurrent"]
        start = asyncio.Event()
        project_task = asyncio.create_task(
            _race_operation(
                "project",
                start,
                lambda: _activate_project_skill(skills, actor, concurrent),
            )
        )
        system_task = asyncio.create_task(
            _race_operation(
                "system",
                start,
                lambda: _enable_system_skill(bindings, actor, concurrent),
            )
        )
        await asyncio.sleep(0)
        start.set()
        outcomes = await asyncio.gather(project_task, system_task)

        assert outcomes.count("conflict") == 1
        winner = next(outcome for outcome in outcomes if outcome != "conflict")
        assert winner in {"project", "system"}
        visibility = await _runtime_visibility(seed, concurrent)
        assert visibility == (winner == "project", winner == "system")
        assert sum(visibility) == 1
    finally:
        await seed.engine.dispose()
