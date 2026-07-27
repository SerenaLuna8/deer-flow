from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkConflict,
    PrivateWorkUnavailable,
)
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.snapshot_repository import (
    RunSnapshotAssetStale,
    RunSnapshotRepository,
)
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
)
from deerflow.mcp.definition import ExactMcpEndpointPolicy
from deerflow.persistence.run.model import RunRow


@dataclass(frozen=True)
class SnapshotScenario:
    seed: M4ThreadSeed
    thread_id: str
    other_thread_id: str
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    mcp_id: uuid.UUID
    mcp_version_id: uuid.UUID
    slot_id: uuid.UUID
    alternate_slot_id: uuid.UUID
    grant_id: uuid.UUID
    credential_id: uuid.UUID
    credential_version_id: uuid.UUID
    envelope_id: uuid.UUID
    generation: int

    @property
    def resolved_agent(self) -> ResolvedAgentSnapshot:
        return ResolvedAgentSnapshot(
            kind=AssetKind.AGENT,
            scope=AssetScope.PROJECT,
            asset_id=self.agent_id,
            version_id=self.agent_version_id,
            checksum="a" * 64,
            catalog_generation=self.generation,
            dependency_version_ids=(self.skill_version_id, self.mcp_version_id),
            payload=AgentPayload(
                description="",
                soul="thread agent",
                model_ref="test-model",
                tool_groups=(),
                skill_version_ids=(self.skill_version_id,),
                mcp_version_ids=(self.mcp_version_id,),
            ),
        )

    @property
    def repository(self) -> RunSnapshotRepository:
        return RunSnapshotRepository(
            self.seed.factory,
            endpoint_policy=ExactMcpEndpointPolicy(frozenset({"https://snapshot-mcp.example.test/exact"})),
        )


@pytest.mark.asyncio
async def test_run_snapshot_skill_resolution_takes_shared_row_locks() -> None:
    project_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset = SimpleNamespace(
        scope="project",
        project_id=project_id,
        status="active",
    )
    version = SimpleNamespace(workflow_status="published")

    class Result:
        def one_or_none(self):
            return asset, version

    class Session:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return Result()

    session = Session()

    rows = await RunSnapshotRepository._skills(
        session,
        (version_id,),
        project_id,
    )

    assert rows == [(asset, version)]
    assert session.statement is not None
    assert session.statement._for_update_arg is not None
    assert session.statement._for_update_arg.read is True


@pytest.mark.asyncio
async def test_run_snapshot_rejects_historical_project_stdio_mcp() -> None:
    project_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset = SimpleNamespace(
        scope="project",
        project_id=project_id,
        status="active",
    )
    version = SimpleNamespace(
        workflow_status="published",
        transport="stdio",
    )

    class Result:
        def one_or_none(self):
            return asset, version

    class Session:
        async def execute(self, _statement):
            return Result()

    with pytest.raises(RunSnapshotAssetStale):
        await RunSnapshotRepository._mcps(
            Session(),
            (version_id,),
            project_id,
        )


@pytest.mark.asyncio
async def test_run_snapshot_rejects_historical_project_remote_outside_operator_policy() -> None:
    project_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset = SimpleNamespace(
        scope="project",
        project_id=project_id,
        status="active",
    )
    version = SimpleNamespace(
        workflow_status="published",
        transport="http",
        url="https://blocked.example.test/mcp",
        non_secret_env={},
        non_secret_headers={},
        oauth_metadata={},
    )

    class Result:
        def one_or_none(self):
            return asset, version

    class Session:
        async def execute(self, _statement):
            return Result()

    with pytest.raises(RunSnapshotAssetStale):
        await RunSnapshotRepository._mcps(
            Session(),
            (version_id,),
            project_id,
            endpoint_policy=ExactMcpEndpointPolicy(frozenset({"https://allowed.example.test/mcp"})),
        )


@pytest.mark.asyncio
async def test_run_snapshot_accepts_project_remote_in_operator_policy() -> None:
    project_id = uuid.uuid4()
    version_id = uuid.uuid4()
    endpoint = "https://allowed.example.test/mcp"
    asset = SimpleNamespace(
        scope="project",
        project_id=project_id,
        status="active",
    )
    version = SimpleNamespace(
        workflow_status="published",
        transport="http",
        url=endpoint,
        non_secret_env={},
        non_secret_headers={},
        oauth_metadata={},
    )

    class Result:
        def one_or_none(self):
            return asset, version

    class Session:
        statement = None

        async def execute(self, _statement):
            self.statement = _statement
            return Result()

    session = Session()
    rows = await RunSnapshotRepository._mcps(
        session,
        (version_id,),
        project_id,
        endpoint_policy=ExactMcpEndpointPolicy(frozenset({endpoint})),
    )

    assert rows == [(asset, version)]
    assert session.statement is not None
    assert session.statement._for_update_arg is not None
    assert session.statement._for_update_arg.read is True


