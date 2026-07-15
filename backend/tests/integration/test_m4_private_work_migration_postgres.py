from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from deerflow.persistence.bootstrap import _get_alembic_config
from scripts import setup_postgres
from scripts.migrate_private_work import (
    PrivateWorkMigrationError,
    run_private_work_migration,
)
from scripts.migrate_sqlite_to_postgres import _run_cli


async def _upgrade(url: str, revision: str) -> None:
    engine = create_async_engine(url)
    try:
        await asyncio.to_thread(command.upgrade, _get_alembic_config(engine), revision)
    finally:
        await engine.dispose()


async def _seed_legacy_private_work(url: str) -> tuple[str, uuid.UUID]:
    owner = str(uuid.uuid4())
    project = uuid.uuid4()
    membership = uuid.uuid4()
    agent = uuid.uuid4()
    agent_version = uuid.uuid4()
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,password_hash,system_role,created_at,oauth_provider,oauth_id,needs_setup,token_version)
                    VALUES (:owner,:email,NULL,'user',now(),NULL,NULL,false,0)"""
                ),
                {"owner": owner, "email": f"{owner}@example.invalid"},
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                    (id,slug,display_name,description,icon,status,is_suspended,membership_version,created_by_user_id,created_at,updated_at)
                    VALUES (:project,:slug,'Migration target','','folder','active',false,1,:owner,now(),now())"""
                ),
                {"project": project, "slug": f"p-{project.hex[:12]}", "owner": owner},
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                    (id,project_id,user_id,role,status,version,is_pinned,created_at,updated_at)
                    VALUES (:membership,:project,:owner,'admin','active',1,false,now(),now())"""
                ),
                {"membership": membership, "project": project, "owner": owner},
            )
            await connection.execute(
                text(
                    """INSERT INTO agents
                    (id,scope,project_id,slug,display_name,status,current_published_version_id,version,source_key,created_by_user_id,created_at,updated_at)
                    VALUES (:agent,'system',NULL,'lead-agent','Lead Agent','active',NULL,1,'system-agent:lead-agent',:owner,now(),now())"""
                ),
                {"agent": agent, "owner": owner},
            )
            await connection.execute(
                text(
                    """INSERT INTO agent_versions
                    (id,agent_id,version_number,workflow_status,description,soul,model_ref,tool_groups,
                     supersedes_version_id,payload_checksum,submitted_at,reviewed_at,reviewed_by_user_id,review_note,
                     created_by_user_id,created_at)
                    VALUES (:version,:agent,1,'published','','','test-model','[]'::jsonb,NULL,:checksum,
                            NULL,NULL,NULL,NULL,:owner,now())"""
                ),
                {"version": agent_version, "agent": agent, "checksum": "c" * 64, "owner": owner},
            )
            await connection.execute(
                text("UPDATE agents SET current_published_version_id=:version WHERE id=:agent"),
                {"version": agent_version, "agent": agent},
            )
            await connection.execute(
                text(
                    """INSERT INTO threads_meta
                    (thread_id,assistant_id,user_id,display_name,status,metadata_json,created_at,updated_at)
                    VALUES ('thread-1','lead-agent',:owner,'Private title','idle',CAST(:metadata AS JSONB),now(),now())"""
                ),
                {"owner": owner, "metadata": json.dumps({"private_note": "preserve-thread"})},
            )
            await connection.execute(
                text(
                    """INSERT INTO runs
                    (run_id,thread_id,assistant_id,user_id,status,model_name,multitask_strategy,metadata_json,kwargs_json,error,
                     message_count,first_human_message,last_ai_message,total_input_tokens,total_output_tokens,total_tokens,
                     llm_call_count,lead_agent_tokens,subagent_tokens,middleware_tokens,token_usage_by_model,
                     follow_up_to_run_id,created_at,updated_at)
                    VALUES ('run-1','thread-1','lead-agent',:owner,'success','test-model','reject',CAST(:metadata AS JSONB),CAST(:kwargs AS JSONB),NULL,
                            2,'hello','world',1,2,3,1,3,0,0,'{}'::jsonb,NULL,now(),now())"""
                ),
                {
                    "owner": owner,
                    "metadata": json.dumps({"private_note": "preserve-run"}),
                    "kwargs": json.dumps({"input": {"messages": [{"role": "user", "content": "secret prompt"}]}}),
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO run_events
                    (thread_id,run_id,user_id,event_type,category,content,event_metadata,seq,created_at)
                    VALUES ('thread-1','run-1',:owner,'message','message','private event',CAST(:metadata AS JSONB),1,now())"""
                ),
                {"owner": owner, "metadata": json.dumps({"message_id": "message-1"})},
            )
            await connection.execute(
                text(
                    """INSERT INTO feedback
                    (feedback_id,run_id,thread_id,user_id,message_id,rating,comment,created_at)
                    VALUES ('feedback-1','run-1','thread-1',:owner,'message-1',1,'private feedback',now())"""
                ),
                {"owner": owner},
            )
            await connection.execute(
                text(
                    """CREATE TABLE checkpoints (
                        thread_id TEXT NOT NULL,
                        checkpoint_ns TEXT NOT NULL DEFAULT '',
                        checkpoint_id TEXT NOT NULL,
                        parent_checkpoint_id TEXT,
                        type TEXT,
                        checkpoint BYTEA NOT NULL,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                    )"""
                )
            )
            await connection.execute(
                text(
                    """CREATE TABLE checkpoint_blobs (
                        thread_id TEXT NOT NULL,
                        checkpoint_ns TEXT NOT NULL DEFAULT '',
                        channel TEXT NOT NULL,
                        version TEXT NOT NULL,
                        type TEXT NOT NULL,
                        blob BYTEA,
                        PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
                    )"""
                )
            )
            await connection.execute(
                text(
                    """CREATE TABLE checkpoint_writes (
                        thread_id TEXT NOT NULL,
                        checkpoint_ns TEXT NOT NULL DEFAULT '',
                        checkpoint_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        idx INTEGER NOT NULL,
                        channel TEXT NOT NULL,
                        type TEXT,
                        blob BYTEA NOT NULL,
                        task_path TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                    )"""
                )
            )
            await connection.execute(
                text(
                    """INSERT INTO checkpoints
                    (thread_id,checkpoint_ns,checkpoint_id,type,checkpoint,metadata)
                    VALUES ('thread-1','','checkpoint-1','json',:payload,:metadata)"""
                ),
                {"payload": b"checkpoint-payload", "metadata": json.dumps({"step": 1})},
            )
            await connection.execute(
                text(
                    """INSERT INTO checkpoint_blobs
                    (thread_id,checkpoint_ns,channel,version,type,blob)
                    VALUES ('thread-1','','messages','1','bytes',:payload)"""
                ),
                {"payload": b"blob-payload"},
            )
            await connection.execute(
                text(
                    """INSERT INTO checkpoint_writes
                    (thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,blob,task_path)
                    VALUES ('thread-1','','checkpoint-1','task-1',0,'messages','bytes',:payload,'')"""
                ),
                {"payload": b"write-payload"},
            )
    finally:
        await engine.dispose()
    return owner, project


