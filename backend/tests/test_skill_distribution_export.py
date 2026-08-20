from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.audit.models import AuditUnavailable
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import skill_service as skill_service_module
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import AssetForbidden, AssetStorageUnavailable
from app.shared_assets.models import SkillArchiveFile
from app.shared_assets.skill_service import SkillService


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Session:
    def begin(self) -> _Transaction:
        return _Transaction()

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _SessionFactory:
    def __init__(self) -> None:
        self.sessions: list[_Session] = []

    def __call__(self) -> _Session:
        session = _Session()
        self.sessions.append(session)
        return session


def _project_context(role: ProjectRole = ProjectRole.EDITOR) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-skill-export",
    )


def _files() -> tuple[SkillArchiveFile, ...]:
    return (
        SkillArchiveFile(
            "SKILL.md",
            b"---\nname: meeting-brief\ndescription: Summarize meetings safely\n---\n",
            "text/markdown",
        ),
        SkillArchiveFile("scripts/run.py", b"print('ok')\n", "text/x-python"),
        SkillArchiveFile("evals/case.json", b"{}\n", "application/json"),
    )


def _content_record(
    *,
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    scope: str,
    project_id: uuid.UUID | None,
):
    files = _files()
    rows = tuple(
        sorted(
            (
                SimpleNamespace(
                    path=item.path,
                    content=item.content,
                    media_type=item.media_type,
                    size_bytes=len(item.content),
                    sha256=hashlib.sha256(item.content).hexdigest(),
                )
                for item in files
            ),
            key=lambda item: item.path,
        )
    )
    checksum = skill_service_module._snapshot_checksum(  # noqa: SLF001 - exact persistence contract
        skill_service_module._file_views(  # noqa: SLF001
            tuple(sorted(files, key=lambda item: item.path))
        )
    )
    return SimpleNamespace(
        asset=SimpleNamespace(
            id=asset_id,
            slug="meeting-brief",
            scope=scope,
            project_id=project_id,
        ),
        version=SimpleNamespace(
            id=version_id,
            skill_id=asset_id,
            version_number=7,
            workflow_status="draft" if scope == "project" else "published",
            payload_checksum=checksum,
            revoked_at=None,
            revoked_by_user_id=None,
            revocation_reason_code=None,
        ),
        files=rows,
    )


class _Repository:
    def __init__(self, record) -> None:
        self.record = record
        self.project_content_calls = 0
        self.project_metadata_calls = 0
        self.system_content_calls = 0
        self.system_metadata_calls = 0

    async def get_project_visible_version(self, context, asset_id, version_id):
        self.project_content_calls += 1
        return self.record

    async def get_project_visible_version_metadata(self, context, asset_id, version_id):
        self.project_metadata_calls += 1
        return self.record

    async def get_system_export_version(self, context, asset_id, version_id):
        self.system_content_calls += 1
        return self.record

    async def get_system_export_version_metadata(self, context, asset_id, version_id):
        self.system_metadata_calls += 1
        return self.record


@pytest.mark.asyncio
async def test_project_skill_export_packages_exact_selected_version_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _project_context()
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    repository = _Repository(
        _content_record(
            asset_id=asset_id,
            version_id=version_id,
            scope="project",
            project_id=context.project_id,
        )
    )
    monkeypatch.setattr(
        skill_service_module,
        "SkillRepository",
        lambda session: repository,
    )
    governance = SimpleNamespace(
        append_project=AsyncMock(),
        append_override=AsyncMock(),
    )
    service = SkillService(_SessionFactory(), governance)

    package = await service.export_distribution_package(
        context,
        asset_id,
        version_id,
    )

    assert package.filename == "meeting-brief-v7.zip"
    assert package.version_number == 7
    with zipfile.ZipFile(io.BytesIO(package.content), mode="r") as archive:
        assert archive.namelist() == ["SKILL.md", "scripts/run.py"]
    assert repository.project_content_calls == 1
    assert repository.project_metadata_calls == 1
    governance.append_project.assert_awaited_once()
    assert governance.append_project.await_args.kwargs["action"] == "skill.export"
    assert governance.append_project.await_args.kwargs["version_id"] == version_id
    governance.append_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_skill_export_rejects_members_without_authoring_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _project_context(ProjectRole.VIEWER)
    repository_factory = AsyncMock()
    monkeypatch.setattr(skill_service_module, "SkillRepository", repository_factory)
    service = SkillService(_SessionFactory())

    with pytest.raises(AssetForbidden):
        await service.export_distribution_package(
            context,
            uuid.uuid4(),
            uuid.uuid4(),
        )

    repository_factory.assert_not_called()


@pytest.mark.asyncio
async def test_global_admin_exports_only_eligible_system_skill_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SystemAssetGovernanceContext(
        user_id=uuid.uuid4(),
        request_id="req-system-skill-export",
    )
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    repository = _Repository(
        _content_record(
            asset_id=asset_id,
            version_id=version_id,
            scope="system",
            project_id=None,
        )
    )
    monkeypatch.setattr(
        skill_service_module,
        "SkillRepository",
        lambda session: repository,
    )
    governance = SimpleNamespace(
        append_project=AsyncMock(),
        append_override=AsyncMock(),
    )
    service = SkillService(_SessionFactory(), governance)

    package = await service.export_distribution_package(
        context,
        asset_id,
        version_id,
    )

    assert package.filename == "meeting-brief-v7.zip"
    assert repository.system_content_calls == 1
    assert repository.system_metadata_calls == 1
    governance.append_override.assert_awaited_once()
    assert governance.append_override.await_args.kwargs["project_id"] is None
    assert governance.append_override.await_args.kwargs["action"] == "skill.export"
    governance.append_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_skill_export_fails_closed_when_audit_cannot_be_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _project_context()
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    repository = _Repository(
        _content_record(
            asset_id=asset_id,
            version_id=version_id,
            scope="project",
            project_id=context.project_id,
        )
    )
    monkeypatch.setattr(
        skill_service_module,
        "SkillRepository",
        lambda session: repository,
    )
    governance = SimpleNamespace(
        append_project=AsyncMock(side_effect=AuditUnavailable()),
        append_override=AsyncMock(),
    )
    service = SkillService(_SessionFactory(), governance)

    with pytest.raises(AssetStorageUnavailable):
        await service.export_distribution_package(context, asset_id, version_id)

    governance.append_project.assert_awaited_once()
