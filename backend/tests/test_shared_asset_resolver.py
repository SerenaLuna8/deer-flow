from __future__ import annotations

import hashlib
import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import (
    AssetForbidden,
    AssetResolutionUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    AssetSelection,
    ResolvedAgentSnapshot,
    ResolvedMcpSnapshot,
    ResolvedRunAssetClosure,
    ResolvedSkillSnapshot,
    SkillArchiveFile,
)
from deerflow.persistence.shared_assets import AgentRow, AgentVersionRow


def _must_not_open_session():
    raise AssertionError("type validation must run before database access")


def _context(role: ProjectRole = ProjectRole.ADMIN) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=1,
        request_id="req-materialize-unit",
    )


def _resolved_agent(*, scope: AssetScope = AssetScope.PROJECT) -> ResolvedAgentSnapshot:
    return ResolvedAgentSnapshot(
        kind=AssetKind.AGENT,
        scope=scope,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum="f" * 64,
        catalog_generation=7,
        dependency_version_ids=(),
        payload=AgentPayload(
            description="agent",
            soul="agent soul",
            model_ref="test-model",
            tool_groups=(),
            skill_version_ids=(),
            mcp_version_ids=(),
        ),
    )


def test_resolved_run_asset_closure_keeps_main_dependency_boundaries() -> None:
    lead = _resolved_agent(scope=AssetScope.SYSTEM)
    delegated = _resolved_agent()
    main_skill_version_id = uuid.uuid4()
    historical_skill_version_id = uuid.uuid4()
    main_mcp_version_id = uuid.uuid4()
    historical_mcp_version_id = uuid.uuid4()
    skills = tuple(
        ResolvedSkillSnapshot(
            kind=AssetKind.SKILL,
            scope=AssetScope.PROJECT,
            asset_id=uuid.uuid4(),
            version_id=version_id,
            checksum="a" * 64,
            catalog_generation=7,
            dependency_version_ids=(),
            files=(SkillArchiveFile("SKILL.md", b"---\nname: demo\n---\n"),),
            secret_requirements=(),
        )
        for version_id in (main_skill_version_id, historical_skill_version_id)
    )
    mcps = tuple(
        ResolvedMcpSnapshot(
            kind=AssetKind.MCP,
            scope=AssetScope.PROJECT,
            asset_id=uuid.uuid4(),
            version_id=version_id,
            checksum="b" * 64,
            catalog_generation=7,
            dependency_version_ids=(),
            definition={"transport": "http"},
            credential_grant_ids=(),
        )
        for version_id in (main_mcp_version_id, historical_mcp_version_id)
    )

    closure = ResolvedRunAssetClosure(
        lead_agent=lead,
        delegated_agents=(delegated,),
        skills=skills,
        mcps=mcps,
        main_skill_version_ids=(main_skill_version_id,),
        main_mcp_version_ids=(main_mcp_version_id,),
    )

    assert closure.lead_agent is lead
    assert closure.delegated_agents == (delegated,)
    assert closure.main_skill_version_ids == (main_skill_version_id,)
    assert closure.main_mcp_version_ids == (main_mcp_version_id,)


@pytest.mark.asyncio
async def test_materializer_rejects_skill_snapshots_before_database_access() -> None:
    from app.shared_assets.resolver import ProjectAssetResolver

    resolver = ProjectAssetResolver(_must_not_open_session)
    snapshot = ResolvedSkillSnapshot(
        kind=AssetKind.SKILL,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum="a" * 64,
        catalog_generation=1,
        dependency_version_ids=(),
        files=(SkillArchiveFile("SKILL.md", b"---\nname: demo\n---\n"),),
        secret_requirements=(),
    )

    with pytest.raises(AssetValidationFailed):
        await resolver.materialize_mcp_secrets(_context(), snapshot)


def test_materialized_secrets_never_render_plaintext_in_repr() -> None:
    from app.shared_assets.resolver import MaterializedMcpSecrets

    materialized = MaterializedMcpSecrets(
        mcp_version_id=uuid.uuid4(),
        by_slot={"primary": {"env": {"API_TOKEN": "do-not-render"}}},
    )

    assert "do-not-render" not in repr(materialized)
    assert materialized.by_slot["primary"]["env"]["API_TOKEN"] == "do-not-render"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload_schema_version", [1, 2])
