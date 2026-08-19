from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace

import pytest

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.errors import (
    AssetForbidden,
    SkillFrontmatterSourceStale,
    SkillSecretDeclarationInvalid,
)
from app.shared_assets.skill_frontmatter_service import (
    MAX_SKILL_FRONTMATTER_DOCUMENT_BYTES,
    SkillFrontmatterService,
)
from deerflow.skills.types import SecretRequirement


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role="editor",
        capabilities=frozenset(
            {
                Capability.SHARED_ASSETS_READ,
                Capability.SHARED_ASSETS_EDIT,
            }
        ),
        membership_version=1,
        request_id="skill-frontmatter-service",
    )


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


_VALID_SKILL = """---
name: form-service
description: Form service fixture
---

# Body
"""


@pytest.mark.asyncio
async def test_parse_returns_safe_projection_for_the_exact_buffer() -> None:
    result = await SkillFrontmatterService().parse(
        _context(),
        _VALID_SKILL,
        expected_source_sha256=_sha(_VALID_SKILL),
    )

    assert result.valid is True
    assert result.patchable is True
    assert result.source_sha256 == _sha(_VALID_SKILL)
    assert result.projection is not None
    assert result.projection.required_secrets == ()
    assert result.projection.secrets_autonomous is True
    assert result.projection.secrets_autonomous_explicit is False
    assert result.diagnostics == ()


@pytest.mark.asyncio
async def test_patch_updates_the_same_skill_md_buffer() -> None:
    result = await SkillFrontmatterService().patch(
        _context(),
        _VALID_SKILL,
        expected_source_sha256=_sha(_VALID_SKILL),
        required_secrets=(
            SecretRequirement(name="YES", optional=False),
            SecretRequirement(name="OPTIONAL_TOKEN", optional=True),
        ),
        secrets_autonomous=False,
    )

    assert result.changed is True
    assert result.source_sha256 == _sha(_VALID_SKILL)
    assert result.result_sha256 == _sha(result.content)
    assert 'name: "YES"' in result.content
    assert 'name: "OPTIONAL_TOKEN"' in result.content
    assert "secrets-autonomous: false" in result.content
    assert result.content.endswith("\n# Body\n")


@pytest.mark.asyncio
async def test_read_capability_can_parse_but_cannot_patch() -> None:
    actor = replace(
        _context(),
        capabilities=frozenset({Capability.SHARED_ASSETS_READ}),
    )

    parsed = await SkillFrontmatterService().parse(
        actor,
        _VALID_SKILL,
        expected_source_sha256=_sha(_VALID_SKILL),
    )
    assert parsed.valid is True

    with pytest.raises(AssetForbidden):
        await SkillFrontmatterService().patch(
            actor,
            _VALID_SKILL,
            expected_source_sha256=_sha(_VALID_SKILL),
            required_secrets=(),
            secrets_autonomous=True,
        )


@pytest.mark.asyncio
async def test_source_hash_mismatch_is_a_conflict() -> None:
    with pytest.raises(SkillFrontmatterSourceStale):
        await SkillFrontmatterService().patch(
            _context(),
            _VALID_SKILL,
            expected_source_sha256="0" * 64,
            required_secrets=(),
            secrets_autonomous=True,
        )


@pytest.mark.asyncio
async def test_invalid_document_patch_returns_safe_structured_diagnostics() -> None:
    content = "---\nname: broken\nrequired-secrets: [\n---\n"

    with pytest.raises(SkillSecretDeclarationInvalid) as exc_info:
        await SkillFrontmatterService().patch(
            _context(),
            content,
            expected_source_sha256=_sha(content),
            required_secrets=(),
            secrets_autonomous=True,
        )

    assert exc_info.value.request_id == _context().request_id
    assert exc_info.value.diagnostics
    assert all("[" not in item.public_message for item in exc_info.value.diagnostics)


@pytest.mark.asyncio
async def test_document_size_is_bounded_before_yaml_processing() -> None:
    content = "x" * (MAX_SKILL_FRONTMATTER_DOCUMENT_BYTES + 1)

    with pytest.raises(SkillSecretDeclarationInvalid):
        await SkillFrontmatterService().parse(
            _context(),
            content,
            expected_source_sha256=_sha(content),
        )
