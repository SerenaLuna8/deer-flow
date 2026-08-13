from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import (
    AssetForbidden,
    AssetNotFound,
    AssetValidationFailed,
)
from app.shared_assets.mcp_tool_inventory_repository import mcp_grant_closure_digest
from app.shared_assets.project_authoring_catalog import (
    MAX_AUTHORING_MCP_SCAN_PER_PAGE,
    McpToolCatalogSearch,
    McpToolCatalogSearchResult,
    McpToolMetadataInspect,
    ProjectAuthoringCatalogRepository,
    ProjectAuthoringCatalogTools,
    SkillCatalogItem,
    SkillCatalogSearch,
    SkillCatalogSearchResult,
    SkillTextRead,
)


def _context(*capabilities: Capability) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=frozenset(capabilities),
        membership_version=7,
        request_id="request-project-authoring-catalog",
    )


def _sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_authoring_catalog_request_dtos_are_closed_and_bounded() -> None:
    with pytest.raises(ValidationError):
        SkillCatalogSearch.model_validate({"query": "review", "limit": 5, "project_id": str(uuid.uuid4())})
    with pytest.raises(ValidationError):
        SkillCatalogSearch.model_validate({"query": "review", "limit": "5"})
    assert SkillCatalogSearch(query=" ").query == ""
    assert McpToolCatalogSearch(query="").query == ""
    with pytest.raises(ValidationError):
        SkillCatalogSearch(query="review", cursor="x" * 2049)
    with pytest.raises(ValidationError):
        SkillTextRead(
            skill_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            path="../SKILL.md",
        )
    with pytest.raises(ValidationError):
        SkillTextRead(
            skill_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            path=".",
        )
    with pytest.raises(ValidationError):
        McpToolMetadataInspect(
            mcp_server_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            tool_name="tools/call",
        )


@pytest.mark.asyncio
async def test_skill_search_query_uses_only_current_or_exact_bound_versions() -> None:
    context = _context(Capability.SHARED_ASSETS_READ)
    rows = [
        SimpleNamespace(
            scope="project",
            skill_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            version_number=3,
            slug="code-review",
            display_name="代码\x00审查",
            description="审查\n代码",
            payload_checksum="a" * 64,
        ),
        SimpleNamespace(
            scope="system",
            skill_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            version_number=2,
            slug="research",
            display_name="研究",
            description="检索资料",
            payload_checksum="b" * 64,
        ),
    ]

    class _Result:
        def all(self) -> list[object]:
            return rows

    class _Session:
        statement: object | None = None

        async def execute(self, statement: object) -> _Result:
            self.statement = statement
            return _Result()

    session = _Session()
    result = await ProjectAuthoringCatalogRepository(  # type: ignore[arg-type]
        session
    ).search_skills(
        context,
        SkillCatalogSearch(query="review", limit=1),
    )

    assert result.truncated is True
    assert result.next_cursor is not None
    assert "code-review" not in result.next_cursor
    assert len(result.items) == 1
    assert result.items[0].display_name == "代码审查"
    assert result.items[0].description == "审查 代码"
    assert result.items[0].authoring_only is True
    assert session.statement is not None
    sql = _sql(session.statement)
    assert "skills.status = 'active'" in sql
    assert "skills.current_published_version_id = skill_versions.id" in sql
    assert "skill_versions.workflow_status = 'published'" in sql
    assert "skill_versions.revoked_at IS NULL" in sql
    assert "project_system_skill_bindings.enabled IS true" in sql
    assert "skill_versions.id = project_system_skill_bindings.skill_version_id" in sql
    assert "project_memberships.user_id" in sql
    assert "project_memberships.version = 7" in sql
    assert "LIMIT 2" in sql
    assert "credential_" not in sql


