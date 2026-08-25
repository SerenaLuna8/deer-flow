from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import PrivateWorkContext
from app.private_work.snapshot_repository import RunSnapshotRepository
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    AssetSelection,
    ResolvedAgentSnapshot,
    ResolvedRunAssetFact,
)
from app.shared_assets.resolver import (
    BUILTIN_MAIN_AGENT_SOURCE_KEY,
    ProjectAssetResolver,
    _ResolvedRecord,
)
from deerflow.persistence.shared_assets import (
    AgentRow,
    McpServerRow,
    McpServerVersionRow,
    SkillRow,
    SkillVersionRow,
)


def _private_context() -> PrivateWorkContext:
    role = ProjectRole.ADMIN
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
            project_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
            membership_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
            role=role,
            capabilities=capabilities_for(role),
            membership_version=1,
            request_id="run-asset-facts",
        )
    )


def _project_context() -> ProjectContext:
    private = _private_context()
    return ProjectContext(
        user_id=private.user_id,
        project_id=private.project_id,
        membership_id=private.membership_id,
        role=private.role,
        capabilities=private.capabilities,
        membership_version=private.membership_version,
        request_id=private.request_id,
    )


def _agent_snapshot(
    record: _ResolvedRecord,
    *,
    checksum: str,
    skill_version_ids: tuple[uuid.UUID, ...] = (),
    mcp_version_ids: tuple[uuid.UUID, ...] = (),
) -> ResolvedAgentSnapshot:
    return ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=record.scope,
        asset_id=record.asset.id,
        version_id=record.version_id,
        checksum=checksum,
        catalog_generation=0,
        dependency_version_ids=(*skill_version_ids, *mcp_version_ids),
        payload=AgentPayload(
            description="fact fixture",
            soul="",
            model_ref="default",
            tool_groups=(),
            skill_refs=(),
            mcp_version_ids=mcp_version_ids,
        ),
        skill_version_ids=skill_version_ids,
    )


def _agent_record(
    asset_int: int,
    version_int: int,
    *,
    scope: AssetScope,
    checksum: str,
    source_key: str | None = None,
) -> _ResolvedRecord:
    agent = AgentRow(
        id=uuid.UUID(int=asset_int),
        scope=scope.value,
        source_key=source_key,
        definition_id=uuid.UUID(int=version_int),
        payload_checksum=checksum,
    )
    return _ResolvedRecord(
        scope,
        agent,
        agent,
    )


def _skill_record(asset_int: int, version_int: int, *, checksum: str) -> _ResolvedRecord:
    return _ResolvedRecord(
        AssetScope.PROJECT,
        SkillRow(
            id=uuid.UUID(int=asset_int),
            scope=AssetScope.PROJECT.value,
            slug="main-skill",
        ),
        SkillVersionRow(id=uuid.UUID(int=version_int), payload_checksum=checksum),
    )


def _mcp_record(
    asset_id: uuid.UUID,
    version_int: int,
    *,
    checksum: str,
) -> _ResolvedRecord:
    return _ResolvedRecord(
        AssetScope.PROJECT,
        McpServerRow(id=asset_id, scope=AssetScope.PROJECT.value),
        McpServerVersionRow(
            id=uuid.UUID(int=version_int),
            payload_checksum=checksum,
        ),
    )


def _fact(
    kind: AssetKind,
    dependency_order: int,
    record: _ResolvedRecord,
    *,
    checksum: str,
) -> ResolvedRunAssetFact:
    return ResolvedRunAssetFact(
        kind=kind,
        dependency_order=dependency_order,
        scope=record.scope,
        asset_id=record.asset.id,
        version_id=record.version_id,
        checksum=checksum,
        catalog_generation=12,
    )


class _MetadataSession:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.row = row
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return SimpleNamespace(all=lambda: [self.row])