@pytest.mark.asyncio
async def test_run_snapshot_rejects_project_remote_when_policy_is_not_injected() -> None:
    project_id = uuid.uuid4()
    version_id = uuid.uuid4()
    asset = SimpleNamespace(
        scope="project",
        project_id=project_id,
        status="active",
    )
    version = SimpleNamespace(
        workflow_status="published",
        transport="http",
        url="https://allowed.example.test/mcp",
        non_secret_env={},
        non_secret_headers={},
        oauth_metadata={},
    )

    class Result:
        def one_or_none(self):
            return asset, version

    class Session:
        async def execute(self, _statement):
            return Result()

    with pytest.raises(RunSnapshotAssetStale):
        await RunSnapshotRepository._mcps(
            Session(),
            (version_id,),
            project_id,
        )


def test_run_snapshot_rejects_historical_project_non_header_credential_slot() -> None:
    endpoint = "https://allowed.example.test/mcp"
    version_id = uuid.uuid4()
    asset = SimpleNamespace(scope="project")
    version = SimpleNamespace(
        id=version_id,
        transport="http",
        url=endpoint,
        non_secret_env={},
        non_secret_headers={},
        oauth_metadata={},
    )
    closures = {
        version_id: SimpleNamespace(
            slots=(SimpleNamespace(payload_schema={"env": ["TOKEN"]}),),
        )
    }

    with pytest.raises(RunSnapshotAssetStale):
        RunSnapshotRepository._validate_project_mcp_credential_slots(
            [(asset, version)],
            closures,
            endpoint_policy=ExactMcpEndpointPolicy(frozenset({endpoint})),
        )


def test_run_snapshot_rejects_cross_slot_credential_header_collisions() -> None:
    endpoint = "https://allowed.example.test/mcp"
    version_id = uuid.uuid4()
    asset = SimpleNamespace(scope="project")
    version = SimpleNamespace(
        id=version_id,
        transport="http",
        url=endpoint,
        non_secret_env={},
        non_secret_headers={},
        oauth_metadata={},
    )
    closures = {
        version_id: SimpleNamespace(
            slots=(
                SimpleNamespace(
                    payload_schema={"headers": ["Authorization"]},
                ),
                SimpleNamespace(
                    payload_schema={"headers": ["authorization"]},
                ),
            ),
        )
    }

    with pytest.raises(RunSnapshotAssetStale):
        RunSnapshotRepository._validate_project_mcp_credential_slots(
            [(asset, version)],
            closures,
            endpoint_policy=ExactMcpEndpointPolicy(frozenset({endpoint})),
        )