@pytest.mark.asyncio
async def test_exact_skill_text_read_revalidates_scope_and_bytes() -> None:
    context = _context(Capability.SHARED_ASSETS_READ)
    skill_id = uuid.uuid4()
    version_id = uuid.uuid4()
    content = b"---\nname: code-review\ndescription: Review code\n---\n"
    row = SimpleNamespace(
        scope="system",
        skill_id=skill_id,
        version_id=version_id,
        payload_checksum="c" * 64,
        path="SKILL.md",
        media_type="text/markdown",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )

    class _Result:
        @staticmethod
        def one_or_none() -> object:
            return row

    class _Session:
        statement: object | None = None

        async def execute(self, statement: object) -> _Result:
            self.statement = statement
            return _Result()

    session = _Session()
    result = await ProjectAuthoringCatalogRepository(  # type: ignore[arg-type]
        session
    ).read_skill_text(
        context,
        SkillTextRead(
            skill_id=skill_id,
            version_id=version_id,
            path="SKILL.md",
        ),
    )

    assert result.content == content.decode()
    assert result.authoring_only is True
    assert session.statement is not None
    sql = _sql(session.statement)
    assert "project_system_skill_bindings.enabled IS true" in sql
    assert "skill_versions.revoked_at IS NULL" in sql
    assert "skill_version_files.path = 'SKILL.md'" in sql
    assert str(skill_id) in sql
    assert str(version_id) in sql
    assert "skill_version_files.size_bytes <= 1048576" in sql


@pytest.mark.asyncio
async def test_exact_skill_text_read_rejects_corrupt_content() -> None:
    context = _context(Capability.SHARED_ASSETS_READ)
    row = SimpleNamespace(
        scope="project",
        skill_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        payload_checksum="d" * 64,
        path="SKILL.md",
        media_type="text/markdown",
        size_bytes=4,
        sha256="e" * 64,
        content=b"text",
    )

    class _Result:
        @staticmethod
        def one_or_none() -> object:
            return row

    class _Session:
        async def execute(self, _statement: object) -> _Result:
            return _Result()

    with pytest.raises(AssetValidationFailed):
        await ProjectAuthoringCatalogRepository(  # type: ignore[arg-type]
            _Session()
        ).read_skill_text(
            context,
            SkillTextRead(
                skill_id=row.skill_id,
                version_id=row.version_id,
                path="SKILL.md",
            ),
        )


@pytest.mark.asyncio
async def test_mcp_search_reads_only_current_cached_inventory_metadata() -> None:
    context = _context(Capability.SHARED_ASSETS_READ)
    server_id = uuid.uuid4()
    version_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    grant_digest = mcp_grant_closure_digest((grant_id,))
    now = datetime.now(UTC)
    tool_row = SimpleNamespace(
        scope="system",
        scope_rank=1,
        mcp_server_id=server_id,
        version_id=version_id,
        version_number=4,
        server_slug="issue-tracker",
        server_name="Issue\x00Tracker",
        server_description="项目\n问题管理",
        payload_checksum="f" * 64,
        tool_name="search_issues",
        tool_description="Search\nproject issues",
        attempt_payload_checksum="f" * 64,
        attempt_grant_digest=grant_digest,
        attempt_status="ready",
        public_error_code=None,
        tools_payload_checksum="f" * 64,
        tools_grant_digest=grant_digest,
        last_success_at=now,
    )
    grant_row = SimpleNamespace(
        mcp_server_version_id=version_id,
        id=grant_id,
    )

    class _Result:
        def __init__(self, rows: list[object]) -> None:
            self._rows = rows

        def all(self) -> list[object]:
            return self._rows

    class _Session:
        statements: list[object]

        def __init__(self) -> None:
            self.statements = []

        async def execute(self, statement: object) -> _Result:
            self.statements.append(statement)
            return _Result([tool_row] if len(self.statements) == 1 else [grant_row])

    session = _Session()
    result = await ProjectAuthoringCatalogRepository(  # type: ignore[arg-type]
        session
    ).search_mcp_tools(
        context,
        McpToolCatalogSearch(query="issues", limit=5),
    )

    assert len(result.items) == 1
    item = result.items[0]
    assert item.server_name == "IssueTracker"
    assert item.server_description == "项目 问题管理"
    assert item.tool_description == "Search project issues"
    assert item.inventory_status == "ready"
    assert item.authoring_only is True
    assert (
        result.model_dump()["items"][0]
        .keys()
        .isdisjoint(
            {
                "credential",
                "credential_grant_id",
                "dependency",
                "endpoint",
                "headers",
                "url",
            }
        )
    )
    assert len(session.statements) == 2
    catalog_sql = _sql(session.statements[0])
    grant_sql = _sql(session.statements[1])
    assert "mcp_servers.status = 'active'" in catalog_sql
    assert "mcp_servers.current_published_version_id = mcp_server_versions.id" in catalog_sql
    assert "project_system_mcp_bindings.enabled IS true" in catalog_sql
    assert "mcp_server_versions.id = project_system_mcp_bindings.mcp_server_version_id" in catalog_sql
    assert "project_mcp_tool_inventories.project_id" in catalog_sql
    assert "JOIN LATERAL jsonb_array_elements" in catalog_sql
    assert "project_mcp_tool_inventories.tools_payload_checksum" in catalog_sql
    assert f"LIMIT {MAX_AUTHORING_MCP_SCAN_PER_PAGE + 1}" in catalog_sql
    assert "credential_envelopes" not in catalog_sql
    assert "credential_versions" not in catalog_sql
    assert "ciphertext" not in catalog_sql
    assert "credential_grants.id" in grant_sql
    assert "credential_grants.status = 'active'" in grant_sql
    assert "credential_versions" not in grant_sql