@pytest.mark.asyncio
async def test_frozen_run_asset_facts_are_scoped_and_do_not_load_snapshot_json() -> None:
    context = _private_context()
    thread_id = "thread-1"
    run_id = "run-1"
    asset_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
    version_id = uuid.UUID("55555555-5555-4555-8555-555555555555")
    session = _MetadataSession(
        (
            AssetKind.AGENT.value,
            0,
            AssetScope.PROJECT.value,
            asset_id,
            version_id,
            "a" * 64,
            7,
        )
    )
    repository = RunSnapshotRepository(lambda: None)  # type: ignore[arg-type]

    facts = await repository.list_asset_facts_in_session(  # type: ignore[arg-type]
        session,
        context,
        thread_id,
        run_id,
    )

    assert facts == (
        ResolvedRunAssetFact(
            kind=AssetKind.AGENT,
            dependency_order=0,
            scope=AssetScope.PROJECT,
            asset_id=asset_id,
            version_id=version_id,
            checksum="a" * 64,
            catalog_generation=7,
        ),
    )
    assert session.statement is not None
    assert tuple(column.key for column in session.statement.selected_columns) == (
        "asset_kind",
        "dependency_order",
        "asset_scope",
        "asset_id",
        "version_id",
        "payload_checksum",
        "catalog_generation",
    )
    compiled = session.statement.compile()
    assert "snapshot_json" not in str(compiled)
    assert context.project_id in compiled.params.values()
    assert str(context.user_id) in compiled.params.values()
    assert thread_id in compiled.params.values()
    assert run_id in compiled.params.values()


@pytest.mark.asyncio
async def test_current_run_asset_facts_preserve_main_delegate_and_runtime_checksum_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lead_record = _agent_record(
        1,
        2,
        scope=AssetScope.SYSTEM,
        checksum="1" * 64,
        source_key=BUILTIN_MAIN_AGENT_SOURCE_KEY,
    )
    delegate_record = _agent_record(
        3,
        4,
        scope=AssetScope.PROJECT,
        checksum="2" * 64,
    )
    stale_delegate_record = _agent_record(
        5,
        6,
        scope=AssetScope.PROJECT,
        checksum="3" * 64,
    )
    skill_record = _skill_record(7, 8, checksum="b" * 64)
    mcp_asset_id = uuid.UUID(int=9)
    main_mcp_record = _mcp_record(mcp_asset_id, 10, checksum="c" * 64)
    delegate_only_mcp_record = _mcp_record(
        mcp_asset_id,
        11,
        checksum="d" * 64,
    )
    lead_snapshot = _agent_snapshot(
        lead_record,
        checksum="a" * 64,
    )
    delegate_snapshot = _agent_snapshot(
        delegate_record,
        checksum="9" * 64,
        skill_version_ids=(skill_record.version.id,),
        mcp_version_ids=(delegate_only_mcp_record.version.id,),
    )

    class _FactsResolver(ProjectAssetResolver):
        async def _resolve_record(self, *_args, **_kwargs):
            return lead_record

        async def _agent_snapshot_with_dependencies(
            self,
            _session,
            _context,
            record,
            _generation,
        ):
            if record is lead_record:
                return lead_snapshot, (), ()
            if record is delegate_record:
                return (
                    delegate_snapshot,
                    (skill_record,),
                    (delegate_only_mcp_record,),
                )
            from app.shared_assets.errors import AssetResolutionUnavailable

            raise AssetResolutionUnavailable("run-asset-facts")

        async def _main_pool_records(self, _session, _context, kind):
            return {
                AssetKind.AGENT: (delegate_record, stale_delegate_record),
                AssetKind.SKILL: (skill_record,),
                AssetKind.MCP: (main_mcp_record,),
            }[kind]

        async def _skill_snapshot(self, *_args, **_kwargs):
            raise AssertionError("facts must not load Skill version files")

    async def _generation(_repository):
        return 12

    monkeypatch.setattr(
        "app.shared_assets.resolver.CatalogStateRepository.read_generation",
        _generation,
    )
    resolver = _FactsResolver(lambda: None)  # type: ignore[arg-type]
    session = AsyncSession()
    try:
        async with session.begin():
            facts = await resolver.resolve_run_asset_facts_in_session(
                session,
                _project_context(),
                AssetSelection(AssetKind.AGENT, lead_record.asset.id),
            )
    finally:
        await session.close()

    assert facts == (
        _fact(
            AssetKind.AGENT,
            0,
            lead_record,
            # The persisted legacy checksum differs; facts must use the v4
            # runtime checksum that Run admission would freeze.
            checksum="a" * 64,
        ),
        _fact(AssetKind.AGENT, 1, delegate_record, checksum="9" * 64),
        _fact(AssetKind.SKILL, 2, skill_record, checksum="b" * 64),
        _fact(AssetKind.MCP, 3, main_mcp_record, checksum="c" * 64),
        _fact(AssetKind.MCP, 4, delegate_only_mcp_record, checksum="d" * 64),
    )
