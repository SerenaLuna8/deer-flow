from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.shared_assets.skill_version_facts import skill_version_archive_facts


@dataclass(frozen=True)
class SealedSkillVersionFixture:
    version_id: uuid.UUID
    path: str
    media_type: str
    content: bytes
    sha256: str
    payload_checksum: str
    file_count: int
    content_size_bytes: int


def sealed_skill_version_fixture(
    version_id: uuid.UUID,
    *,
    name: str,
) -> SealedSkillVersionFixture:
    """Build one canonical file and the exact parent facts for a test Skill version."""

    path = "SKILL.md"
    content = (f"---\nname: {name}\ndescription: PostgreSQL fixture Skill.\n---\n\n# {name}\n").encode()
    sha256 = hashlib.sha256(content).hexdigest()
    facts = skill_version_archive_facts(((path, sha256, len(content)),))
    return SealedSkillVersionFixture(
        version_id=version_id,
        path=path,
        media_type="text/markdown",
        content=content,
        sha256=sha256,
        payload_checksum=facts.payload_checksum,
        file_count=facts.file_count,
        content_size_bytes=facts.content_size_bytes,
    )


async def assemble_and_seal_skill_version(
    executor: AsyncConnection | AsyncSession,
    fixture: SealedSkillVersionFixture,
) -> None:
    """Persist the exact file set through the production assembly and seal gates."""

    await executor.execute(
        text(
            """SELECT set_config(
                'deerflow.asset_version_assembly',
                :version_id,
                true
            )"""
        ),
        {"version_id": str(fixture.version_id)},
    )
    await executor.execute(
        text(
            """INSERT INTO skill_version_files
            (skill_version_id,path,media_type,size_bytes,sha256,content)
            VALUES
            (:version_id,:path,:media_type,:size_bytes,:sha256,:content)"""
        ),
        {
            "version_id": fixture.version_id,
            "path": fixture.path,
            "media_type": fixture.media_type,
            "size_bytes": fixture.content_size_bytes,
            "sha256": fixture.sha256,
            "content": fixture.content,
        },
    )
    await executor.execute(
        text(
            """UPDATE skill_versions
            SET files_sealed=true
            WHERE id=:version_id"""
        ),
        {"version_id": fixture.version_id},
    )