@pytest.mark.asyncio
async def test_empty_skill_query_lists_and_cursor_is_bound_to_query() -> None:
    context = _context(Capability.SHARED_ASSETS_READ)
    rows = [
        SimpleNamespace(
            scope="project",
            scope_rank=0,
            match_rank=0,
            sort_slug="alpha",
            skill_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            version_number=1,
            slug="alpha",
            display_name="Alpha",
            description="Alpha skill",
            payload_checksum="a" * 64,
        ),
        SimpleNamespace(
            scope="system",
            scope_rank=1,
            match_rank=0,
            sort_slug="beta",
            skill_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            version_number=1,
            slug="beta",
            display_name="Beta",
            description="Beta skill",
            payload_checksum="b" * 64,
        ),
    ]

    class _Result:
        def all(self) -> list[object]:
            return rows

    class _Session:
        statement: object | None = None

        async def execute(self, statement: object) -> _Result:
            self.statement = statement
            return _Result()

    session = _Session()
    repository = ProjectAuthoringCatalogRepository(session)  # type: ignore[arg-type]
    first = await repository.search_skills(
        context,
        SkillCatalogSearch(query="", limit=1),
    )

    assert tuple(item.slug for item in first.items) == ("alpha",)
    assert first.truncated is True
    assert first.next_cursor is not None
    assert session.statement is not None
    first_sql = _sql(session.statement)
    assert " LIKE " not in first_sql
    assert "ORDER BY" in first_sql

    with pytest.raises(AssetValidationFailed):
        await repository.search_skills(
            context,
            SkillCatalogSearch(
                query="different-query",
                limit=1,
                cursor=first.next_cursor,
            ),
        )


@pytest.mark.asyncio
async def test_mcp_cursor_can_page_past_scan_budget_without_unreachable_tools() -> None:
    context = _context(Capability.SHARED_ASSETS_READ)
    server_id = uuid.uuid4()
    version_id = uuid.uuid4()
    now = datetime.now(UTC)
    current_digest = mcp_grant_closure_digest(())

    def _row(index: int, *, current: bool) -> SimpleNamespace:
        digest = current_digest if current else "stale-grant-closure"
        return SimpleNamespace(
            scope="project",
            scope_rank=0,
            match_rank=0,
            sort_server_slug="catalog",
            sort_tool_name=f"tool_{index:03d}",
            mcp_server_id=server_id,
            version_id=version_id,
            version_number=1,
            server_slug="catalog",
            server_name="Catalog",
            server_description="Catalog service",
            payload_checksum="c" * 64,
            tool_name=f"tool_{index:03d}",
            tool_description=f"Tool {index}",
            attempt_payload_checksum="c" * 64,
            attempt_grant_digest=digest,
            attempt_status="ready",
            public_error_code=None,
            tools_payload_checksum="c" * 64,
            tools_grant_digest=digest,
            last_success_at=now,
        )

    first_rows = [_row(index, current=False) for index in range(MAX_AUTHORING_MCP_SCAN_PER_PAGE + 1)]

    class _Result:
        def __init__(self, values: list[object]) -> None:
            self._values = values

        def all(self) -> list[object]:
            return self._values

    class _Session:
        def __init__(self, tool_rows: list[object]) -> None:
            self.tool_rows = tool_rows
            self.statements: list[object] = []

        async def execute(self, statement: object) -> _Result:
            self.statements.append(statement)
            return _Result(self.tool_rows if len(self.statements) == 1 else [])

    first_session = _Session(first_rows)
    first = await ProjectAuthoringCatalogRepository(  # type: ignore[arg-type]
        first_session
    ).search_mcp_tools(
        context,
        McpToolCatalogSearch(query="", limit=1),
    )

    assert first.items == ()
    assert first.truncated is True
    assert first.next_cursor is not None
    assert "tool_255" not in first.next_cursor

    second_session = _Session(
        [_row(MAX_AUTHORING_MCP_SCAN_PER_PAGE, current=True)],
    )
    second = await ProjectAuthoringCatalogRepository(  # type: ignore[arg-type]
        second_session
    ).search_mcp_tools(
        context,
        McpToolCatalogSearch(
            query="",
            limit=1,
            cursor=first.next_cursor,
        ),
    )

    assert tuple(item.tool_name for item in second.items) == (f"tool_{MAX_AUTHORING_MCP_SCAN_PER_PAGE:03d}",)
    assert second.truncated is False
    assert second.next_cursor is None
    second_sql = _sql(second_session.statements[0])
    assert "ORDER BY" in second_sql
    assert ") > (" in second_sql


