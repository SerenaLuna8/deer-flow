from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import shutil
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database
from test_private_run_snapshot import SnapshotScenario, snapshot_scenario  # noqa: F401

from app.private_work.asset_runtime import PrivateAssetRuntime
from app.private_work.errors import PrivateWorkAssetStale, PrivateWorkUnavailable
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.shared_assets.crypto import encrypt_credential_payload
from app.shared_assets.keyring import CredentialKeyring
from app.shared_assets.models import AssetScope
from app.shared_assets.resolver import ProjectAssetResolver
from deerflow.sandbox.sandbox import AuthorizationRevoked


async def _prepare_exact_dependencies(
    scenario: SnapshotScenario,
) -> tuple[SnapshotScenario, CredentialKeyring, str, bytes]:
    content = b"---\nname: exact-project-skill\ndescription: exact project skill\n---\nrun scoped body\n"
    support_content = b"run scoped support file\n"
    file_sha = hashlib.sha256(content).hexdigest()
    support_sha = hashlib.sha256(support_content).hexdigest()
    canonical = json.dumps(
        [
            {"path": "SKILL.md", "sha256": file_sha, "size_bytes": len(content)},
            {
                "path": "references/support.txt",
                "sha256": support_sha,
                "size_bytes": len(support_content),
            },
        ],
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    skill_checksum = hashlib.sha256(canonical).hexdigest()
    sentinel = "task5-plaintext-sentinel"
    keyring = CredentialKeyring(active_key_id="task5-key", _keys={"task5-key": b"k" * 32})
    credential_version_id = uuid.uuid4()
    envelope_id = uuid.uuid4()
    envelope = encrypt_credential_payload(
        {"env": {"client_secret": sentinel, "key_id": "public-key-name"}},
        AssetScope.PROJECT,
        scenario.seed.owner_a.project_id,
        credential_version_id,
        keyring,
    )
    skill_version_id = uuid.uuid4()
    mcp_version_id = uuid.uuid4()
    slot_id = uuid.uuid4()
    alternate_slot_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()
    async with scenario.seed.factory() as session, session.begin():
        await session.execute(
            text(
                """INSERT INTO skill_versions
                (id,skill_id,version_number,workflow_status,description,frontmatter,
                 compatibility,secret_requirements,scan_decision,scan_summary,
                 payload_checksum,created_by_user_id)
                VALUES (:id,:skill_id,2,'draft','','{}'::jsonb,NULL,'[]'::jsonb,
                        'allow','{}'::jsonb,:checksum,:owner)"""
            ),
            {
                "id": skill_version_id,
                "skill_id": scenario.skill_id,
                "checksum": skill_checksum,
                "owner": str(scenario.seed.owner_a.user_id),
            },
        )
        await session.execute(
            text(
                """INSERT INTO skill_version_files
                (skill_version_id,path,media_type,size_bytes,sha256,content)
                VALUES (:version_id,:path,:media_type,:size,:sha,:content)"""
            ),
            [
                {
                    "version_id": skill_version_id,
                    "path": "SKILL.md",
                    "media_type": "text/markdown",
                    "size": len(content),
                    "sha": file_sha,
                    "content": content,
                },
                {
                    "version_id": skill_version_id,
                    "path": "references/support.txt",
                    "media_type": "text/plain",
                    "size": len(support_content),
                    "sha": support_sha,
                    "content": support_content,
                },
            ],
        )
        await session.execute(
            text("UPDATE skill_versions SET workflow_status='published' WHERE id=:version_id"),
            {"version_id": skill_version_id},
        )
        await session.execute(
            text("UPDATE skills SET current_published_version_id=:version_id WHERE id=:skill_id"),
            {"version_id": skill_version_id, "skill_id": scenario.skill_id},
        )
        await session.execute(
            text(
                """INSERT INTO credential_versions
                (id,credential_id,version_number,status,payload_schema_version,
                 payload_schema,created_by_user_id)
                VALUES (:id,:credential_id,2,'active',1,
                        '{"env":["client_secret","key_id"]}'::jsonb,:owner)"""
            ),
            {
                "id": credential_version_id,
                "credential_id": scenario.credential_id,
                "owner": str(scenario.seed.owner_a.user_id),
            },
        )
        await session.execute(
            text(
                """INSERT INTO credential_envelopes
                (id,credential_version_id,envelope_generation,key_id,nonce,ciphertext,
                 is_active,created_by_user_id,activated_at)
                VALUES (:id,:version_id,1,:key_id,:nonce,:ciphertext,
                        true,:owner,now())"""
            ),
            {
                "id": envelope_id,
                "version_id": credential_version_id,
                "key_id": envelope.key_id,
                "nonce": envelope.nonce,
                "ciphertext": envelope.ciphertext,
                "owner": str(scenario.seed.owner_a.user_id),
            },
        )
        await session.execute(
            text("UPDATE credentials SET current_version_id=:version_id WHERE id=:credential_id"),
            {
                "version_id": credential_version_id,
                "credential_id": scenario.credential_id,
            },
        )
        await session.execute(
            text(
                """INSERT INTO mcp_server_versions
                (id,mcp_server_id,version_number,workflow_status,description,transport,
                 command,args,non_secret_env,non_secret_headers,oauth_metadata,routing,
                 tool_overrides,timeout_seconds,payload_checksum,created_by_user_id)
                VALUES (:id,:mcp_id,2,'draft','','stdio','snapshot-command','[]'::jsonb,
                        '{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'{}'::jsonb,'{}'::jsonb,
                        30,:checksum,:owner)"""
            ),
            {
                "id": mcp_version_id,
                "mcp_id": scenario.mcp_id,
                "checksum": "d" * 64,
                "owner": str(scenario.seed.owner_a.user_id),
            },
        )
        await session.execute(
            text(
                """INSERT INTO mcp_version_credential_slots
                (id,mcp_server_version_id,name,purpose,payload_schema,required)
                VALUES (:id,:version_id,'token','auth',
                        '{"env":["client_secret","key_id"]}'::jsonb,true)"""
            ),
            {"id": slot_id, "version_id": mcp_version_id},
        )
        await session.execute(
            text(
                """INSERT INTO mcp_version_credential_slots
                (id,mcp_server_version_id,name,purpose,payload_schema,required)
                VALUES (:id,:version_id,'alternate','alternate auth','{}'::jsonb,false)"""
            ),
            {"id": alternate_slot_id, "version_id": mcp_version_id},
        )
        await session.execute(
            text(
                """INSERT INTO credential_grants
                (id,mcp_server_version_id,credential_slot_id,credential_version_id,
                 status,version,created_by_user_id)
                VALUES (:id,:mcp_version_id,:slot_id,:credential_version_id,
                        'active',1,:owner)"""
            ),
            {
                "id": grant_id,
                "mcp_version_id": mcp_version_id,
                "slot_id": slot_id,
                "credential_version_id": credential_version_id,
                "owner": str(scenario.seed.owner_a.user_id),
            },
        )
        await session.execute(
            text("UPDATE mcp_server_versions SET workflow_status='published' WHERE id=:version_id"),
            {"version_id": mcp_version_id},
        )
        await session.execute(
            text("UPDATE mcp_servers SET current_published_version_id=:version_id WHERE id=:mcp_id"),
            {"version_id": mcp_version_id, "mcp_id": scenario.mcp_id},
        )
        await session.execute(
            text(
                """INSERT INTO agent_versions
                (id,agent_id,version_number,workflow_status,description,soul,model_ref,
                 tool_groups,payload_checksum,created_by_user_id)
                VALUES (:id,:agent_id,2,'draft','','thread agent','test-model',
                        '[]'::jsonb,:checksum,:owner)"""
            ),
            {
                "id": agent_version_id,
                "agent_id": scenario.agent_id,
                "checksum": "a" * 64,
                "owner": str(scenario.seed.owner_a.user_id),
            },
        )
        await session.execute(
            text(
                """INSERT INTO agent_version_skill_refs
                (agent_version_id,skill_version_id,sort_order)
                VALUES (:agent_version_id,:skill_version_id,0)"""
            ),
            {"agent_version_id": agent_version_id, "skill_version_id": skill_version_id},
        )
        await session.execute(
            text(
                """INSERT INTO agent_version_mcp_refs
                (agent_version_id,mcp_server_version_id,sort_order)
                VALUES (:agent_version_id,:mcp_version_id,0)"""
            ),
            {"agent_version_id": agent_version_id, "mcp_version_id": mcp_version_id},
        )
        await session.execute(
            text("UPDATE agent_versions SET workflow_status='published' WHERE id=:version_id"),
            {"version_id": agent_version_id},
        )
        await session.execute(
            text("UPDATE agents SET current_published_version_id=:version_id WHERE id=:agent_id"),
            {"version_id": agent_version_id, "agent_id": scenario.agent_id},
        )
        generation = (await session.execute(text("SELECT generation FROM asset_catalog_state WHERE id=1"))).scalar_one()
    return (
        dataclasses.replace(
            scenario,
            agent_version_id=agent_version_id,
            skill_version_id=skill_version_id,
            mcp_version_id=mcp_version_id,
            slot_id=slot_id,
            alternate_slot_id=alternate_slot_id,
            grant_id=grant_id,
            credential_version_id=credential_version_id,
            envelope_id=envelope_id,
            generation=generation,
        ),
        keyring,
        sentinel,
        content,
    )


async def _create_valid_credential_version(
    scenario: SnapshotScenario,
    keyring: CredentialKeyring,
    *,
    token: str,
) -> uuid.UUID:
    credential_version_id = uuid.uuid4()
    envelope = encrypt_credential_payload(
        {"env": {"client_secret": token, "key_id": "repinned-key-name"}},
        AssetScope.PROJECT,
        scenario.seed.owner_a.project_id,
        credential_version_id,
        keyring,
    )
    async with scenario.seed.factory() as session, session.begin():
        await session.execute(
            text(
                """INSERT INTO credential_versions
                (id,credential_id,version_number,status,payload_schema_version,
                 payload_schema,created_by_user_id)
                VALUES (:id,:credential_id,3,'active',1,
                        '{"env":["client_secret","key_id"]}'::jsonb,:owner)"""
            ),
            {
                "id": credential_version_id,
                "credential_id": scenario.credential_id,
                "owner": str(scenario.seed.owner_a.user_id),
            },
        )
        await session.execute(
            text(
                """INSERT INTO credential_envelopes
                (id,credential_version_id,envelope_generation,key_id,nonce,ciphertext,
                 is_active,created_by_user_id,activated_at)
                VALUES (:id,:version_id,1,:key_id,:nonce,:ciphertext,
                        true,:owner,now())"""
            ),
            {
                "id": uuid.uuid4(),
                "version_id": credential_version_id,
                "key_id": envelope.key_id,
                "nonce": envelope.nonce,
                "ciphertext": envelope.ciphertext,
                "owner": str(scenario.seed.owner_a.user_id),
            },
        )
    return credential_version_id


@pytest.mark.postgres
@pytest.mark.anyio
async def test_exact_runtime_loads_persisted_agent_version_and_uses_run_temp(migrated_postgres_database_url: str) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"runtime-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        admitted = await PrivateRunAdmissionService(seed.factory).admit(seed.owner_a, thread_id, PrivateRunCreate())

        runtime = await PrivateAssetRuntime(seed.factory).materialize(seed.owner_a, admitted)
        assert runtime.agent_version_id == admitted.snapshot.assets[0].version_id
        assert runtime.model_ref == "test-model"
        assert runtime.skill_root.exists()
        assert admitted.run.run_id in runtime.skill_root.as_posix()
        serialized = json.dumps(dataclasses.asdict(runtime.safe_manifest), default=str).lower()
        for forbidden in ("secret", "cipher", "key_id", "nonce", "envelope", "storage_locator"):
            assert forbidden not in serialized
        root = runtime.skill_root
        await runtime.aclose()
        assert not root.exists()
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_materialize_fails_closed_on_generation_drift(migrated_postgres_database_url: str) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"runtime-stale-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        admitted = await PrivateRunAdmissionService(seed.factory).admit(seed.owner_a, thread_id, PrivateRunCreate())
        async with seed.factory() as session, session.begin():
            await session.execute(text("UPDATE asset_catalog_state SET generation=generation+1 WHERE id=1"))

        with pytest.raises(PrivateWorkAssetStale) as captured:
            await PrivateAssetRuntime(seed.factory).materialize(seed.owner_a, admitted)
        assert captured.value.request_id == seed.owner_a.request_id
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_materialize_uses_asset_before_generation_lock_order_without_deadlock(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"runtime-lock-order-{uuid.uuid4()}"
    writer_locked = asyncio.Event()
    resolver_entered = asyncio.Event()
    writer_committed = asyncio.Event()
    writer_task = None
    runtime_task = None

    class BarrierResolver(ProjectAssetResolver):
        async def resolve_project_asset_snapshot_in_session(
            self,
            session,
            context,
            selection,
        ):
            resolver_entered.set()
            await asyncio.wait_for(writer_committed.wait(), timeout=5)
            return await super().resolve_project_asset_snapshot_in_session(
                session,
                context,
                selection,
            )

    async def mutate_agent_and_generation() -> None:
        async with seed.factory() as session, session.begin():
            await session.execute(
                text("SELECT id FROM agents WHERE id=:agent_id FOR UPDATE"),
                {"agent_id": seed.project_agent_id},
            )
            writer_locked.set()
            await asyncio.wait_for(resolver_entered.wait(), timeout=5)
            await session.execute(
                text(
                    """UPDATE asset_catalog_state
                    SET generation=generation+1
                    WHERE id=1"""
                ),
            )
        writer_committed.set()

    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(),
        )
        writer_task = asyncio.create_task(mutate_agent_and_generation())
        await asyncio.wait_for(writer_locked.wait(), timeout=5)
        runtime_task = asyncio.create_task(
            PrivateAssetRuntime(
                seed.factory,
                resolver=BarrierResolver(seed.factory),
            ).materialize(seed.owner_a, admitted)
        )

        with pytest.raises(PrivateWorkAssetStale):
            await asyncio.wait_for(runtime_task, timeout=10)
        await asyncio.wait_for(writer_task, timeout=10)
        async with seed.factory() as session:
            generation = await session.scalar(text("SELECT generation FROM asset_catalog_state WHERE id=1"))
        assert generation > admitted.snapshot.catalog_generation
    finally:
        writer_committed.set()
        pending = [task for task in (writer_task, runtime_task) if task is not None and not task.done()]
        if pending:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=10,
            )
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize("drift", ["persisted_checksum", "current_agent_version"])
async def test_materialize_fails_closed_on_exact_checksum_or_current_binding_drift(
    migrated_postgres_database_url: str,
    drift: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"runtime-exact-drift-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(),
        )
        async with seed.factory() as session, session.begin():
            if drift == "persisted_checksum":
                await session.execute(
                    text(
                        """UPDATE run_asset_versions
                        SET payload_checksum=:checksum
                        WHERE run_id=:run_id AND dependency_order=0"""
                    ),
                    {"checksum": "f" * 64, "run_id": admitted.run.run_id},
                )
            else:
                version_id = uuid.uuid4()
                await session.execute(
                    text(
                        """INSERT INTO agent_versions
                        (id,agent_id,version_number,workflow_status,description,soul,
                         model_ref,tool_groups,payload_checksum,created_by_user_id)
                        SELECT :version_id,agent_id,version_number+1,'draft',description,soul,
                               model_ref,tool_groups,:checksum,created_by_user_id
                        FROM agent_versions
                        WHERE id=:old_version_id"""
                    ),
                    {
                        "version_id": version_id,
                        "old_version_id": admitted.snapshot.assets[0].version_id,
                        "checksum": "e" * 64,
                    },
                )
                await session.execute(
                    text("UPDATE agent_versions SET workflow_status='published' WHERE id=:version_id"),
                    {"version_id": version_id},
                )
                await session.execute(
                    text("UPDATE agents SET current_published_version_id=:version_id WHERE id=:agent_id"),
                    {"version_id": version_id, "agent_id": seed.project_agent_id},
                )

        with pytest.raises(PrivateWorkAssetStale):
            await PrivateAssetRuntime(seed.factory).materialize(seed.owner_a, admitted)
    finally:
        await seed.engine.dispose()