@pytest_asyncio.fixture()
async def snapshot_scenario(migrated_postgres_database_url):
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"snapshot-{uuid.uuid4()}"
    other_thread_id = f"snapshot-other-{uuid.uuid4()}"
    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()
    mcp_id = uuid.uuid4()
    mcp_version_id = uuid.uuid4()
    slot_id = uuid.uuid4()
    alternate_slot_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    credential_version_id = uuid.uuid4()
    envelope_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()
    owner_id = str(seed.owner_a.user_id)
    project_id = seed.owner_a.project_id
    async with seed.factory() as session, session.begin():
        await session.execute(
            text(
                """INSERT INTO agents
                (id,scope,project_id,slug,display_name,status,version,created_by_user_id)
                VALUES (:id,'project',:project_id,:slug,'Snapshot Agent','active',1,:owner)"""
            ),
            {"id": agent_id, "project_id": project_id, "slug": f"snapshot-agent-{agent_id.hex[:8]}", "owner": owner_id},
        )
        await session.execute(
            text(
                """INSERT INTO agent_versions
                (id,agent_id,version_number,workflow_status,description,soul,model_ref,
                 tool_groups,payload_checksum,created_by_user_id)
                VALUES (:id,:asset_id,1,'draft','','thread agent','test-model',
                        '[]'::jsonb,:checksum,:owner)"""
            ),
            {"id": agent_version_id, "asset_id": agent_id, "checksum": "a" * 64, "owner": owner_id},
        )
        threads = PrivateThreadRepository(session)
        await threads.create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(agent_id, "project"),
        )
        await threads.create(
            scope=seed.owner_b_scope,
            thread_id=other_thread_id,
            agent=ThreadAgentRef(agent_id, "project"),
        )
        await session.execute(
            text(
                """INSERT INTO skills
                (id,scope,project_id,slug,display_name,status,version,created_by_user_id)
                VALUES (:id,'project',:project_id,:slug,'Snapshot Skill','active',1,:owner)"""
            ),
            {"id": skill_id, "project_id": project_id, "slug": f"snapshot-skill-{skill_id.hex[:8]}", "owner": owner_id},
        )
        await session.execute(
            text(
                """INSERT INTO skill_versions
                (id,skill_id,version_number,workflow_status,description,frontmatter,
                 compatibility,secret_requirements,scan_decision,scan_summary,
                 payload_checksum,created_by_user_id)
                VALUES (:id,:asset_id,1,'draft','','{}'::jsonb,NULL,'[]'::jsonb,
                        'allow','{}'::jsonb,:checksum,:owner)"""
            ),
            {"id": skill_version_id, "asset_id": skill_id, "checksum": "b" * 64, "owner": owner_id},
        )
        await session.execute(
            text("UPDATE skills SET current_published_version_id=:version_id WHERE id=:id"),
            {"version_id": skill_version_id, "id": skill_id},
        )
        await session.execute(
            text(
                """INSERT INTO mcp_servers
                (id,scope,project_id,slug,display_name,status,version,created_by_user_id)
                VALUES (:id,'project',:project_id,:slug,'Snapshot MCP','active',1,:owner)"""
            ),
            {"id": mcp_id, "project_id": project_id, "slug": f"snapshot-mcp-{mcp_id.hex[:8]}", "owner": owner_id},
        )
        await session.execute(
            text(
                """INSERT INTO mcp_server_versions
                (id,mcp_server_id,version_number,workflow_status,description,transport,
                 command,args,url,non_secret_env,non_secret_headers,oauth_metadata,routing,
                 tool_overrides,timeout_seconds,payload_checksum,created_by_user_id)
                VALUES (:id,:asset_id,1,'draft','','http',NULL,'[]'::jsonb,
                        'https://snapshot-mcp.example.test/exact',
                        '{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'{}'::jsonb,
                        30,:checksum,:owner)"""
            ),
            {"id": mcp_version_id, "asset_id": mcp_id, "checksum": "c" * 64, "owner": owner_id},
        )
        await session.execute(
            text("UPDATE mcp_servers SET current_published_version_id=:version_id WHERE id=:id"),
            {"version_id": mcp_version_id, "id": mcp_id},
        )
        await session.execute(
            text(
                """INSERT INTO credentials
                (id,scope,project_id,name,display_name,credential_type,status,version,created_by_user_id)
                VALUES (:id,'project',:project_id,:name,'Snapshot Credential','token','active',1,:owner)"""
            ),
            {"id": credential_id, "project_id": project_id, "name": f"snapshot-{credential_id.hex[:8]}", "owner": owner_id},
        )
        await session.execute(
            text(
                """INSERT INTO credential_versions
                (id,credential_id,version_number,status,payload_schema_version,payload_schema,created_by_user_id)
                VALUES (:id,:credential_id,1,'active',1,
                        '{"headers":["Authorization"]}'::jsonb,:owner)"""
            ),
            {"id": credential_version_id, "credential_id": credential_id, "owner": owner_id},
        )
        await session.execute(
            text("UPDATE credentials SET current_version_id=:version_id WHERE id=:id"),
            {"version_id": credential_version_id, "id": credential_id},
        )
        await session.execute(
            text(
                """INSERT INTO credential_envelopes
                (id,credential_version_id,envelope_generation,key_id,nonce,ciphertext,
                 is_active,created_by_user_id,activated_at)
                VALUES (:id,:version_id,1,'snapshot-key',:nonce,:ciphertext,true,:owner,now())"""
            ),
            {
                "id": envelope_id,
                "version_id": credential_version_id,
                "nonce": b"n" * 12,
                "ciphertext": b"top-secret-ciphertext",
                "owner": owner_id,
            },
        )
        await session.execute(
            text(
                """INSERT INTO mcp_version_credential_slots
                (id,mcp_server_version_id,name,purpose,payload_schema,required)
                VALUES (:id,:version_id,'token','auth',
                        '{"headers":["Authorization"]}'::jsonb,true)"""
            ),
            {"id": slot_id, "version_id": mcp_version_id},
        )
        await session.execute(
            text(
                """INSERT INTO mcp_version_credential_slots
                (id,mcp_server_version_id,name,purpose,payload_schema,required)
                VALUES (:id,:version_id,'alternate','alternate auth',
                        '{"headers":["Authorization"]}'::jsonb,false)"""
            ),
            {"id": alternate_slot_id, "version_id": mcp_version_id},
        )
        await session.execute(
            text(
                """INSERT INTO credential_grants
                (id,mcp_server_version_id,credential_slot_id,credential_version_id,
                 status,version,created_by_user_id)
                VALUES (:id,:mcp_version_id,:slot_id,:credential_version_id,'active',1,:owner)"""
            ),
            {
                "id": grant_id,
                "mcp_version_id": mcp_version_id,
                "slot_id": slot_id,
                "credential_version_id": credential_version_id,
                "owner": owner_id,
            },
        )
        await session.execute(
            text(
                """INSERT INTO agent_version_skill_refs
                (agent_version_id,skill_version_id,sort_order)
                VALUES (:agent_version_id,:version_id,0)"""
            ),
            {"agent_version_id": agent_version_id, "version_id": skill_version_id},
        )
        await session.execute(
            text(
                """INSERT INTO agent_version_mcp_refs
                (agent_version_id,mcp_server_version_id,sort_order)
                VALUES (:agent_version_id,:version_id,0)"""
            ),
            {"agent_version_id": agent_version_id, "version_id": mcp_version_id},
        )
        await session.execute(
            text("UPDATE agent_versions SET workflow_status='published' WHERE id=:id"),
            {"id": agent_version_id},
        )
        await session.execute(
            text("UPDATE agents SET current_published_version_id=:version_id WHERE id=:id"),
            {"version_id": agent_version_id, "id": agent_id},
        )
        await session.execute(
            text("UPDATE skill_versions SET workflow_status='published' WHERE id=:id"),
            {"id": skill_version_id},
        )
        await session.execute(
            text("UPDATE mcp_server_versions SET workflow_status='published' WHERE id=:id"),
            {"id": mcp_version_id},
        )
        generation = (await session.execute(text("SELECT generation FROM asset_catalog_state WHERE id=1"))).scalar_one()
    scenario = SnapshotScenario(
        seed=seed,
        thread_id=thread_id,
        other_thread_id=other_thread_id,
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        mcp_id=mcp_id,
        mcp_version_id=mcp_version_id,
        slot_id=slot_id,
        alternate_slot_id=alternate_slot_id,
        grant_id=grant_id,
        credential_id=credential_id,
        credential_version_id=credential_version_id,
        envelope_id=envelope_id,
        generation=generation,
    )
    try:
        yield scenario
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_run_snapshot_is_exact_ordered_and_secret_free(snapshot_scenario: SnapshotScenario) -> None:
    scenario = snapshot_scenario
    repository = scenario.repository
    request = PrivateRunCreate(metadata={"source": "snapshot-test"}, kwargs={"input": "hello"})

    run = await repository.create_run_with_snapshot(
        scenario.seed.owner_a,
        scenario.thread_id,
        request,
        scenario.resolved_agent,
    )

    assets = await repository.list_assets(scenario.seed.owner_a, run.run_id)
    assert [(row.asset_kind, row.dependency_order, row.version_id, row.payload_checksum) for row in assets] == [
        ("agent", 0, scenario.agent_version_id, "a" * 64),
        ("skill", 1, scenario.skill_version_id, "b" * 64),
        ("mcp", 2, scenario.mcp_version_id, "c" * 64),
    ]
    assert {row.catalog_generation for row in assets} == {scenario.generation}
    grants = await repository.list_mcp_grants(scenario.seed.owner_a, run.run_id)
    assert [
        (
            row.mcp_version_id,
            row.credential_slot_id,
            row.credential_grant_id,
            row.credential_version_id,
        )
        for row in grants
    ] == [
        (
            scenario.mcp_version_id,
            scenario.slot_id,
            scenario.grant_id,
            scenario.credential_version_id,
        )
    ]
    serialized = json.dumps(
        [dataclasses.asdict(row) for row in (*assets, *grants)],
        default=str,
    ).lower()
    for forbidden in ("secret", "cipher", "key_id", "nonce", "envelope", "storage_locator", "snapshot-key"):
        assert forbidden not in serialized
    assert await repository.list_assets(scenario.seed.owner_b, run.run_id) == ()
    assert await repository.list_assets(scenario.seed.project_b_owner_a, run.run_id) == ()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_snapshot_transaction_rolls_back_stale_or_secret_bearing_admission(
    snapshot_scenario: SnapshotScenario,
) -> None:
    scenario = snapshot_scenario
    repository = scenario.repository
    stale_run_id = str(uuid.uuid4())
    stale = dataclasses.replace(scenario.resolved_agent, checksum="d" * 64)
    with pytest.raises(PrivateWorkAssetStale) as stale_error:
        await repository.create_run_with_snapshot(
            scenario.seed.owner_a,
            scenario.thread_id,
            PrivateRunCreate(run_id=stale_run_id),
            stale,
        )
    _assert_public_error(
        stale_error.value,
        code="PRIVATE_WORK_ASSET_STALE",
        message="Private work asset is stale.",
        request_id=scenario.seed.owner_a.request_id,
    )
    secret_run_id = str(uuid.uuid4())
    with pytest.raises(PrivateWorkConflict) as secret_error:
        await repository.create_run_with_snapshot(
            scenario.seed.owner_a,
            scenario.thread_id,
            PrivateRunCreate(run_id=secret_run_id, kwargs={"key_id": "must-not-persist"}),
            scenario.resolved_agent,
        )
    _assert_public_error(
        secret_error.value,
        code="PRIVATE_WORK_CONFLICT",
        message="Private work conflict.",
        request_id=scenario.seed.owner_a.request_id,
    )
    generation_run_id = str(uuid.uuid4())
    with pytest.raises(PrivateWorkAssetStale) as generation_error:
        await repository.create_run_with_snapshot(
            scenario.seed.owner_a,
            scenario.thread_id,
            PrivateRunCreate(run_id=generation_run_id),
            dataclasses.replace(
                scenario.resolved_agent,
                catalog_generation=scenario.generation + 1,
            ),
        )
    _assert_public_error(
        generation_error.value,
        code="PRIVATE_WORK_ASSET_STALE",
        message="Private work asset is stale.",
        request_id=scenario.seed.owner_a.request_id,
    )
    async with scenario.seed.factory() as session:
        count = (await session.execute(select(RunRow.run_id).where(RunRow.run_id.in_((stale_run_id, secret_run_id, generation_run_id))))).all()
        assert count == []