@pytest.mark.asyncio
async def test_mcp_inspect_rejects_inventory_from_a_stale_grant_closure() -> None:
    context = _context(Capability.SHARED_ASSETS_READ)
    server_id = uuid.uuid4()
    version_id = uuid.uuid4()
    stale_digest = mcp_grant_closure_digest((uuid.uuid4(),))
    row = SimpleNamespace(
        scope="project",
        scope_rank=0,
        mcp_server_id=server_id,
        version_id=version_id,
        version_number=1,
        server_slug="search",
        server_name="Search",
        server_description="Search service",
        payload_checksum="1" * 64,
        tool_name="search",
        tool_description="Search",
        attempt_payload_checksum="1" * 64,
        attempt_grant_digest=stale_digest,
        attempt_status="ready",
        public_error_code=None,
        tools_payload_checksum="1" * 64,
        tools_grant_digest=stale_digest,
        last_success_at=datetime.now(UTC),
    )

    class _Result:
        def __init__(self, rows: list[object]) -> None:
            self._rows = rows

        def one_or_none(self) -> object | None:
            return self._rows[0] if self._rows else None

        def all(self) -> list[object]:
            return self._rows

    class _Session:
        calls = 0

        async def execute(self, _statement: object) -> _Result:
            self.calls += 1
            return _Result([row] if self.calls == 1 else [])

    with pytest.raises(AssetNotFound):
        await ProjectAuthoringCatalogRepository(  # type: ignore[arg-type]
            _Session()
        ).inspect_mcp_tool(
            context,
            McpToolMetadataInspect(
                mcp_server_id=server_id,
                version_id=version_id,
                tool_name="search",
            ),
        )


class _TransactionSession:
    def begin(self) -> _TransactionSession:
        return self

    async def __aenter__(self) -> _TransactionSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def test_builder_tools_require_read_capability_before_opening_storage() -> None:
    context = _context(Capability.SHARED_ASSETS_EDIT)

    with pytest.raises(AssetForbidden):
        ProjectAuthoringCatalogTools(
            lambda: (_ for _ in ()).throw(AssertionError("must not open storage")),  # type: ignore[arg-type]
            context,
        )


@pytest.mark.asyncio
async def test_builder_tools_keep_server_context_fixed_and_expose_only_reads() -> None:
    context = _context(Capability.SHARED_ASSETS_READ)
    session = _TransactionSession()
    item = SkillCatalogItem(
        scope="project",
        skill_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        version_number=1,
        slug="review",
        display_name="Review",
        description="Review code",
        payload_checksum="2" * 64,
    )

    class _Repository:
        async def search_skills(
            self,
            received_context: ProjectContext,
            request: SkillCatalogSearch,
        ) -> SkillCatalogSearchResult:
            assert received_context is context
            return SkillCatalogSearchResult(
                query=request.query,
                items=(item,),
                truncated=False,
            )

        async def read_skill_text(self, *_args: object) -> object:
            raise AssertionError("not called")

        async def search_mcp_tools(self, *_args: object) -> McpToolCatalogSearchResult:
            raise AssertionError("not called")

        async def inspect_mcp_tool(self, *_args: object) -> object:
            raise AssertionError("not called")

    tools = ProjectAuthoringCatalogTools(
        lambda: session,  # type: ignore[arg-type]
        context,
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
    )

    result = await tools.search_skills(SkillCatalogSearch(query="review"))

    assert result.items == (item,)
    for forbidden_method in (
        "activate_skill",
        "discover_mcp_tools",
        "execute_mcp_tool",
        "read_credential",
        "record_runtime_dependency",
        "run_shell",
        "write_file",
    ):
        assert not hasattr(tools, forbidden_method)
    with pytest.raises(AssetValidationFailed):
        await tools.search_skills({"query": "review"})  # type: ignore[arg-type]
