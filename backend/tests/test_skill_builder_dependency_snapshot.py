from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetNotFound
from app.shared_assets.project_authoring_catalog import (
    McpToolCatalogItem,
    McpToolMetadata,
    ProjectAuthoringCatalogRepository,
)
from app.shared_assets.skill_design_generation import (
    SkillBuilderDependencySnapshot,
    SkillBuilderMcpToolDependency,
    SkillBuilderSkillDependency,
)


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=frozenset(
            {
                Capability.SHARED_ASSETS_READ,
                Capability.SHARED_ASSETS_EDIT,
            }
        ),
        membership_version=3,
        request_id="request-builder-dependencies",
    )


def _skill_dependency() -> SkillBuilderSkillDependency:
    return SkillBuilderSkillDependency(
        reference="skill:system:code-review:v2",
        scope="system",
        skill_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        version_number=2,
        slug="code-review",
        display_name="Code Review",
        payload_checksum="a" * 64,
    )


def _mcp_dependency() -> SkillBuilderMcpToolDependency:
    return SkillBuilderMcpToolDependency(
        reference="mcp:project:docs:v3:search_docs",
        scope="project",
        mcp_server_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        version_number=3,
        server_slug="docs",
        server_name="Docs",
        tool_name="search_docs",
        payload_checksum="b" * 64,
        inventory_status="ready",
        inventory_error_code=None,
        last_success_at=datetime.now(UTC),
    )


def test_dependency_snapshot_rejects_model_spoofable_identity_shapes() -> None:
    dependency = _skill_dependency()
    with pytest.raises(ValidationError):
        SkillBuilderSkillDependency.model_validate(
            {
                **dependency.model_dump(mode="json"),
                "reference": "skill:system:different:v2",
            }
        )
    with pytest.raises(ValidationError):
        SkillBuilderDependencySnapshot(
            draft_checksum="c" * 64,
            requirements=(dependency, dependency),
        )


@pytest.mark.asyncio
async def test_skill_dependency_revalidation_requires_current_exact_visibility() -> None:
    context = _context()
    dependency = _skill_dependency()
    row = SimpleNamespace(
        scope=dependency.scope,
        skill_id=dependency.skill_id,
        version_id=dependency.version_id,
        version_number=dependency.version_number,
        slug=dependency.slug,
        display_name=dependency.display_name,
        payload_checksum=dependency.payload_checksum,
    )

    class _Result:
        def one_or_none(self) -> object:
            return row

    class _Session:
        async def execute(self, statement: object) -> _Result:
            del statement
            return _Result()

    snapshot = SkillBuilderDependencySnapshot(
        draft_checksum="c" * 64,
        requirements=(dependency,),
    )
    revalidated = await ProjectAuthoringCatalogRepository(  # type: ignore[arg-type]
        _Session()
    ).revalidate_dependency_snapshot(context, snapshot)
    assert revalidated == snapshot

    row.payload_checksum = "d" * 64
    with pytest.raises(AssetNotFound):
        await ProjectAuthoringCatalogRepository(  # type: ignore[arg-type]
            _Session()
        ).revalidate_dependency_snapshot(context, snapshot)


@pytest.mark.asyncio
async def test_mcp_dependency_revalidation_refreshes_inventory_without_granting() -> None:
    context = _context()
    dependency = _mcp_dependency()
    refreshed_at = datetime.now(UTC)
    item = McpToolCatalogItem(
        scope=dependency.scope,
        mcp_server_id=dependency.mcp_server_id,
        version_id=dependency.version_id,
        version_number=dependency.version_number,
        server_slug=dependency.server_slug,
        server_name=dependency.server_name,
        server_description="Cached metadata",
        payload_checksum=dependency.payload_checksum,
        tool_name=dependency.tool_name,
        tool_description="Search documentation",
        inventory_status="degraded",
        inventory_error_code="mcp_discovery_unavailable",
        last_success_at=refreshed_at,
    )

    class _Repository(ProjectAuthoringCatalogRepository):
        async def inspect_mcp_tool(self, current, request):  # type: ignore[no-untyped-def]
            assert current is context
            assert request.version_id == dependency.version_id
            return McpToolMetadata(item=item)

    snapshot = SkillBuilderDependencySnapshot(
        draft_checksum="e" * 64,
        requirements=(dependency,),
    )
    revalidated = await _Repository(object()).revalidate_dependency_snapshot(  # type: ignore[arg-type]
        context,
        snapshot,
    )
    current = revalidated.requirements[0]
    assert isinstance(current, SkillBuilderMcpToolDependency)
    assert current.inventory_status == "degraded"
    assert current.last_success_at == refreshed_at
    assert current.authoring_only is True
    assert current.runtime_authorized is False