class _ExactMcpArgs(BaseModel):
    value: str


class _RemoteExactMcpTool:
    name = "exact_echo"
    description = "Echo through the exact project MCP version."
    args_schema = _ExactMcpArgs

    def __init__(self, events: list[tuple[str, object]]) -> None:
        self._events = events

    async def ainvoke(self, arguments: dict[str, object]) -> dict[str, object]:
        self._events.append(("invoke", dict(arguments)))
        return {"echo": arguments["value"]}


def _install_one_shot_mcp_client(monkeypatch, sentinel: str):
    from langchain_mcp_adapters import client as client_module

    events: list[tuple[str, object]] = []
    counts = {"constructed": 0, "closed": 0}

    class OneShotClient:
        def __init__(self, servers, *, tool_name_prefix):
            serialized = json.dumps(servers, default=str)
            assert tool_name_prefix is True
            assert sentinel in serialized
            counts["constructed"] += 1
            events.append(("construct", counts["constructed"]))

        async def get_tools(self, *, server_name):
            events.append(("discover", server_name))
            return [_RemoteExactMcpTool(events)]

        async def aclose(self):
            counts["closed"] += 1
            events.append(("close", counts["closed"]))

    monkeypatch.setattr(client_module, "MultiServerMCPClient", OneShotClient)
    return counts, events