def _write_legacy_private_sqlite(path: Path, owner: str) -> None:
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,email TEXT,password_hash TEXT,system_role TEXT,
                created_at TEXT,oauth_provider TEXT,oauth_id TEXT,needs_setup INTEGER,
                token_version INTEGER
            );
            CREATE TABLE threads_meta (
                thread_id TEXT PRIMARY KEY,assistant_id TEXT,user_id TEXT,display_name TEXT,
                status TEXT,metadata_json TEXT,created_at TEXT,updated_at TEXT
            );
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,thread_id TEXT,assistant_id TEXT,user_id TEXT,
                status TEXT,model_name TEXT,multitask_strategy TEXT,metadata_json TEXT,
                kwargs_json TEXT,error TEXT,message_count INTEGER,first_human_message TEXT,
                last_ai_message TEXT,total_input_tokens INTEGER,total_output_tokens INTEGER,
                total_tokens INTEGER,llm_call_count INTEGER,lead_agent_tokens INTEGER,
                subagent_tokens INTEGER,middleware_tokens INTEGER,token_usage_by_model TEXT,
                follow_up_to_run_id TEXT,created_at TEXT,updated_at TEXT
            );
            CREATE TABLE run_events (
                id INTEGER PRIMARY KEY,thread_id TEXT,run_id TEXT,user_id TEXT,event_type TEXT,
                category TEXT,content TEXT,event_metadata TEXT,seq INTEGER,created_at TEXT
            );
            CREATE TABLE feedback (
                feedback_id TEXT PRIMARY KEY,run_id TEXT,thread_id TEXT,user_id TEXT,
                message_id TEXT,rating INTEGER,comment TEXT,created_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?)",
            (owner, f"{owner}@example.invalid", None, "user", now, None, None, 0, 0),
        )
        connection.execute(
            "INSERT INTO threads_meta VALUES (?,?,?,?,?,?,?,?)",
            (
                "sqlite-thread-1",
                "lead-agent",
                owner,
                "SQLite private title",
                "idle",
                json.dumps({"private_note": "sqlite-thread"}),
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sqlite-run-1",
                "sqlite-thread-1",
                "lead-agent",
                owner,
                "success",
                "test-model",
                "reject",
                json.dumps({"private_note": "sqlite-run"}),
                json.dumps({"input": {"messages": [{"role": "user", "content": "sqlite secret"}]}}),
                None,
                2,
                "hello",
                "world",
                1,
                2,
                3,
                1,
                3,
                0,
                0,
                json.dumps({}),
                None,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO run_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                1,
                "sqlite-thread-1",
                "sqlite-run-1",
                owner,
                "message",
                "message",
                "sqlite private event",
                json.dumps({"message_id": "sqlite-message-1"}),
                1,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?)",
            (
                "sqlite-feedback-1",
                "sqlite-run-1",
                "sqlite-thread-1",
                owner,
                "sqlite-message-1",
                1,
                "sqlite private feedback",
                now,
            ),
        )


