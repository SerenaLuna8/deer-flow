from __future__ import annotations

import dataclasses
import json
import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.private_work.errors import PrivateWorkConflict
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.snapshot_repository import RunSnapshotRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.shared_assets.models import (
    AgentPayload,
    AssetKind,
    AssetScope,
    ResolvedAgentSnapshot,
)
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
    grant_id: uuid.UUID
    credential_version_id: uuid.UUID
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
                 command,args,non_secret_env,non_secret_headers,oauth_metadata,routing,
                 tool_overrides,timeout_seconds,payload_checksum,created_by_user_id)
                VALUES (:id,:asset_id,1,'draft','','stdio','snapshot-command','[]'::jsonb,
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
                VALUES (:id,:credential_id,1,'active',1,'{}'::jsonb,:owner)"""
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
                VALUES (:id,:version_id,'token','auth','{}'::jsonb,true)"""
            ),
            {"id": slot_id, "version_id": mcp_version_id},
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
        grant_id=grant_id,
        credential_version_id=credential_version_id,
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
    repository = RunSnapshotRepository(scenario.seed.factory)
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
    repository = RunSnapshotRepository(scenario.seed.factory)
    stale_run_id = str(uuid.uuid4())
    stale = dataclasses.replace(scenario.resolved_agent, checksum="d" * 64)
    with pytest.raises(PrivateWorkConflict):
        await repository.create_run_with_snapshot(
            scenario.seed.owner_a,
            scenario.thread_id,
            PrivateRunCreate(run_id=stale_run_id),
            stale,
        )
    secret_run_id = str(uuid.uuid4())
    with pytest.raises(PrivateWorkConflict):
        await repository.create_run_with_snapshot(
            scenario.seed.owner_a,
            scenario.thread_id,
            PrivateRunCreate(run_id=secret_run_id, kwargs={"key_id": "must-not-persist"}),
            scenario.resolved_agent,
        )
    generation_run_id = str(uuid.uuid4())
    with pytest.raises(PrivateWorkConflict):
        await repository.create_run_with_snapshot(
            scenario.seed.owner_a,
            scenario.thread_id,
            PrivateRunCreate(run_id=generation_run_id),
            dataclasses.replace(
                scenario.resolved_agent,
                catalog_generation=scenario.generation + 1,
            ),
        )
    async with scenario.seed.factory() as session:
        count = (await session.execute(select(RunRow.run_id).where(RunRow.run_id.in_((stale_run_id, secret_run_id, generation_run_id))))).all()
        assert count == []


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