@pytest.mark.postgres
@pytest.mark.anyio
async def test_snapshot_true_run_conflict_uses_context_request_id(
    snapshot_scenario: SnapshotScenario,
) -> None:
    scenario = snapshot_scenario

    with pytest.raises(PrivateWorkConflict) as captured:
        await scenario.repository.create_run_with_snapshot(
            scenario.seed.owner_a,
            f"missing-{uuid.uuid4()}",
            PrivateRunCreate(),
            scenario.resolved_agent,
        )

    _assert_public_error(
        captured.value,
        code="PRIVATE_WORK_CONFLICT",
        message="Private work conflict.",
        request_id=scenario.seed.owner_a.request_id,
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_snapshot_database_unavailable_is_stable_and_sanitized(
    snapshot_scenario: SnapshotScenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = snapshot_scenario

    async def unavailable(*_args, **_kwargs):
        raise OperationalError(
            "SELECT ciphertext FROM credential_envelopes",
            {"credential_version_id": str(scenario.credential_version_id)},
            RuntimeError("database-url-with-secret"),
        )

    monkeypatch.setattr(RunSnapshotRepository, "_agent", unavailable)

    with pytest.raises(PrivateWorkUnavailable) as captured:
        await scenario.repository.create_run_with_snapshot(
            scenario.seed.owner_a,
            scenario.thread_id,
            PrivateRunCreate(),
            scenario.resolved_agent,
        )

    _assert_public_error(
        captured.value,
        code="PRIVATE_WORK_UNAVAILABLE",
        message="Private work is unavailable.",
        request_id=scenario.seed.owner_a.request_id,
    )


def _assert_public_error(error, *, code: str, message: str, request_id: str) -> None:
    assert error.code == code
    assert error.public_message == message
    assert error.request_id == request_id
    mapped = private_work_http_exception(error)
    assert mapped.detail == {
        "code": code,
        "message": message,
        "request_id": request_id,
    }
    serialized = json.dumps(mapped.detail).lower()
    for forbidden in (
        "select ",
        "credential_versions",
        "snapshot-key",
        "top-secret-ciphertext",
        "storage_locator",
    ):
        assert forbidden not in serialized


async def _current_generation(scenario: SnapshotScenario) -> int:
    async with scenario.seed.factory() as session:
        return int((await session.execute(text("SELECT generation FROM asset_catalog_state WHERE id=1"))).scalar_one())


async def _insert_credential_material(
    scenario: SnapshotScenario,
    *,
    scope: str,
    project_id: uuid.UUID | None,
    payload_schema: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    credential_id = uuid.uuid4()
    version_id = uuid.uuid4()
    owner_id = str(scenario.seed.owner_a.user_id)
    async with scenario.seed.factory() as session, session.begin():
        await session.execute(
            text(
                """INSERT INTO credentials
                (id,scope,project_id,name,display_name,credential_type,status,version,
                 created_by_user_id)
                VALUES (:id,:scope,:project_id,:name,'Alternate Credential','token',
                        'active',1,:owner)"""
            ),
            {
                "id": credential_id,
                "scope": scope,
                "project_id": project_id,
                "name": f"alternate-{credential_id.hex[:8]}",
                "owner": owner_id,
            },
        )
        await session.execute(
            text(
                """INSERT INTO credential_versions
                (id,credential_id,version_number,status,payload_schema_version,
                 payload_schema,created_by_user_id)
                VALUES (:id,:credential_id,1,'active',1,CAST(:payload_schema AS jsonb),
                        :owner)"""
            ),
            {
                "id": version_id,
                "credential_id": credential_id,
                "payload_schema": payload_schema,
                "owner": owner_id,
            },
        )
        await session.execute(
            text("UPDATE credentials SET current_version_id=:version_id WHERE id=:id"),
            {"version_id": version_id, "id": credential_id},
        )
        await session.execute(
            text(
                """INSERT INTO credential_envelopes
                (id,credential_version_id,envelope_generation,key_id,nonce,ciphertext,
                 is_active,created_by_user_id,activated_at)
                VALUES (:id,:version_id,1,'alternate-key',:nonce,:ciphertext,true,
                        :owner,now())"""
            ),
            {
                "id": uuid.uuid4(),
                "version_id": version_id,
                "nonce": b"a" * 12,
                "ciphertext": b"a" * 16,
                "owner": owner_id,
            },
        )
        await session.execute(
            text(
                """UPDATE credential_grants
                SET credential_version_id=:version_id
                WHERE id=:grant_id"""
            ),
            {"version_id": version_id, "grant_id": scenario.grant_id},
        )
    return credential_id, version_id


@pytest.mark.postgres
@pytest.mark.anyio
async def test_snapshot_accepts_active_grant_pinned_to_retired_version(
    snapshot_scenario: SnapshotScenario,
) -> None:
    scenario = snapshot_scenario
    async with scenario.seed.factory() as session, session.begin():
        await session.execute(
            text(
                """UPDATE credential_versions
                SET status='retired', retired_at=now()
                WHERE id=:version_id"""
            ),
            {"version_id": scenario.credential_version_id},
        )
    resolved = dataclasses.replace(
        scenario.resolved_agent,
        catalog_generation=await _current_generation(scenario),
    )
    repository = scenario.repository

    run = await repository.create_run_with_snapshot(
        scenario.seed.owner_a,
        scenario.thread_id,
        PrivateRunCreate(),
        resolved,
    )

    grants = await repository.list_mcp_grants(scenario.seed.owner_a, run.run_id)
    assert tuple(row.credential_version_id for row in grants) == (scenario.credential_version_id,)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_snapshot_rejects_inactive_or_missing_active_envelope(
    snapshot_scenario: SnapshotScenario,
) -> None:
    scenario = snapshot_scenario
    run_id = str(uuid.uuid4())
    async with scenario.seed.factory() as session, session.begin():
        await session.execute(
            text("UPDATE credential_envelopes SET is_active=false WHERE id=:id"),
            {"id": scenario.envelope_id},
        )
    resolved = dataclasses.replace(
        scenario.resolved_agent,
        catalog_generation=await _current_generation(scenario),
    )

    with pytest.raises(PrivateWorkAssetStale) as captured:
        await scenario.repository.create_run_with_snapshot(
            scenario.seed.owner_a,
            scenario.thread_id,
            PrivateRunCreate(run_id=run_id),
            resolved,
        )

    _assert_public_error(
        captured.value,
        code="PRIVATE_WORK_ASSET_STALE",
        message="Private work asset is stale.",
        request_id=scenario.seed.owner_a.request_id,
    )
    async with scenario.seed.factory() as session:
        assert await session.get(RunRow, run_id) is None


@pytest.mark.postgres
@pytest.mark.anyio
async def test_snapshot_rejects_credential_scope_mismatch(
    snapshot_scenario: SnapshotScenario,
) -> None:
    scenario = snapshot_scenario
    await _insert_credential_material(
        scenario,
        scope="system",
        project_id=None,
        payload_schema="{}",
    )
    resolved = dataclasses.replace(
        scenario.resolved_agent,
        catalog_generation=await _current_generation(scenario),
    )

    with pytest.raises(PrivateWorkAssetStale) as captured:
        await scenario.repository.create_run_with_snapshot(
            scenario.seed.owner_a,
            scenario.thread_id,
            PrivateRunCreate(),
            resolved,
        )

    _assert_public_error(
        captured.value,
        code="PRIVATE_WORK_ASSET_STALE",
        message="Private work asset is stale.",
        request_id=scenario.seed.owner_a.request_id,
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_snapshot_rejects_slot_payload_schema_mismatch(
    snapshot_scenario: SnapshotScenario,
) -> None:
    scenario = snapshot_scenario
    await _insert_credential_material(
        scenario,
        scope="project",
        project_id=scenario.seed.owner_a.project_id,
        payload_schema='{"env":["OTHER_TOKEN"]}',
    )
    resolved = dataclasses.replace(
        scenario.resolved_agent,
        catalog_generation=await _current_generation(scenario),
    )

    with pytest.raises(PrivateWorkAssetStale) as captured:
        await scenario.repository.create_run_with_snapshot(
            scenario.seed.owner_a,
            scenario.thread_id,
            PrivateRunCreate(),
            resolved,
        )

    _assert_public_error(
        captured.value,
        code="PRIVATE_WORK_ASSET_STALE",
        message="Private work asset is stale.",
        request_id=scenario.seed.owner_a.request_id,
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_snapshot_credential_closure_locks_serialize_repin(
    snapshot_scenario: SnapshotScenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.private_work.snapshot_repository as snapshot_module
    from app.shared_assets.credential_closure import lock_mcp_credential_closures

    scenario = snapshot_scenario
    closure_locked = asyncio.Event()
    release_snapshot = asyncio.Event()
    repin_attempted = asyncio.Event()
    repin_committed = asyncio.Event()

    async def pause_after_closure(*args, **kwargs):
        closures = await lock_mcp_credential_closures(*args, **kwargs)
        closure_locked.set()
        await release_snapshot.wait()
        return closures

    monkeypatch.setattr(
        snapshot_module,
        "lock_mcp_credential_closures",
        pause_after_closure,
    )

    async def repin() -> None:
        repin_attempted.set()
        async with scenario.seed.engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE credential_grants
                    SET credential_slot_id=:slot_id
                    WHERE id=:grant_id"""
                ),
                {
                    "slot_id": scenario.alternate_slot_id,
                    "grant_id": scenario.grant_id,
                },
            )
        repin_committed.set()

    snapshot_task = asyncio.create_task(
        scenario.repository.create_run_with_snapshot(
            scenario.seed.owner_a,
            scenario.thread_id,
            PrivateRunCreate(),
            scenario.resolved_agent,
        )
    )
    repin_task = None
    try:
        await asyncio.wait_for(closure_locked.wait(), timeout=5)
        repin_task = asyncio.create_task(repin())
        await asyncio.wait_for(repin_attempted.wait(), timeout=5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(repin_task), timeout=0.2)
        assert not repin_committed.is_set()
        release_snapshot.set()
        run, _ = await asyncio.wait_for(
            asyncio.gather(snapshot_task, repin_task),
            timeout=10,
        )
        grants = await scenario.repository.list_mcp_grants(scenario.seed.owner_a, run.run_id)
        assert tuple(row.credential_slot_id for row in grants) == (scenario.slot_id,)
    finally:
        release_snapshot.set()
        pending = [task for task in (snapshot_task, repin_task) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_snapshot_credential_closure_locks_serialize_revoke(
    snapshot_scenario: SnapshotScenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.private_work.snapshot_repository as snapshot_module
    from app.shared_assets.credential_closure import lock_mcp_credential_closures

    scenario = snapshot_scenario
    closure_locked = asyncio.Event()
    release_snapshot = asyncio.Event()
    revoke_attempted = asyncio.Event()
    revoke_committed = asyncio.Event()

    async def pause_after_closure(*args, **kwargs):
        closures = await lock_mcp_credential_closures(*args, **kwargs)
        closure_locked.set()
        await release_snapshot.wait()
        return closures

    monkeypatch.setattr(
        snapshot_module,
        "lock_mcp_credential_closures",
        pause_after_closure,
    )

    async def revoke() -> None:
        revoke_attempted.set()
        async with scenario.seed.engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE credentials
                    SET status='revoked'
                    WHERE id=:credential_id"""
                ),
                {"credential_id": scenario.credential_id},
            )
        revoke_committed.set()

    snapshot_task = asyncio.create_task(
        scenario.repository.create_run_with_snapshot(
            scenario.seed.owner_a,
            scenario.thread_id,
            PrivateRunCreate(),
            scenario.resolved_agent,
        )
    )
    revoke_task = None
    try:
        await asyncio.wait_for(closure_locked.wait(), timeout=5)
        revoke_task = asyncio.create_task(revoke())
        await asyncio.wait_for(revoke_attempted.wait(), timeout=5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(revoke_task), timeout=0.2)
        assert not revoke_committed.is_set()
        release_snapshot.set()
        run, _ = await asyncio.wait_for(
            asyncio.gather(snapshot_task, revoke_task),
            timeout=10,
        )
        assert await scenario.repository.list_mcp_grants(
            scenario.seed.owner_a,
            run.run_id,
        )
    finally:
        release_snapshot.set()
        pending = [task for task in (snapshot_task, revoke_task) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_composite_fks_reject_wrong_scope_run_for_event_snapshot_and_file(
    snapshot_scenario: SnapshotScenario,
) -> None:
    scenario = snapshot_scenario
    other_run_id = str(uuid.uuid4())
    async with scenario.seed.factory() as session, session.begin():
        await PrivateRunRepository(session).create(
            scope=scenario.seed.owner_b_scope,
            thread_id=scenario.other_thread_id,
            request=PrivateRunCreate(run_id=other_run_id),
        )
    owner_project = scenario.seed.owner_a.project_id
    owner_user = str(scenario.seed.owner_a.user_id)

    statements = (
        (
            """INSERT INTO run_events
            (project_id,owner_user_id,thread_id,run_id,event_type,category,content,
             event_metadata,seq,created_at)
            VALUES (:project,:owner,:thread,:run,'test','trace','','{}'::jsonb,1,now())""",
            {},
        ),
        (
            """INSERT INTO run_asset_versions
            (project_id,owner_user_id,thread_id,run_id,asset_kind,dependency_order,
             asset_scope,asset_id,version_id,payload_checksum,catalog_generation,created_at)
            VALUES (:project,:owner,:thread,:run,'agent',0,'project',:asset,:version,
                    :checksum,:generation,now())""",
            {
                "asset": scenario.agent_id,
                "version": scenario.agent_version_id,
                "checksum": "a" * 64,
                "generation": scenario.generation,
            },
        ),
        (
            """INSERT INTO files
            (id,project_id,owner_user_id,thread_id,kind,logical_path,media_type,size,
             sha256,status,version,created_by_run_id,created_at,updated_at)
            VALUES (:file_id,:project,:owner,:thread,'output','wrong-scope.txt','text/plain',0,
                    :sha,'ready',1,:run,now(),now())""",
            {"file_id": uuid.uuid4(), "sha": "0" * 64},
        ),
    )
    for statement, extra in statements:
        with pytest.raises(IntegrityError):
            async with scenario.seed.factory() as session, session.begin():
                await session.execute(
                    text(statement),
                    {
                        "project": owner_project,
                        "owner": owner_user,
                        "thread": scenario.thread_id,
                        "run": other_run_id,
                        **extra,
                    },
                )
