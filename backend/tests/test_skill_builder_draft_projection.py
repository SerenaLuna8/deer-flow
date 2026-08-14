"""Skill Builder draft projection stays available during partial generation."""

from __future__ import annotations

import uuid

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.models import SkillArchiveFile
from app.shared_assets.skill_design_service import SkillDesignService


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        project_id=uuid.UUID("00000000-0000-4000-8000-000000000002"),
        membership_id=uuid.UUID("00000000-0000-4000-8000-000000000003"),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="skill-builder-draft-projection",
    )


def test_partial_draft_without_root_skill_md_remains_projectable() -> None:
    partial = (
        SkillArchiveFile(
            "chinese-statistics/SKILL.md",
            b"---\nname: chinese-statistics\n---\n",
            "text/markdown",
        ),
    )

    views = SkillDesignService._file_views(_context(), partial)

    assert tuple(item.path for item in views) == ("chinese-statistics/SKILL.md",)


def test_partial_draft_without_root_skill_md_can_resume_generation() -> None:
    partial = (
        SkillArchiveFile(
            "chinese-statistics/SKILL.md",
            b"---\nname: chinese-statistics\n---\n",
            "text/markdown",
        ),
    )

    resumed = SkillDesignService._validate_partial_builder_files(
        _context(),
        partial,
    )

    assert resumed == partial


def test_final_candidate_validation_still_requires_root_skill_md() -> None:
    partial = (
        SkillArchiveFile(
            "chinese-statistics/SKILL.md",
            b"---\nname: chinese-statistics\n---\n",
            "text/markdown",
        ),
    )

    with pytest.raises(AssetValidationFailed):
        SkillDesignService._validate_builder_files(_context(), partial)
