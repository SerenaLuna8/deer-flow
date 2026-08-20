from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.shared_assets import (
    ProjectSystemSkillBindingRow,
    SkillRow,
    SkillVersionRow,
)


async def seed_new_project_system_skill_bindings(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    actor_user_id: uuid.UUID | str,
) -> int:
    """Enable every active System Skill with an eligible Current Version.

    This helper is intentionally restricted to new-project transactions. It does
    not inspect or reconcile an existing project's bindings, so a later catalog
    bootstrap cannot re-enable a binding that an administrator disabled.
    """

    statement = (
        select(SkillRow.id)
        .join(
            SkillVersionRow,
            SkillVersionRow.id == SkillRow.current_version_id,
        )
        .where(
            SkillRow.scope == "system",
            SkillRow.project_id.is_(None),
            SkillRow.status == "active",
            SkillRow.current_version_id.is_not(None),
            SkillVersionRow.skill_id == SkillRow.id,
            SkillVersionRow.version_number == 1,
            SkillVersionRow.revoked_at.is_(None),
        )
        .order_by(SkillRow.id)
        .with_for_update(read=True, of=[SkillRow, SkillVersionRow])
    )
    targets = tuple((await session.execute(statement)).scalars().all())
    actor_id = str(actor_user_id)
    session.add_all(
        [
            ProjectSystemSkillBindingRow(
                project_id=project_id,
                system_skill_id=skill_id,
                enabled=True,
                created_by_user_id=actor_id,
                updated_by_user_id=actor_id,
            )
            for skill_id in targets
        ]
    )
    if targets:
        await session.flush()
    return len(targets)


__all__ = ["seed_new_project_system_skill_bindings"]
