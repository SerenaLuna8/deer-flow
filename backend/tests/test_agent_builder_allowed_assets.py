from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_design_repository import (
    AgentDesignAllowedAssetRecord,
    AgentDesignRepository,
)
from app.shared_assets.agent_design_service import (
    AgentDesignMessageTurn,
    AgentDesignService,
    AgentDesignStatus,
    SubmitAgentDesignTurn,
)
from app.shared_assets.errors import AssetForbidden
from deerflow.persistence.shared_assets import AgentDesignSessionRow


def _context(*capabilities: Capability) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=frozenset(capabilities),
        membership_version=7,
        request_id="request-agent-builder-assets",
    )


class _TransactionSession:
    def begin(self) -> _TransactionSession:
        return self

    async def __aenter__(self) -> _TransactionSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_prepare_turn_injects_only_server_loaded_exact_asset_versions() -> None:
    now = datetime.now(UTC)
    project_skill_id = uuid.uuid4()
    project_skill_version_id = uuid.uuid4()
    system_mcp_id = uuid.uuid4()
    system_mcp_version_id = uuid.uuid4()
    records = (
        AgentDesignAllowedAssetRecord(
            kind="mcp",
            scope="system",
            asset_id=system_mcp_id,
            version_id=system_mcp_version_id,
            name="发布服务",
            slug="release-service",
            description="执行受管发布流程",
        ),
        AgentDesignAllowedAssetRecord(
            kind="skill",
            scope="project",
            asset_id=project_skill_id,
            version_id=project_skill_version_id,
            name="代码审查",
            slug="code-review",
            description="审查代码正确性与安全风险",
        ),
    )
    row = AgentDesignSessionRow(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        thread_id=uuid.uuid4(),
        slug="reviewer",
        display_name="审查助手",
        status=AgentDesignStatus.INTERVIEWING.value,
        revision=1,
        messages_json=[],
        progress_json=[],
        active_clarification_json=None,
        blueprint_json=None,
        blueprint_checksum=None,
        error_code=None,
        error_message=None,
        created_agent_id=None,
        created_agent_version_id=None,
        create_idempotency_key_hash="a" * 64,
        create_request_checksum="b" * 64,
        created_at=now,
        updated_at=now,
    )
    context = _context(
        Capability.SHARED_ASSETS_READ,
        Capability.SHARED_ASSETS_EDIT,
    )
    row.project_id = context.project_id
    row.owner_user_id = str(context.user_id)
    fake_session = _TransactionSession()

    class _Repository:
        session = fake_session

        async def get_operation(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def get(self, *_args: object, **_kwargs: object) -> AgentDesignSessionRow:
            return row

        async def create_operation(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def list_allowed_assets(
            self,
            received_context: ProjectContext,
            *,
            limit: int,
        ) -> tuple[AgentDesignAllowedAssetRecord, ...]:
            assert received_context is context
            assert limit == 50
            return records

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        generator=SimpleNamespace(),  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
    )
    command = SubmitAgentDesignTurn(
        input=AgentDesignMessageTurn(
            kind="message",
            message="设计一个代码审查助手",
        ),
        expected_revision=1,
        idempotency_key="turn-1",
    )

    prepared = await service._prepare_turn(  # noqa: SLF001 - focused orchestration contract
        context,
        row.id,
        command,
        operation_hash="c" * 64,
        request_checksum="d" * 64,
    )

    assert isinstance(prepared, tuple)
    generation_context = prepared[2]
    assert [(asset.kind, asset.scope, asset.asset_id, asset.version_id, asset.enabled) for asset in generation_context.allowed_assets] == [
        (
            "skill",
            "project",
            project_skill_id,
            project_skill_version_id,
            True,
        ),
        (
            "mcp",
            "system",
            system_mcp_id,
            system_mcp_version_id,
            True,
        ),
    ]


@pytest.mark.asyncio
async def test_submit_turn_requires_shared_asset_read_before_loading_catalog() -> None:
    context = _context(Capability.SHARED_ASSETS_EDIT)
    service = AgentDesignService(
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),  # type: ignore[arg-type]
        generator=SimpleNamespace(),  # type: ignore[arg-type]
    )

    with pytest.raises(AssetForbidden):
        await service.submit_turn(
            context,
            uuid.uuid4(),
            SubmitAgentDesignTurn(
                input=AgentDesignMessageTurn(kind="message", message="创建助手"),
                expected_revision=1,
                idempotency_key="turn-read-boundary",
            ),
        )


@pytest.mark.asyncio
async def test_allowed_asset_query_is_bounded_and_filters_unusable_versions() -> None:
    context = _context(Capability.SHARED_ASSETS_READ)
    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()

    class _Result:
        @staticmethod
        def all() -> list[object]:
            return [
                SimpleNamespace(
                    kind="skill",
                    scope="project",
                    asset_id=skill_id,
                    version_id=skill_version_id,
                    name="代码审查",
                    slug="code-review",
                    description="审查代码",
                )
            ]

    class _Session:
        statement: object | None = None

        async def execute(self, statement: object) -> _Result:
            self.statement = statement
            return _Result()

    session = _Session()
    records = await AgentDesignRepository(session).list_allowed_assets(  # type: ignore[arg-type]
        context,
        limit=50,
    )

    assert records == (
        AgentDesignAllowedAssetRecord(
            kind="skill",
            scope="project",
            asset_id=skill_id,
            version_id=skill_version_id,
            name="代码审查",
            slug="code-review",
            description="审查代码",
        ),
    )
    assert session.statement is not None
    sql = str(
        session.statement.compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "skills.status = 'active'" in sql
    assert "skill_versions.workflow_status = 'published'" in sql
    assert "skills.current_published_version_id = skill_versions.id" in sql
    assert "project_system_skill_bindings.enabled IS true" in sql
    assert "mcp_servers.status = 'active'" in sql
    assert "mcp_server_versions.workflow_status = 'published'" in sql
    assert "mcp_servers.current_published_version_id = mcp_server_versions.id" in sql
    assert "project_system_mcp_bindings.enabled IS true" in sql
    assert "project_memberships.version = 7" in sql
    assert "ORDER BY" in sql
    assert "LIMIT 50" in sql