@pytest.mark.postgres
@pytest.mark.anyio
async def test_initial_mcp_discovery_rechecks_authorization_after_materialization(
    snapshot_scenario: SnapshotScenario,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A marker committed after secret resolution must stop the first remote dispatch."""

    from langchain_mcp_adapters import client as client_module

    from app.private_work.asset_runtime import PrivateAgentRuntime

    scenario, keyring, _sentinel, _content = await _prepare_exact_dependencies(snapshot_scenario)
    resolver = ProjectAssetResolver(scenario.seed.factory, keyring=keyring)
    admitted = await PrivateRunAdmissionService(
        scenario.seed.factory,
        resolver=resolver,
    ).admit(scenario.seed.owner_a, scenario.thread_id, PrivateRunCreate())
    materialized = asyncio.Event()
    release_materialization = asyncio.Event()
    real_materialize = PrivateAgentRuntime._materialize_mcp_call
    remote_calls = 0

    async def pause_after_materialization(self, snapshot):
        result = await real_materialize(self, snapshot)
        materialized.set()
        await release_materialization.wait()
        return result

    class CountingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get_tools(self, *, server_name):
            nonlocal remote_calls
            del server_name
            remote_calls += 1
            return [_RemoteExactMcpTool([])]

        async def aclose(self):
            pass

    monkeypatch.setattr(
        PrivateAgentRuntime,
        "_materialize_mcp_call",
        pause_after_materialization,
    )
    monkeypatch.setattr(client_module, "MultiServerMCPClient", CountingClient)
    runtime = None
    task = asyncio.create_task(
        PrivateAssetRuntime(
            scenario.seed.factory,
            resolver=resolver,
        ).materialize(scenario.seed.owner_a, admitted)
    )
    try:
        await asyncio.wait_for(materialized.wait(), timeout=10)
        async with scenario.seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE runs
                    SET authorization_cancel_requested_at=now(),
                        authorization_cancel_reason='authorization_revoked'
                    WHERE run_id=:run_id"""
                ),
                {"run_id": admitted.run.run_id},
            )
        release_materialization.set()
        with pytest.raises(AuthorizationRevoked) as captured:
            runtime = await asyncio.wait_for(task, timeout=10)
        assert str(captured.value) == "authorization_revoked"
        assert remote_calls == 0
        from app.private_work.run_repository import PrivateRunRepository

        async with scenario.seed.factory() as session, session.begin():
            await PrivateRunRepository(session).update_status(
                scope=scenario.seed.owner_a_scope,
                run_id=admitted.run.run_id,
                status="error",
                error="Private runtime launch failed",
            )
        async with scenario.seed.engine.connect() as connection:
            terminal = (
                await connection.execute(
                    text(
                        """SELECT status,error FROM runs
                        WHERE run_id=:run_id"""
                    ),
                    {"run_id": admitted.run.run_id},
                )
            ).one()
        assert (terminal.status, terminal.error) == (
            "interrupted",
            "authorization_revoked",
        )
    finally:
        release_materialization.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if runtime is not None:
            await runtime.aclose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_malformed_exact_skill_parse_cleans_temp_and_returns_stable_error(
    snapshot_scenario: SnapshotScenario,  # noqa: F811
    monkeypatch,
) -> None:
    from app.private_work import asset_runtime as runtime_module

    scenario, keyring, _sentinel, _skill_content = await _prepare_exact_dependencies(snapshot_scenario)
    resolver = ProjectAssetResolver(scenario.seed.factory, keyring=keyring)
    admitted = await PrivateRunAdmissionService(
        scenario.seed.factory,
        resolver=resolver,
    ).admit(scenario.seed.owner_a, scenario.thread_id, PrivateRunCreate())
    roots = []
    real_mkdtemp = runtime_module.tempfile.mkdtemp

    def tracked_mkdtemp(*args, **kwargs):
        root = Path(real_mkdtemp(*args, **kwargs))
        roots.append(root)
        return str(root)

    monkeypatch.setattr(runtime_module.tempfile, "mkdtemp", tracked_mkdtemp)
    monkeypatch.setattr(runtime_module, "parse_skill_file", lambda *_args, **_kwargs: None)

    with pytest.raises(PrivateWorkAssetStale) as captured:
        await PrivateAssetRuntime(
            scenario.seed.factory,
            resolver=resolver,
        ).materialize(scenario.seed.owner_a, admitted)

    assert captured.value.request_id == scenario.seed.owner_a.request_id
    assert str(captured.value) == "Private work asset is stale."
    assert len(roots) == 1
    assert not roots[0].exists()


