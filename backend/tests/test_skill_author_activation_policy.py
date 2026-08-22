from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import skill_service as skill_service_module
from app.shared_assets.errors import AssetInUse
from app.shared_assets.models import SkillArchiveFile
from app.shared_assets.skill_repository import SkillRepository


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
@pytest.mark.parametrize("status", ["active", "suspended"])
async def test_editor_can_delete_skill_with_or_without_current(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    actor = _context(ProjectRole.EDITOR)
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        status=status,
        current_version_id=uuid.uuid4(),
        revision=4,
    )
    session = _Session()
    repository = SimpleNamespace(
        session=session,
        lock_project_delete_scope=AsyncMock(),
        get_project_asset=AsyncMock(return_value=asset),
        plan_project_asset_deletion=AsyncMock(return_value=()),
        delete_project_asset=AsyncMock(),
    )
    monkeypatch.setattr(
        skill_service_module,
        "SkillRepository",
        lambda _session: repository,
    )

    await skill_service_module.SkillService(
        lambda: session,
        governance_sink=SimpleNamespace(append_project=AsyncMock()),
    ).delete(actor, asset.id, expected_asset_version=4)

    repository.plan_project_asset_deletion.assert_awaited_once_with(actor, asset)
    repository.delete_project_asset.assert_awaited_once_with(actor, asset, ())


@pytest.mark.asyncio
async def test_editor_can_delete_skill_without_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context(ProjectRole.EDITOR)
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        status="suspended",
        current_version_id=None,
        revision=2,
    )
    session = _Session()
    repository = SimpleNamespace(
        session=session,
        lock_project_delete_scope=AsyncMock(),
        get_project_asset=AsyncMock(return_value=asset),
        plan_project_asset_deletion=AsyncMock(return_value=()),
        delete_project_asset=AsyncMock(),
    )
    monkeypatch.setattr(
        skill_service_module,
        "SkillRepository",
        lambda _session: repository,
    )

    await skill_service_module.SkillService(
        lambda: session,
        governance_sink=SimpleNamespace(append_project=AsyncMock()),
    ).delete(actor, asset.id, expected_asset_version=2)

    repository.plan_project_asset_deletion.assert_awaited_once_with(actor, asset)
    repository.delete_project_asset.assert_awaited_once_with(actor, asset, ())


class _VersionRows:
    def __init__(self, version_ids: tuple[uuid.UUID, ...]) -> None:
        self._version_ids = version_ids

    def scalars(self):
        return self

    def all(self) -> list[uuid.UUID]:
        return list(self._version_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_reference_exists", "run_reference_exists"),
    [(True, False), (False, True)],
)
async def test_referenced_skill_delete_uses_safe_in_use_error(
    agent_reference_exists: bool,
    run_reference_exists: bool,
) -> None:
    actor = _context(ProjectRole.ADMIN)
    version_id = uuid.uuid4()
    session = SimpleNamespace(
        execute=AsyncMock(return_value=_VersionRows((version_id,))),
        scalar=AsyncMock(
            side_effect=(agent_reference_exists, run_reference_exists),
        ),
    )
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
    )

    with pytest.raises(AssetInUse) as exc_info:
        await SkillRepository(session).plan_project_asset_deletion(actor, asset)

    assert exc_info.value.request_id == actor.request_id
    assert exc_info.value.code == "ASSET_IN_USE"
    assert exc_info.value.public_message == "Asset is still referenced"