async def test_agent_snapshot_carries_exact_prompt_fields(
    payload_schema_version: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets import resolver as resolver_module

    context = _context()
    agent_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset = AgentRow(
        id=agent_id,
        scope="project",
        project_id=context.project_id,
        slug="exact-agent",
        display_name="Exact Agent",
        status="active",
        created_by_user_id=str(context.user_id),
    )
    version = AgentVersionRow(
        id=version_id,
        agent_id=agent_id,
        version_number=1,
        workflow_status="published",
        description="exact description",
        payload_schema_version=payload_schema_version,
        agents_instructions="exact agents instructions",
        soul="exact soul",
        identity="exact identity",
        user_context="exact user context",
        model_ref="test-model",
        tool_groups=["task"],
        payload_checksum="a" * 64,
        created_by_user_id=str(context.user_id),
    )

    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        async def execute(self, _statement):
            return EmptyResult()

    resolver = resolver_module.ProjectAssetResolver(_must_not_open_session)
    monkeypatch.setattr(
        resolver,
        "_lock_credential_closures",
        AsyncMock(return_value={}),
    )

    snapshot = await resolver._agent_snapshot(  # noqa: SLF001 - exact runtime contract
        Session(),  # type: ignore[arg-type]
        context,
        resolver_module._ResolvedRecord(  # noqa: SLF001 - exact runtime contract
            AssetScope.PROJECT,
            asset,
            version,
        ),
        3,
    )

    assert snapshot.payload.payload_schema_version == payload_schema_version
    assert snapshot.payload.agents_instructions == "exact agents instructions"
    assert snapshot.payload.soul == "exact soul"
    assert snapshot.payload.identity == "exact identity"
    assert snapshot.payload.user_context == "exact user context"


@pytest.mark.asyncio
async def test_materializer_rejects_duplicate_grant_references_before_database_access() -> None:
    from app.shared_assets.resolver import ProjectAssetResolver

    grant_id = uuid.uuid4()
    snapshot = ResolvedMcpSnapshot(
        kind=AssetKind.MCP,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum="b" * 64,
        catalog_generation=1,
        dependency_version_ids=(),
        definition={"transport": "http"},
        credential_grant_ids=(grant_id, grant_id),
    )

    with pytest.raises(AssetValidationFailed):
        await ProjectAssetResolver(_must_not_open_session).materialize_mcp_secrets(
            _context(),
            snapshot,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_generation", [True, False, -1, 1.0, "1", None])
async def test_materializer_rejects_invalid_catalog_generation_before_database_access(
    catalog_generation: object,
) -> None:
    from app.shared_assets.resolver import ProjectAssetResolver

    snapshot = ResolvedMcpSnapshot(
        kind=AssetKind.MCP,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum="e" * 64,
        catalog_generation=catalog_generation,  # type: ignore[arg-type]
        dependency_version_ids=(),
        definition={"transport": "http"},
        credential_grant_ids=(),
    )

    with pytest.raises(AssetValidationFailed):
        await ProjectAssetResolver(_must_not_open_session).materialize_mcp_secrets(
            _context(),
            snapshot,
        )


@pytest.mark.asyncio
async def test_materializer_rejects_untrusted_context_before_database_access() -> None:
    from app.shared_assets.resolver import ProjectAssetResolver

    snapshot = ResolvedMcpSnapshot(
        kind=AssetKind.MCP,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum="c" * 64,
        catalog_generation=1,
        dependency_version_ids=(),
        definition={"transport": "http"},
        credential_grant_ids=(),
    )

    with pytest.raises(AssetForbidden):
        await ProjectAssetResolver(_must_not_open_session).materialize_mcp_secrets(
            object(),
            snapshot,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("use_function_adapter", [False, True])
async def test_materializer_requires_execute_capability_before_database_access(
    use_function_adapter: bool,
) -> None:
    from app.shared_assets.resolver import (
        ProjectAssetResolver,
        materialize_mcp_secrets,
    )

    snapshot = ResolvedMcpSnapshot(
        kind=AssetKind.MCP,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum="d" * 64,
        catalog_generation=1,
        dependency_version_ids=(),
        definition={"transport": "http"},
        credential_grant_ids=(),
    )
    viewer = _context(ProjectRole.VIEWER)

    with pytest.raises(AssetForbidden):
        if use_function_adapter:
            await materialize_mcp_secrets(
                viewer,
                snapshot,
                session_factory=_must_not_open_session,
            )
        else:
            await ProjectAssetResolver(_must_not_open_session).materialize_mcp_secrets(
                viewer,
                snapshot,
            )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_canonical_main_resolves_project_pool_without_binding_and_keeps_history(
    migrated_postgres_database_url: str,
) -> None:
    from app.private_work.run_repository import PrivateRunCreate
    from app.private_work.snapshot_repository import (
        RunSnapshotRepository,
        agent_model_snapshot_purpose,
    )
    from app.private_work.thread_repository import (
        PrivateThreadRepository,
        ThreadAgentRef,
    )
    from app.shared_assets.resolver import (
        BUILTIN_MAIN_AGENT_SOURCE_KEY,
        ProjectAssetResolver,
    )
    from deerflow.mcp.definition import ExactMcpEndpointPolicy

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    skill_id = uuid.uuid4()
    skill_versions = (uuid.uuid4(), uuid.uuid4())
    mcp_id = uuid.uuid4()
    mcp_versions = (uuid.uuid4(), uuid.uuid4())
    unavailable_skill_id = uuid.uuid4()
    unavailable_skill_version_id = uuid.uuid4()
    unavailable_agent_id = uuid.uuid4()
    unavailable_agent_version_id = uuid.uuid4()
    project_agent_version_id = uuid.uuid4()
    next_main_version_id = uuid.uuid4()
    thread_id = f"dynamic-main-{uuid.uuid4()}"
    owner_id = str(seed.owner_a.user_id)
    try:
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE agents SET source_key=:source_key WHERE id=:id"
                ),
                {
                    "source_key": BUILTIN_MAIN_AGENT_SOURCE_KEY,
                    "id": seed.system_agent_id,
                },
            )
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.system_agent_id, "system"),
            )
            await session.execute(
                text(
                    """INSERT INTO skills
                    (id,scope,project_id,slug,display_name,status,version,created_by_user_id)
                    VALUES (:id,'project',:project_id,:slug,'Unavailable Skill',
                            'suspended',1,:owner)"""
                ),
                {
                    "id": unavailable_skill_id,
                    "project_id": seed.owner_a.project_id,
                    "slug": f"unavailable-skill-{unavailable_skill_id.hex[:8]}",
                    "owner": owner_id,
                },
            )
            await session.execute(
                text(
                    """INSERT INTO skill_versions
                    (id,skill_id,version_number,workflow_status,description,frontmatter,
                     secret_requirements,scan_decision,scan_summary,payload_checksum,
                     created_by_user_id)
                    VALUES (:id,:skill_id,1,'published','','{}'::jsonb,
                            '[]'::jsonb,'allow','{}'::jsonb,:checksum,:owner)"""
                ),
                {
                    "id": unavailable_skill_version_id,
                    "skill_id": unavailable_skill_id,
                    "checksum": "e" * 64,
                    "owner": owner_id,
                },
            )
            await session.execute(
                text(
                    "UPDATE skills SET current_published_version_id=:version_id WHERE id=:id"
                ),
                {
                    "version_id": unavailable_skill_version_id,
                    "id": unavailable_skill_id,
                },
            )
            await session.execute(
                text(
                    """INSERT INTO agents
                    (id,scope,project_id,slug,display_name,status,version,created_by_user_id)
                    VALUES (:id,'project',:project_id,:slug,'Unavailable Agent',
                            'active',1,:owner)"""
                ),
                {
                    "id": unavailable_agent_id,
                    "project_id": seed.owner_a.project_id,
                    "slug": f"unavailable-agent-{unavailable_agent_id.hex[:8]}",
                    "owner": owner_id,
                },
            )
            await session.execute(
                text(
                    """INSERT INTO agent_versions
                    (id,agent_id,version_number,workflow_status,description,soul,
                     model_ref,tool_groups,payload_checksum,created_by_user_id)
                    VALUES (:id,:agent_id,1,'draft','','bad dependency','test-model',
                            '[]'::jsonb,:checksum,:owner)"""
                ),
                {
                    "id": unavailable_agent_version_id,
                    "agent_id": unavailable_agent_id,
                    "checksum": "f" * 64,
                    "owner": owner_id,
                },
            )
            await session.execute(
                text(
                    """INSERT INTO agent_version_skill_refs
                    (agent_version_id,skill_version_id,sort_order)
                    VALUES (:agent_version_id,:version_id,0)"""
                ),
                {
                    "agent_version_id": unavailable_agent_version_id,
                    "version_id": unavailable_skill_version_id,
                },
            )
            await session.execute(
                text(
                    "UPDATE agent_versions SET workflow_status='published' WHERE id=:id"
                ),
                {"id": unavailable_agent_version_id},
            )
            await session.execute(
                text(
                    "UPDATE agents SET current_published_version_id=:version_id WHERE id=:id"
                ),
                {
                    "version_id": unavailable_agent_version_id,
                    "id": unavailable_agent_id,
                },
            )
            await session.execute(
                text(
                    "DELETE FROM project_system_agent_bindings "
                    "WHERE project_id=:project_id AND system_agent_id=:agent_id"
                ),
                {
                    "project_id": seed.owner_a.project_id,
                    "agent_id": seed.system_agent_id,
                },
            )
            await session.execute(
                text(
                    """INSERT INTO skills
                    (id,scope,project_id,slug,display_name,status,version,created_by_user_id)
                    VALUES (:id,'project',:project_id,:slug,'Dynamic Skill','active',1,:owner)"""
                ),
                {
                    "id": skill_id,
                    "project_id": seed.owner_a.project_id,
                    "slug": f"dynamic-skill-{skill_id.hex[:8]}",
                    "owner": owner_id,
                },
            )
            for number, version_id in enumerate(skill_versions, 1):
                content = (
                    f"---\nname: dynamic-skill\ndescription: v{number}\n---\nbody\n"
                ).encode()
                file_sha = hashlib.sha256(content).hexdigest()
                checksum = hashlib.sha256(
                    json.dumps(
                        [
                            {
                                "path": "SKILL.md",
                                "sha256": file_sha,
                                "size_bytes": len(content),
                            }
                        ],
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                await session.execute(
                    text(
                        """INSERT INTO skill_versions
                        (id,skill_id,version_number,workflow_status,description,frontmatter,
                         secret_requirements,scan_decision,scan_summary,payload_checksum,
                         created_by_user_id)
                        VALUES (:id,:skill_id,:number,'draft','','{}'::jsonb,
                                '[]'::jsonb,'allow','{}'::jsonb,:checksum,:owner)"""
                    ),
                    {
                        "id": version_id,
                        "skill_id": skill_id,
                        "number": number,
                        "checksum": checksum,
                        "owner": owner_id,
                    },
                )
                await session.execute(
                    text(
                        """INSERT INTO skill_version_files
                        (skill_version_id,path,media_type,size_bytes,sha256,content)
                        VALUES (:version_id,'SKILL.md','text/markdown',:size,:sha,:content)"""
                    ),
                    {
                        "version_id": version_id,
                        "size": len(content),
                        "sha": file_sha,
                        "content": content,
                    },
                )
                await session.execute(
                    text(
                        "UPDATE skill_versions SET workflow_status='published' WHERE id=:id"
                    ),
                    {"id": version_id},
                )
            await session.execute(
                text(
                    "UPDATE skills SET current_published_version_id=:version_id WHERE id=:id"
                ),
                {"version_id": skill_versions[1], "id": skill_id},
            )
            await session.execute(
                text(
                    """INSERT INTO mcp_servers
                    (id,scope,project_id,slug,display_name,status,version,created_by_user_id)
                    VALUES (:id,'project',:project_id,:slug,'Dynamic MCP','active',1,:owner)"""
                ),
                {
                    "id": mcp_id,
                    "project_id": seed.owner_a.project_id,
                    "slug": f"dynamic-mcp-{mcp_id.hex[:8]}",
                    "owner": owner_id,
                },
            )
            for number, version_id in enumerate(mcp_versions, 1):
                await session.execute(
                    text(
                        """INSERT INTO mcp_server_versions
                        (id,mcp_server_id,version_number,workflow_status,description,transport,
                         args,url,non_secret_env,non_secret_headers,oauth_metadata,routing,
                         tool_overrides,timeout_seconds,payload_checksum,created_by_user_id)
                        VALUES (:id,:mcp_id,:number,'published','','http','[]'::jsonb,
                                'http://10.0.0.8/api/mcp','{}'::jsonb,'{}'::jsonb,
                                '{}'::jsonb,'{}'::jsonb,'{}'::jsonb,30,:checksum,:owner)"""
                    ),
                    {
                        "id": version_id,
                        "mcp_id": mcp_id,
                        "number": number,
                        "checksum": ("c" if number == 1 else "d") * 64,
                        "owner": owner_id,
                    },
                )
            await session.execute(
                text(
                    "UPDATE mcp_servers SET current_published_version_id=:version_id WHERE id=:id"
                ),
                {"version_id": mcp_versions[1], "id": mcp_id},
            )
            await session.execute(
                text(
                    """INSERT INTO agent_versions
                    (id,agent_id,version_number,workflow_status,description,soul,
                     model_ref,tool_groups,payload_checksum,created_by_user_id)
                    VALUES (:id,:agent_id,2,'draft','','historical deps','test-model',
                            '[]'::jsonb,:checksum,:owner)"""
                ),
                {
                    "id": project_agent_version_id,
                    "agent_id": seed.project_agent_id,
                    "checksum": "2" * 64,
                    "owner": owner_id,
                },
            )
            await session.execute(
                text(
                    """INSERT INTO agent_version_skill_refs
                    (agent_version_id,skill_version_id,sort_order)
                    VALUES (:agent_version_id,:version_id,0)"""
                ),
                {
                    "agent_version_id": project_agent_version_id,
                    "version_id": skill_versions[0],
                },
            )
            await session.execute(
                text(
                    """INSERT INTO agent_version_mcp_refs
                    (agent_version_id,mcp_server_version_id,sort_order)
                    VALUES (:agent_version_id,:version_id,0)"""
                ),
                {
                    "agent_version_id": project_agent_version_id,
                    "version_id": mcp_versions[0],
                },
            )
            await session.execute(
                text(
                    "UPDATE agent_versions SET workflow_status='published' WHERE id=:id"
                ),
                {"id": project_agent_version_id},
            )
            await session.execute(
                text(
                    "UPDATE agents SET current_published_version_id=:version_id WHERE id=:id"
                ),
                {
                    "version_id": project_agent_version_id,
                    "id": seed.project_agent_id,
                },
            )

        project_context = ProjectContext(
            user_id=seed.owner_a.user_id,
            project_id=seed.owner_a.project_id,
            membership_id=seed.owner_a.membership_id,
            role=seed.owner_a.role,
            capabilities=seed.owner_a.capabilities,
            membership_version=seed.owner_a.membership_version,
            request_id="req-dynamic-main",
        )
        resolver = ProjectAssetResolver(seed.factory)
        async with seed.factory() as session, session.begin():
            closure = await resolver.resolve_run_asset_closure_in_session(
                session,
                project_context,
                AssetSelection(AssetKind.AGENT, seed.system_agent_id),
            )

        assert closure.lead_agent.asset_id == seed.system_agent_id
        assert tuple(item.asset_id for item in closure.delegated_agents) == (
            seed.project_agent_id,
        )
        assert seed.project_b_agent_id not in {
            item.asset_id for item in closure.delegated_agents
        }
        assert unavailable_agent_id not in {
            item.asset_id for item in closure.delegated_agents
        }
        assert tuple(item.version_id for item in closure.skills) == (
            skill_versions[1],
            skill_versions[0],
        )
        assert closure.main_skill_version_ids == (skill_versions[1],)
        assert tuple(item.version_id for item in closure.mcps) == (
            mcp_versions[1],
            mcp_versions[0],
        )
        assert closure.main_mcp_version_ids == (mcp_versions[1],)

        model_snapshot_calls: list[tuple[str, str]] = []

        class ModelCatalog:
            async def admit_model_snapshot(self, _session, **kwargs):
                model_snapshot_calls.append(
                    (kwargs["purpose"], kwargs["model_ref"])
                )
                return type("ModelSnapshot", (), {"logical_name": "test-model"})()

        snapshot_repository = RunSnapshotRepository(
            seed.factory,
            model_catalog=ModelCatalog(),
            endpoint_policy=ExactMcpEndpointPolicy(
                frozenset({"http://10.0.0.8/api/mcp"})
            ),
        )
        run = await snapshot_repository.create_run_with_snapshot(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(),
            closure,
        )
        persisted = await snapshot_repository.list_assets(
            seed.owner_a,
            run.run_id,
        )
        assert [
            (item.asset_kind, item.dependency_order, item.version_id)
            for item in persisted
        ] == [
            ("agent", 0, closure.lead_agent.version_id),
            ("agent", 1, closure.delegated_agents[0].version_id),
            ("skill", 2, skill_versions[1]),
            ("skill", 3, skill_versions[0]),
            ("mcp", 4, mcp_versions[1]),
            ("mcp", 5, mcp_versions[0]),
        ]
        assert model_snapshot_calls == [
            ("lead", closure.lead_agent.payload.model_ref),
            (
                agent_model_snapshot_purpose(
                    closure.delegated_agents[0].version_id
                ),
                closure.delegated_agents[0].payload.model_ref,
            ),
        ]

        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """INSERT INTO agent_versions
                    (id,agent_id,version_number,workflow_status,description,soul,
                     model_ref,tool_groups,payload_checksum,created_by_user_id)
                    VALUES (:id,:agent_id,2,'published','','new main','test-model',
                            '[]'::jsonb,:checksum,:owner)"""
                ),
                {
                    "id": next_main_version_id,
                    "agent_id": seed.system_agent_id,
                    "checksum": "1" * 64,
                    "owner": owner_id,
                },
            )
            await session.execute(
                text(
                    "UPDATE agents SET current_published_version_id=:version_id WHERE id=:id"
                ),
                {
                    "version_id": next_main_version_id,
                    "id": seed.system_agent_id,
                },
            )

        async with seed.factory() as session, session.begin():
            exact_old_main = await resolver.resolve_run_asset_snapshot_in_session(
                session,
                project_context,
                AssetSelection(
                    AssetKind.AGENT,
                    seed.system_agent_id,
                    closure.lead_agent.version_id,
                ),
            )
            exact_historical_skill = (
                await resolver.resolve_run_asset_snapshot_in_session(
                    session,
                    project_context,
                    AssetSelection(
                        AssetKind.SKILL,
                        skill_id,
                        skill_versions[0],
                    ),
                )
            )
            exact_historical_mcp = (
                await resolver.resolve_run_asset_snapshot_in_session(
                    session,
                    project_context,
                    AssetSelection(
                        AssetKind.MCP,
                        mcp_id,
                        mcp_versions[0],
                    ),
                )
            )
            with pytest.raises(AssetResolutionUnavailable):
                await resolver.resolve_project_asset_snapshot_in_session(
                    session,
                    project_context,
                    AssetSelection(
                        AssetKind.SKILL,
                        skill_id,
                        skill_versions[0],
                    ),
                )
        assert exact_old_main.version_id == closure.lead_agent.version_id
        assert exact_historical_skill.version_id == skill_versions[0]
        assert exact_historical_mcp.version_id == mcp_versions[0]
    finally:
        await seed.engine.dispose()
