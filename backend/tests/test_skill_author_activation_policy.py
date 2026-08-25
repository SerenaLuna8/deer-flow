from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import skill_service as skill_service_module
from app.shared_assets.errors import AssetForbidden
from app.shared_assets.models import SkillArchiveFile
from app.shared_assets.skill_deletion import SkillDeleteResult


def _context(role: ProjectRole) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id=f"skill-policy-{role.value}",
    )


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def begin(self):
        return self


@pytest.mark.asyncio
async def test_editor_is_authorized_to_activate_skill_version() -> None:
    class _ExplodingFactory:
        def __call__(self):
            raise AssertionError("authorization must fail before storage is opened")

    with pytest.raises(AssertionError, match="authorization"):
        await skill_service_module.SkillService(_ExplodingFactory()).activate_version(
            _context(ProjectRole.EDITOR),
            uuid.uuid4(),
            uuid.uuid4(),
            expected_asset_version=1,
            expected_payload_checksum="a" * 64,
            expected_secret_revision=0,
        )


@pytest.mark.asyncio
async def test_editor_is_authorized_to_save_batch_replacement_candidates() -> None:
    class _ExplodingFactory:
        def __call__(self):
            raise AssertionError("authorization must fail before storage is opened")

    with pytest.raises(AssertionError, match="authorization"):
        await skill_service_module.SkillService(
            _ExplodingFactory(),
        ).import_project_archives_atomic(
            _context(ProjectRole.EDITOR),
            (
                skill_service_module.ProjectSkillArchiveImport(
                    files=(
                        SkillArchiveFile(
                            "SKILL.md",
                            b"---\nname: editor-skill\ndescription: Editor Skill\n---\n",
                            "text/markdown",
                        ),
                    ),
                ),
            ),
            execute=True,
            replace=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "has_current"),
    (("active", True), ("suspended", True), ("suspended", False)),
)
async def test_editor_can_delete_skill_with_or_without_current(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    has_current: bool,
) -> None:
    actor = _context(ProjectRole.EDITOR)
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        status=status,
        current_version_id=uuid.uuid4() if has_current else None,
        revision=4,
    )
    session = _Session()
    repository = SimpleNamespace(session=session)
    monkeypatch.setattr(
        skill_service_module,
        "SkillRepository",
        lambda _session: repository,
    )
    deletion = SimpleNamespace(
        delete_in_session=AsyncMock(return_value=SkillDeleteResult(0)),
    )
    service = skill_service_module.SkillService(
        lambda: session,
        governance_sink=SimpleNamespace(append_project=AsyncMock()),
    )
    service._deletion = deletion

    result = await service.delete(
        actor,
        asset.id,
        expected_asset_version=4,
    )

    assert result == SkillDeleteResult(0)
    deletion.delete_in_session.assert_awaited_once_with(
        session,
        actor,
        asset.id,
        4,
    )


@pytest.mark.asyncio
async def test_viewer_cannot_delete_skill_before_storage_is_opened() -> None:
    class _ExplodingFactory:
        def __call__(self):
            raise AssertionError("unauthorized Skill delete opened storage")

    with pytest.raises(AssetForbidden):
        await skill_service_module.SkillService(_ExplodingFactory()).delete(
            _context(ProjectRole.VIEWER),
            uuid.uuid4(),
            expected_asset_version=1,
        )