async def _seed_sqlite_migration_authority(
    url: str,
    *,
    owner: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    project = uuid.uuid4()
    membership = uuid.uuid4()
    agent = uuid.uuid4()
    agent_version = uuid.uuid4()
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO projects
                    (id,slug,display_name,description,icon,status,is_suspended,
                     membership_version,created_by_user_id,created_at,updated_at)
                    VALUES (:project,:slug,'SQLite migration target','','folder','active',
                            false,1,:owner,now(),now())"""
                ),
                {"project": project, "slug": f"sqlite-{project.hex[:12]}", "owner": owner},
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships
                    (id,project_id,user_id,role,status,version,is_pinned,created_at,updated_at)
                    VALUES (:membership,:project,:owner,'admin','active',1,false,now(),now())"""
                ),
                {"membership": membership, "project": project, "owner": owner},
            )
            await connection.execute(
                text(
                    """INSERT INTO agents
                    (id,scope,project_id,slug,display_name,status,current_published_version_id,
                     version,source_key,created_by_user_id,created_at,updated_at)
                    VALUES (:agent,'system',NULL,'lead-agent','Lead Agent','active',NULL,1,
                            'system-agent:lead-agent',:owner,now(),now())"""
                ),
                {"agent": agent, "owner": owner},
            )
            await connection.execute(
                text(
                    """INSERT INTO agent_versions
                    (id,agent_id,version_number,workflow_status,description,soul,model_ref,
                     tool_groups,supersedes_version_id,payload_checksum,submitted_at,reviewed_at,
                     reviewed_by_user_id,review_note,created_by_user_id,created_at)
                    VALUES (:version,:agent,1,'published','','','test-model','[]'::jsonb,NULL,
                            :checksum,NULL,NULL,NULL,NULL,:owner,now())"""
                ),
                {
                    "version": agent_version,
                    "agent": agent,
                    "checksum": "d" * 64,
                    "owner": owner,
                },
            )
            await connection.execute(
                text("UPDATE agents SET current_published_version_id=:version WHERE id=:agent"),
                {"version": agent_version, "agent": agent},
            )
    finally:
        await engine.dispose()
    return project, agent


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_sqlite_private_rows_reach_final_m4_scope_through_0007(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    setup = await setup_postgres.setup_m4_migration_target(postgres_database_url)
    assert setup.revision == "0007_project_shared_assets"

    owner = str(uuid.uuid4())
    source = tmp_path / "legacy-private.sqlite"
    backup_dir = tmp_path / "sqlite-backups"
    _write_legacy_private_sqlite(source, owner)
    await _run_cli(
        SimpleNamespace(
            source=[source],
            dry_run=False,
            backup_dir=backup_dir,
            m4_staging_target=True,
            reconcile_users_by_email=False,
            reconcile_expected_conflicts=None,
            reconcile_source_sha256=[],
        ),
        postgres_database_url,
    )
    assert len(list(backup_dir.glob("*.bak"))) == 1

    project, agent = await _seed_sqlite_migration_authority(
        postgres_database_url,
        owner=owner,
    )
    result = await run_private_work_migration(
        postgres_database_url,
        owner_map={owner: project},
        repo_root=tmp_path / "repo",
        data_root=tmp_path / "data",
        backup_dir=tmp_path / "m4-backups",
        execute=True,
    )
    assert result.cutover_complete is True

    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0011_private_artifact_tombstone"
            scoped_thread = (
                (
                    await connection.execute(
                        text(
                            """SELECT project_id,owner_user_id,agent_asset_id,agent_scope
                           FROM threads_meta WHERE thread_id='sqlite-thread-1'"""
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert scoped_thread == {
                "project_id": project,
                "owner_user_id": owner,
                "agent_asset_id": agent,
                "agent_scope": "system",
            }
            assert await connection.scalar(text("SELECT project_id FROM runs WHERE run_id='sqlite-run-1'")) == project
            assert await connection.scalar(text("SELECT owner_user_id FROM run_events WHERE run_id='sqlite-run-1'")) == owner
            assert await connection.scalar(text("SELECT comment FROM feedback WHERE run_id='sqlite-run-1'")) == "sqlite private feedback"
            assert await connection.scalar(text("SELECT stage FROM private_work_cutover_state WHERE id=1")) == "cutover_complete"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_dry_run_is_zero_write_and_execute_migrates_core_rows_idempotently(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    await _upgrade(postgres_database_url, "0007_project_shared_assets")
    owner, project = await _seed_legacy_private_work(postgres_database_url)
    backup_dir = tmp_path / "backup"

    preview = await run_private_work_migration(
        postgres_database_url,
        owner_map={owner: project},
        repo_root=tmp_path / "repo",
        data_root=tmp_path / "data",
        backup_dir=backup_dir,
        execute=False,
    )

    assert preview.mode == "dry-run"
    assert preview.counts == {"threads": 1, "runs": 1, "run_events": 1, "feedback": 1, "checkpoints": 1}
    assert preview.backup_written is False
    assert not backup_dir.exists()
    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0007_project_shared_assets"
            assert await connection.scalar(text("SELECT metadata -> 'deerflow_private_scope' FROM checkpoints")) is None
            before_payloads = (
                await connection.scalar(text("SELECT checkpoint FROM checkpoints")),
                await connection.scalar(text("SELECT blob FROM checkpoint_blobs")),
                await connection.scalar(text("SELECT blob FROM checkpoint_writes")),
            )
    finally:
        await engine.dispose()

    result = await run_private_work_migration(
        postgres_database_url,
        owner_map={owner: project},
        repo_root=tmp_path / "repo",
        data_root=tmp_path / "data",
        backup_dir=backup_dir,
        execute=True,
    )

    assert result.mode == "execute"
    assert result.cutover_complete is True
    assert result.empty_install is False
    assert result.backup_written is False
    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0011_private_artifact_tombstone"
            assert await connection.scalar(text("SELECT project_id FROM threads_meta WHERE thread_id='thread-1'")) == project
            assert await connection.scalar(text("SELECT owner_user_id FROM runs WHERE run_id='run-1'")) == owner
            assert await connection.scalar(text("SELECT content FROM run_events WHERE run_id='run-1'")) == "private event"
            assert await connection.scalar(text("SELECT comment FROM feedback WHERE run_id='run-1'")) == "private feedback"
            assert await connection.scalar(text("SELECT kwargs_json #>> '{input,messages,0,content}' FROM runs WHERE run_id='run-1'")) == "secret prompt"
            assert await connection.scalar(text("SELECT metadata #>> '{deerflow_private_scope,project_id}' FROM checkpoints")) == str(project)
            after_payloads = (
                await connection.scalar(text("SELECT checkpoint FROM checkpoints")),
                await connection.scalar(text("SELECT blob FROM checkpoint_blobs")),
                await connection.scalar(text("SELECT blob FROM checkpoint_writes")),
            )
            assert after_payloads == before_payloads
            assert await connection.scalar(text("SELECT stage FROM private_work_cutover_state WHERE id=1")) == "cutover_complete"
            assert await connection.scalar(text("SELECT count(DISTINCT domain) FROM private_work_migration_ledger")) == 12
    finally:
        await engine.dispose()

    (tmp_path / "data").mkdir()
    (tmp_path / "data/memory.json").write_text("{}", encoding="utf-8")
    again = await run_private_work_migration(
        postgres_database_url,
        owner_map={owner: project},
        repo_root=tmp_path / "repo",
        data_root=tmp_path / "data",
        backup_dir=backup_dir,
        execute=True,
    )
    assert again.noop is True


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_empty_install_path_reaches_cutover_without_creating_private_rows(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    await _upgrade(postgres_database_url, "0007_project_shared_assets")

    result = await run_private_work_migration(
        postgres_database_url,
        owner_map={},
        repo_root=tmp_path / "repo",
        data_root=tmp_path / "data",
        backup_dir=tmp_path / "backup",
        execute=True,
    )

    assert result.empty_install is True
    assert result.cutover_complete is True
    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM threads_meta")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM runs")) == 0
            assert await connection.scalar(text("SELECT stage FROM private_work_cutover_state WHERE id=1")) == "cutover_complete"
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_0007_dry_run_rejects_legacy_channel_rows(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    await _upgrade(postgres_database_url, "0007_project_shared_assets")
    owner, project = await _seed_legacy_private_work(postgres_database_url)
    engine = create_async_engine(postgres_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO channel_connections
                    (id,owner_user_id,provider,status,external_account_id,
                     external_account_name,workspace_id,workspace_name,bot_user_id,
                     scopes_json,capabilities_json,metadata_json,created_at,updated_at,
                     last_seen_at,last_error_at)
                    VALUES (:id,:owner,'slack','connected','legacy-account',NULL,
                            'legacy-workspace',NULL,NULL,'[]'::jsonb,'{}'::jsonb,
                            '{}'::jsonb,now(),now(),NULL,NULL)"""
                ),
                {"id": str(uuid.uuid4()), "owner": owner},
            )
    finally:
        await engine.dispose()

    with pytest.raises(
        PrivateWorkMigrationError,
        match="unsupported legacy source",
    ):
        await run_private_work_migration(
            postgres_database_url,
            owner_map={owner: project},
            repo_root=tmp_path / "repo",
            data_root=tmp_path / "data",
            backup_dir=tmp_path / "backup",
            execute=False,
        )