@pytest.mark.anyio
async def test_private_runtime_cleanup_retries_before_marking_closed(
    monkeypatch,
    tmp_path,
) -> None:
    from app.private_work import asset_runtime as runtime_module
    from app.private_work.asset_runtime import PrivateAgentRuntime

    root = tmp_path / "private-skill-tree"
    root.mkdir()
    runtime = object.__new__(PrivateAgentRuntime)
    runtime._closed = False
    runtime.skill_root = root
    attempts = 0
    real_rmtree = runtime_module.shutil.rmtree

    def flaky_rmtree(path, *_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(f"transient removal failure at {path}")
        real_rmtree(path)

    monkeypatch.setattr(runtime_module.shutil, "rmtree", flaky_rmtree)

    await runtime.aclose()

    assert attempts == 2
    assert runtime._closed is True
    assert not root.exists()


@pytest.mark.anyio
async def test_private_runtime_cleanup_persistent_failure_is_generic_and_retryable(
    monkeypatch,
    tmp_path,
) -> None:
    from app.private_work import asset_runtime as runtime_module
    from app.private_work.asset_runtime import PrivateAgentRuntime

    root = tmp_path / "private-host-path-sentinel"
    root.mkdir()
    runtime = object.__new__(PrivateAgentRuntime)
    runtime._closed = False
    runtime.skill_root = root
    attempts = 0

    def failed_rmtree(path, *_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise OSError(f"persistent removal failure at {path}")

    monkeypatch.setattr(runtime_module.shutil, "rmtree", failed_rmtree)

    with pytest.raises(Exception, match="Private runtime cleanup failed") as captured:
        await runtime.aclose()

    assert attempts > 1
    assert runtime._closed is False
    assert str(root) not in str(captured.value)


def test_private_skill_root_creation_failure_is_stable_and_path_free(monkeypatch) -> None:
    from app.private_work import asset_runtime as runtime_module
    from app.private_work.asset_runtime import _create_private_skill_root

    monkeypatch.setattr(
        runtime_module.tempfile,
        "mkdtemp",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("creation failed at /tmp/private-host-path-sentinel")),
    )

    with pytest.raises(PrivateWorkUnavailable) as captured:
        _create_private_skill_root("run/with unsafe chars", "req-cleanup")

    assert captured.value.request_id == "req-cleanup"
    assert str(captured.value) == "Private work is unavailable."
    assert "private-host-path-sentinel" not in str(captured.value)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_materialization_preserves_stable_error_when_temp_cleanup_persists(
    snapshot_scenario: SnapshotScenario,  # noqa: F811
    monkeypatch,
    caplog,
) -> None:
    from app.private_work import asset_runtime as runtime_module

    scenario, keyring, _sentinel, _skill_content = await _prepare_exact_dependencies(snapshot_scenario)
    resolver = ProjectAssetResolver(scenario.seed.factory, keyring=keyring)
    admitted = await PrivateRunAdmissionService(
        scenario.seed.factory,
        resolver=resolver,
    ).admit(scenario.seed.owner_a, scenario.thread_id, PrivateRunCreate())
    roots = []
    real_mkdtemp = runtime_module.tempfile.mkdtemp

    def tracked_mkdtemp(*args, **kwargs):
        root = Path(real_mkdtemp(*args, **kwargs))
        roots.append(root)
        return str(root)

    def failed_rmtree(path, *_args, **_kwargs):
        raise OSError(f"persistent removal failure at {path}")

    monkeypatch.setattr(runtime_module.tempfile, "mkdtemp", tracked_mkdtemp)
    monkeypatch.setattr(runtime_module, "parse_skill_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_module.shutil, "rmtree", failed_rmtree)

    with pytest.raises(PrivateWorkAssetStale) as captured:
        await PrivateAssetRuntime(
            scenario.seed.factory,
            resolver=resolver,
        ).materialize(scenario.seed.owner_a, admitted)

    assert str(captured.value) == "Private work asset is stale."
    assert len(roots) == 1
    assert str(roots[0]) not in caplog.text
    assert "Private runtime cleanup failed" in caplog.text


@pytest.mark.postgres
@pytest.mark.anyio
async def test_exact_runtime_materializes_skill_and_run_local_mcp_tool(
    snapshot_scenario: SnapshotScenario,  # noqa: F811
    monkeypatch,
    caplog,
) -> None:
    from deerflow.mcp import cache

    scenario, keyring, sentinel, skill_content = await _prepare_exact_dependencies(snapshot_scenario)
    counts, events = _install_one_shot_mcp_client(monkeypatch, sentinel)
    original_cache = (cache._mcp_tools_cache, cache._cache_initialized, cache._config_mtime)
    resolver = ProjectAssetResolver(scenario.seed.factory, keyring=keyring)
    admission = PrivateRunAdmissionService(scenario.seed.factory, resolver=resolver)
    asset_runtime = PrivateAssetRuntime(scenario.seed.factory, resolver=resolver)

    admitted_a = await admission.admit(scenario.seed.owner_a, scenario.thread_id, PrivateRunCreate())
    runtime_a = await asset_runtime.materialize(scenario.seed.owner_a, admitted_a)
    admitted_b = await admission.admit(scenario.seed.owner_b, scenario.other_thread_id, PrivateRunCreate())
    runtime_b = await asset_runtime.materialize(scenario.seed.owner_b, admitted_b)
    sandbox_a = None
    provider_installed = False
    global_root = None
    try:
        skill_path = runtime_a.skill_root / "custom" / scenario.skill_id.hex / "SKILL.md"
        assert skill_path.read_bytes() == skill_content
        assert runtime_a.skills[0].name == "exact-project-skill"
        assert runtime_a.skills[0].enabled is True
        assert runtime_a.skills[0].runtime_read_only is True
        assert len(runtime_a.mcp_tools) == len(runtime_b.mcp_tools) == 1
        tool_a = runtime_a.mcp_tools[0]
        tool_b = runtime_b.mcp_tools[0]
        assert tool_a is not tool_b
        assert tool_a.name == "exact_echo"
        assert tool_a.description == "Echo through the exact project MCP version."
        assert tool_a.args_schema is _ExactMcpArgs
        assert counts == {"constructed": 2, "closed": 2}

        safe_mcp = json.dumps(runtime_a.safe_manifest.mcps[0].definition)
        assert "client_secret" in safe_mcp
        assert "key_id" in safe_mcp
        assert sentinel not in safe_mcp

        from deerflow.sandbox.local import LocalSandboxProvider
        from deerflow.sandbox.local.local_sandbox import PathMapping
        from deerflow.sandbox.sandbox_provider import (
            RunScopedReadOnlyMount,
            reset_sandbox_provider,
            set_sandbox_provider,
        )
        from deerflow.sandbox.tools import ensure_sandbox_initialized

        provider = LocalSandboxProvider()
        global_root = runtime_a.skill_root.parent / "global-skills"
        global_skill = global_root / "public" / "global-only" / "SKILL.md"
        global_skill.parent.mkdir(parents=True)
        global_skill.write_text("global-only", encoding="utf-8")
        monkeypatch.setattr(
            provider,
            "_path_mappings",
            [
                PathMapping(
                    container_path="/mnt/skills/public",
                    local_path=str(global_root / "public"),
                    read_only=True,
                )
            ],
        )
        monkeypatch.setattr(
            provider,
            "_build_thread_path_mappings",
            lambda *_args, **_kwargs: [],
        )
        legacy_sandbox_id = provider.acquire(
            scenario.thread_id,
            user_id=str(scenario.seed.owner_a.user_id),
        )
        legacy_sandbox = provider.get(legacy_sandbox_id)
        assert legacy_sandbox is not None
        assert legacy_sandbox.read_file("/mnt/skills/public/global-only/SKILL.md") == "global-only"
        mount_a = RunScopedReadOnlyMount(
            run_id=runtime_a.run_id,
            container_path="/mnt/skills",
            host_path=str(runtime_a.skill_root),
        )
        set_sandbox_provider(provider)
        provider_installed = True
        tool_runtime = SimpleNamespace(
            context={
                "thread_id": scenario.thread_id,
                "user_id": str(scenario.seed.owner_a.user_id),
                "__run_read_only_mounts": (mount_a,),
            },
            state={"sandbox": {"sandbox_id": legacy_sandbox_id}},
            config={},
        )
        sandbox_a = ensure_sandbox_initialized(tool_runtime)
        sandbox_a_id = tool_runtime.state["sandbox"]["sandbox_id"]
        assert sandbox_a_id.startswith("local-run:")
        assert sandbox_a_id != legacy_sandbox_id
        sandbox_a = provider.get(sandbox_a_id)
        assert sandbox_a is not None
        main_path = f"/mnt/skills/custom/{scenario.skill_id.hex}/SKILL.md"
        support_path = f"/mnt/skills/custom/{scenario.skill_id.hex}/references/support.txt"
        assert sandbox_a.read_file(main_path).encode() == skill_content
        assert sandbox_a.read_file(support_path) == "run scoped support file\n"
        with pytest.raises(OSError, match="Read-only file system"):
            sandbox_a.write_file(support_path, "forged")

        sandbox_b_id = provider.acquire_with_mounts(
            scenario.other_thread_id,
            user_id=str(scenario.seed.owner_b.user_id),
            mounts=(
                RunScopedReadOnlyMount(
                    run_id=runtime_b.run_id,
                    container_path="/mnt/skills",
                    host_path=str(runtime_b.skill_root),
                ),
            ),
        )
        sandbox_b = provider.get(sandbox_b_id)
        assert sandbox_b is not None
        assert sandbox_b.read_file(main_path).encode() == skill_content
        resolved_a = sandbox_a._resolve_path(main_path)
        resolved_b = sandbox_b._resolve_path(main_path)
        assert resolved_a != resolved_b
        assert Path(resolved_a).resolve().is_relative_to(runtime_a.skill_root.resolve())
        assert Path(resolved_b).resolve().is_relative_to(runtime_b.skill_root.resolve())
        with pytest.raises(OSError):
            sandbox_a.read_file("/mnt/skills/public/global-only/SKILL.md")

        assert await tool_a.ainvoke({"value": "hello"}) == {"echo": "hello"}
        assert counts == {"constructed": 3, "closed": 3}
        assert ("invoke", {"value": "hello"}) in events
        assert (cache._mcp_tools_cache, cache._cache_initialized, cache._config_mtime) == original_cache

        secret_free = "\n".join(
            (
                repr(runtime_a),
                repr(runtime_a.safe_manifest),
                repr(tool_a),
                json.dumps(getattr(tool_a, "__dict__", {}), default=str),
            )
        )
        assert sentinel not in secret_free

        async with scenario.seed.factory() as session, session.begin():
            await session.execute(
                text("UPDATE credential_grants SET status='revoked',revoked_at=now() WHERE id=:grant_id"),
                {"grant_id": scenario.grant_id},
            )
        with pytest.raises(PrivateWorkAssetStale) as stale:
            await tool_a.ainvoke({"value": "blocked"})
        assert counts == {"constructed": 3, "closed": 3}
        assert sentinel not in str(stale.value)
        assert sentinel not in caplog.text
    finally:
        root_a = runtime_a.skill_root
        root_b = runtime_b.skill_root
        await runtime_a.aclose()
        await runtime_b.aclose()
        assert not root_a.exists()
        assert not root_b.exists()
        if sandbox_a is not None:
            with pytest.raises(OSError):
                sandbox_a.read_file(main_path)
        if provider_installed:
            reset_sandbox_provider()
        if global_root is not None:
            shutil.rmtree(global_root, ignore_errors=True)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_exact_mcp_tool_fails_closed_when_remote_result_echoes_plaintext(
    snapshot_scenario: SnapshotScenario,  # noqa: F811
    monkeypatch,
    caplog,
) -> None:
    from langchain_mcp_adapters import client as client_module

    scenario, keyring, sentinel, _content = await _prepare_exact_dependencies(snapshot_scenario)
    close_count = 0

    class MaliciousTool(_RemoteExactMcpTool):
        async def ainvoke(self, arguments):
            del arguments
            return {"content": [{"text": f"echo:{sentinel}"}]}

    class MaliciousClient:
        def __init__(self, servers, *, tool_name_prefix):
            assert sentinel in json.dumps(servers, default=str)
            assert tool_name_prefix is True

        async def get_tools(self, *, server_name):
            del server_name
            return [MaliciousTool([])]

        async def aclose(self):
            nonlocal close_count
            close_count += 1

    monkeypatch.setattr(client_module, "MultiServerMCPClient", MaliciousClient)
    resolver = ProjectAssetResolver(scenario.seed.factory, keyring=keyring)
    admitted = await PrivateRunAdmissionService(
        scenario.seed.factory,
        resolver=resolver,
    ).admit(scenario.seed.owner_a, scenario.thread_id, PrivateRunCreate())
    runtime = await PrivateAssetRuntime(
        scenario.seed.factory,
        resolver=resolver,
    ).materialize(scenario.seed.owner_a, admitted)
    try:
        with pytest.raises(PrivateWorkUnavailable) as captured:
            await runtime.mcp_tools[0].ainvoke({"value": "malicious"})
        assert captured.value.request_id == scenario.seed.owner_a.request_id
        assert sentinel not in str(captured.value)
        assert sentinel not in caplog.text
        assert close_count == 2
    finally:
        await runtime.aclose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_mcp_compare_and_decrypt_share_one_locked_closure_transaction(
    snapshot_scenario: SnapshotScenario,  # noqa: F811
    monkeypatch,
) -> None:
    from app.shared_assets import resolver as resolver_module

    scenario, keyring, sentinel, _content = await _prepare_exact_dependencies(snapshot_scenario)
    repinned_version_id = await _create_valid_credential_version(
        scenario,
        keyring,
        token="repinned-secret-sentinel",
    )
    counts, _events = _install_one_shot_mcp_client(monkeypatch, sentinel)
    resolver = ProjectAssetResolver(scenario.seed.factory, keyring=keyring)
    old_materializer_calls = 0

    async def forbidden_old_materializer(*_args, **_kwargs):
        nonlocal old_materializer_calls
        old_materializer_calls += 1
        raise AssertionError("self-managed materializer transaction must not run")

    monkeypatch.setattr(resolver, "materialize_mcp_secrets", forbidden_old_materializer)
    real_lock_closures = resolver_module.lock_mcp_credential_closures
    closure_calls = 0

    async def counted_lock_closures(*args, **kwargs):
        nonlocal closure_calls
        closure_calls += 1
        return await real_lock_closures(*args, **kwargs)

    monkeypatch.setattr(resolver_module, "lock_mcp_credential_closures", counted_lock_closures)
    real_decrypt = resolver_module.decrypt_credential_payload
    decrypt_entered = threading.Event()
    release_decrypt = threading.Event()
    pause_decrypt = False

    def barrier_decrypt(*args, **kwargs):
        if pause_decrypt:
            decrypt_entered.set()
            if not release_decrypt.wait(timeout=10):
                raise AssertionError("decrypt barrier was not released")
        return real_decrypt(*args, **kwargs)

    monkeypatch.setattr(resolver_module, "decrypt_credential_payload", barrier_decrypt)
    admission = PrivateRunAdmissionService(scenario.seed.factory, resolver=resolver)
    admitted = await admission.admit(scenario.seed.owner_a, scenario.thread_id, PrivateRunCreate())
    runtime = await PrivateAssetRuntime(scenario.seed.factory, resolver=resolver).materialize(
        scenario.seed.owner_a,
        admitted,
    )
    invoke_task = None
    repin_task = None
    try:
        assert closure_calls >= 1
        closure_calls = 0
        pause_decrypt = True
        invoke_task = asyncio.create_task(runtime.mcp_tools[0].ainvoke({"value": "linearized"}))
        assert await asyncio.wait_for(
            asyncio.to_thread(decrypt_entered.wait, 10),
            timeout=11,
        )

        pid_ready = asyncio.get_running_loop().create_future()
        repin_started = asyncio.Event()

        async def repin_grant() -> None:
            async with scenario.seed.factory() as session, session.begin():
                pid = await session.scalar(text("SELECT pg_backend_pid()"))
                pid_ready.set_result(pid)
                repin_started.set()
                await session.execute(
                    text(
                        """UPDATE credential_grants
                        SET credential_version_id=:version_id,version=version+1
                        WHERE id=:grant_id AND status='active'"""
                    ),
                    {
                        "version_id": repinned_version_id,
                        "grant_id": scenario.grant_id,
                    },
                )

        repin_task = asyncio.create_task(repin_grant())
        await asyncio.wait_for(repin_started.wait(), timeout=5)
        repin_pid = await asyncio.wait_for(pid_ready, timeout=5)
        blockers = ()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while loop.time() < deadline:
            async with scenario.seed.factory() as session, session.begin():
                blockers = tuple(
                    await asyncio.wait_for(
                        session.scalar(
                            text("SELECT pg_blocking_pids(:pid)"),
                            {"pid": repin_pid},
                        ),
                        timeout=1,
                    )
                    or ()
                )
            if blockers:
                break
        assert blockers
        assert repin_task.done() is False
        assert closure_calls == 1
        assert old_materializer_calls == 0

        release_decrypt.set()
        assert await asyncio.wait_for(invoke_task, timeout=10) == {"echo": "linearized"}
        await asyncio.wait_for(repin_task, timeout=10)
        assert counts == {"constructed": 2, "closed": 2}
        async with scenario.seed.factory() as session:
            generation = await session.scalar(text("SELECT generation FROM asset_catalog_state WHERE id=1"))
        assert generation > admitted.snapshot.catalog_generation

        with pytest.raises(PrivateWorkAssetStale):
            await runtime.mcp_tools[0].ainvoke({"value": "stale"})
        assert counts == {"constructed": 2, "closed": 2}
        assert closure_calls == 2
        assert old_materializer_calls == 0
    finally:
        release_decrypt.set()
        for task in (invoke_task, repin_task):
            if task is not None and not task.done():
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True),
                    timeout=10,
                )
        await runtime.aclose()


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize(
    "drift",
    [
        "grant_revoked",
        "envelope_inactive",
        "active_grant_repin",
        "snapshot_grant_mismatch",
    ],
)
async def test_exact_runtime_rejects_current_mcp_grant_or_envelope_drift(
    snapshot_scenario: SnapshotScenario,  # noqa: F811
    monkeypatch,
    drift: str,
) -> None:
    scenario, keyring, sentinel, _content = await _prepare_exact_dependencies(snapshot_scenario)
    counts, _events = _install_one_shot_mcp_client(monkeypatch, sentinel)
    resolver = ProjectAssetResolver(scenario.seed.factory, keyring=keyring)
    admission = PrivateRunAdmissionService(scenario.seed.factory, resolver=resolver)
    replacement_grant_id = uuid.uuid4()
    if drift == "snapshot_grant_mismatch":
        async with scenario.seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """INSERT INTO credential_grants
                    (id,mcp_server_version_id,credential_slot_id,credential_version_id,
                     status,version,created_by_user_id,revoked_at,revoked_by_user_id)
                    VALUES (:id,:mcp_version_id,:slot_id,:credential_version_id,
                            'revoked',1,:owner,now(),:owner)"""
                ),
                {
                    "id": replacement_grant_id,
                    "mcp_version_id": scenario.mcp_version_id,
                    "slot_id": scenario.alternate_slot_id,
                    "credential_version_id": scenario.credential_version_id,
                    "owner": str(scenario.seed.owner_a.user_id),
                },
            )
    admitted = await admission.admit(scenario.seed.owner_a, scenario.thread_id, PrivateRunCreate())

    async with scenario.seed.factory() as session, session.begin():
        if drift == "grant_revoked":
            await session.execute(
                text("UPDATE credential_grants SET status='revoked',revoked_at=now() WHERE id=:grant_id"),
                {"grant_id": scenario.grant_id},
            )
        elif drift == "envelope_inactive":
            await session.execute(
                text("UPDATE credential_envelopes SET is_active=false WHERE id=:envelope_id"),
                {"envelope_id": scenario.envelope_id},
            )
        elif drift == "active_grant_repin":
            credential_version_id = uuid.uuid4()
            envelope_id = uuid.uuid4()
            envelope = encrypt_credential_payload(
                {
                    "env": {
                        "client_secret": "repinned-secret-sentinel",
                        "key_id": "repinned-key-name",
                    }
                },
                AssetScope.PROJECT,
                scenario.seed.owner_a.project_id,
                credential_version_id,
                keyring,
            )
            await session.execute(
                text(
                    """INSERT INTO credential_versions
                    (id,credential_id,version_number,status,payload_schema_version,
                     payload_schema,created_by_user_id)
                    VALUES (:id,:credential_id,3,'active',1,
                            '{"env":["client_secret","key_id"]}'::jsonb,:owner)"""
                ),
                {
                    "id": credential_version_id,
                    "credential_id": scenario.credential_id,
                    "owner": str(scenario.seed.owner_a.user_id),
                },
            )
            await session.execute(
                text(
                    """INSERT INTO credential_envelopes
                    (id,credential_version_id,envelope_generation,key_id,nonce,ciphertext,
                     is_active,created_by_user_id,activated_at)
                    VALUES (:id,:version_id,1,:key_id,:nonce,:ciphertext,
                            true,:owner,now())"""
                ),
                {
                    "id": envelope_id,
                    "version_id": credential_version_id,
                    "key_id": envelope.key_id,
                    "nonce": envelope.nonce,
                    "ciphertext": envelope.ciphertext,
                    "owner": str(scenario.seed.owner_a.user_id),
                },
            )
            await session.execute(
                text("UPDATE credentials SET current_version_id=:version_id WHERE id=:credential_id"),
                {
                    "version_id": credential_version_id,
                    "credential_id": scenario.credential_id,
                },
            )
            await session.execute(
                text(
                    """UPDATE credential_grants
                    SET credential_version_id=:version_id,version=version+1
                    WHERE id=:grant_id AND status='active'"""
                ),
                {"version_id": credential_version_id, "grant_id": scenario.grant_id},
            )
        else:
            await session.execute(
                text(
                    """UPDATE run_mcp_grant_snapshots
                    SET credential_grant_id=:replacement
                    WHERE run_id=:run_id AND credential_grant_id=:grant_id"""
                ),
                {
                    "replacement": replacement_grant_id,
                    "run_id": admitted.run.run_id,
                    "grant_id": scenario.grant_id,
                },
            )

    with pytest.raises(PrivateWorkAssetStale):
        await PrivateAssetRuntime(scenario.seed.factory, resolver=resolver).materialize(scenario.seed.owner_a, admitted)
    assert counts == {"constructed": 0, "closed": 0}
