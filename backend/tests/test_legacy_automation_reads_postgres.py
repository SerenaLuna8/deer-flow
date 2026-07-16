from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from alembic import command
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.m4_private_threads import seed_m4_thread_database

from app.gateway.auth_disabled import AUTH_SOURCE_SESSION
from app.gateway.routers import scheduled_tasks
from deerflow.persistence.bootstrap import _get_alembic_config

OWNER_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
OTHER_ID = uuid.UUID("20000000-0000-0000-0000-000000000002")


class _ExpandGuard:
    async def require_legacy_open(self) -> None:
        return None

    async def require_legacy_mutation_open(self) -> None:
        from app.automations.errors import AutomationMigrationRequired

        raise AutomationMigrationRequired("expand-read-test")

    async def require_project_open(self) -> None:
        return None


@dataclass(slots=True)
class _ExpandedLegacySeed:
    engine: object
    factory: object


@pytest_asyncio.fixture()
async def expanded_legacy_seed(
    migrated_postgres_database_url: str,
) -> _ExpandedLegacySeed:
    engine = create_async_engine(migrated_postgres_database_url)
    config = _get_alembic_config(engine)
    await engine.dispose()
    await asyncio.to_thread(
        command.downgrade,
        config,
        "0011_private_artifact_tombstone",
    )

    engine = create_async_engine(migrated_postgres_database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO scheduled_tasks
                (id,user_id,thread_id,context_mode,assistant_id,title,prompt,
                 schedule_type,schedule_spec,timezone,status,overlap_policy,
                 next_run_at,last_run_at,last_run_id,last_thread_id,last_error,
                 lease_owner,lease_expires_at,run_count,created_at,updated_at)
                VALUES
                ('legacy-owner-task',:owner,'legacy-owner-thread','reuse_thread',
                 'lead_agent','Owner daily','Owner private prompt','cron',
                 '{"cron":"0 9 * * *"}'::json,'UTC','enabled','skip',
                 '2026-07-17T09:00:00+00:00','2026-07-16T09:00:00+00:00',
                 'owner-run','legacy-owner-thread',NULL,NULL,NULL,1,
                 '2026-07-16T08:00:00+00:00','2026-07-16T09:01:00+00:00'),
                ('legacy-other-task',:other,'legacy-other-thread','reuse_thread',
                 'lead_agent','Other daily','Other private prompt','cron',
                 '{"cron":"0 10 * * *"}'::json,'UTC','paused','skip',
                 '2026-07-17T10:00:00+00:00',NULL,NULL,NULL,NULL,NULL,NULL,0,
                 '2026-07-16T07:00:00+00:00','2026-07-16T07:00:00+00:00')"""
            ),
            {"owner": str(OWNER_ID), "other": str(OTHER_ID)},
        )
        await connection.execute(
            text(
                """INSERT INTO scheduled_task_runs
                (id,task_id,thread_id,run_id,scheduled_for,trigger,status,error,
                 started_at,finished_at,created_at)
                VALUES
                ('legacy-owner-occurrence','legacy-owner-task','legacy-owner-thread',
                 'owner-run','2026-07-16T09:00:00+00:00','scheduled','success',NULL,
                 '2026-07-16T09:00:01+00:00','2026-07-16T09:00:05+00:00',
                 '2026-07-16T09:00:00+00:00'),
                ('legacy-other-occurrence','legacy-other-task','legacy-other-thread',
                 NULL,'2026-07-16T10:00:00+00:00','scheduled','failed','private',
                 '2026-07-16T10:00:01+00:00','2026-07-16T10:00:02+00:00',
                 '2026-07-16T10:00:00+00:00')"""
            )
        )
    await engine.dispose()
    await asyncio.to_thread(
        command.upgrade,
        config,
        "0012_project_automation_expand",
    )

    engine = create_async_engine(migrated_postgres_database_url)
    seed = _ExpandedLegacySeed(
        engine=engine,
        factory=async_sessionmaker(engine, expire_on_commit=False),
    )
    try:
        yield seed
    finally:
        await engine.dispose()


def _legacy_app(seed: _ExpandedLegacySeed, identity: dict[str, uuid.UUID]) -> FastAPI:
    from app.automations.legacy_reads import LegacyAutomationReadAdapter

    app = FastAPI()
    app.include_router(scheduled_tasks.router)
    adapter = LegacyAutomationReadAdapter(seed.factory)
    app.state.automation_cutover_guard = _ExpandGuard()
    app.state.scheduled_task_repo = adapter
    app.state.scheduled_task_run_repo = adapter
    app.state.scheduled_task_service = SimpleNamespace()
    app.state.thread_store = SimpleNamespace(check_access=AsyncMock(return_value=True))

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.user = SimpleNamespace(id=identity["user_id"])
        request.state.auth_source = AUTH_SOURCE_SESSION
        return await call_next(request)

    return app


OWNER_TASK_DTO = {
    "id": "legacy-owner-task",
    "user_id": str(OWNER_ID),
    "thread_id": "legacy-owner-thread",
    "context_mode": "reuse_thread",
    "assistant_id": "lead_agent",
    "title": "Owner daily",
    "prompt": "Owner private prompt",
    "schedule_type": "cron",
    "schedule_spec": {"cron": "0 9 * * *"},
    "timezone": "UTC",
    "status": "enabled",
    "overlap_policy": "skip",
    "next_run_at": "2026-07-17T09:00:00+00:00",
    "last_run_at": "2026-07-16T09:00:00+00:00",
    "last_run_id": "owner-run",
    "last_thread_id": "legacy-owner-thread",
    "last_error": None,
    "lease_owner": None,
    "lease_expires_at": None,
    "run_count": 1,
    "created_at": "2026-07-16T08:00:00+00:00",
    "updated_at": "2026-07-16T09:01:00+00:00",
}

OWNER_RUN_DTO = {
    "id": "legacy-owner-occurrence",
    "task_id": "legacy-owner-task",
    "thread_id": "legacy-owner-thread",
    "run_id": "owner-run",
    "scheduled_for": "2026-07-16T09:00:00+00:00",
    "trigger": "scheduled",
    "status": "success",
    "error": None,
    "started_at": "2026-07-16T09:00:01+00:00",
    "finished_at": "2026-07-16T09:00:05+00:00",
    "created_at": "2026-07-16T09:00:00+00:00",
}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_expand_legacy_reads_return_exact_dtos_and_hide_other_owners(
    expanded_legacy_seed: _ExpandedLegacySeed,
) -> None:
    identity = {"user_id": OWNER_ID}
    app = _legacy_app(expanded_legacy_seed, identity)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        listed = await client.get("/api/scheduled-tasks")
        fetched = await client.get("/api/scheduled-tasks/legacy-owner-task")
        history = await client.get("/api/scheduled-tasks/legacy-owner-task/runs")
        thread_alias = await client.get("/api/threads/legacy-owner-thread/scheduled-tasks")

        assert listed.status_code == 200, listed.text
        assert listed.json() == [OWNER_TASK_DTO]
        assert fetched.status_code == 200, fetched.text
        assert fetched.json() == OWNER_TASK_DTO
        assert history.status_code == 200, history.text
        assert history.json() == [OWNER_RUN_DTO]
        assert thread_alias.status_code == 200, thread_alias.text
        assert thread_alias.json() == [OWNER_TASK_DTO]

        identity["user_id"] = OTHER_ID
        owner_get = await client.get("/api/scheduled-tasks/legacy-owner-task")
        owner_history = await client.get("/api/scheduled-tasks/legacy-owner-task/runs")
        owner_thread = await client.get("/api/threads/legacy-owner-thread/scheduled-tasks")
        assert owner_get.status_code == 404
        assert owner_history.status_code == 404
        assert owner_thread.status_code == 200
        assert owner_thread.json() == []
        assert (
            await app.state.scheduled_task_run_repo.list_by_task(
                "legacy-owner-task",
                user_id=str(OTHER_ID),
            )
            == []
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_expand_legacy_mutations_are_409_and_write_nothing(
    expanded_legacy_seed: _ExpandedLegacySeed,
) -> None:
    identity = {"user_id": OWNER_ID}
    app = _legacy_app(expanded_legacy_seed, identity)

    async def snapshot() -> tuple[object, ...]:
        async with expanded_legacy_seed.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """SELECT title,status,run_count,updated_at
                        FROM scheduled_tasks WHERE id='legacy-owner-task'"""
                    )
                )
            ).one()
            run_count = await connection.scalar(text("SELECT count(*) FROM scheduled_task_runs"))
        return (*row, run_count)

    before = await snapshot()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        responses = [
            await client.post("/api/scheduled-tasks", json={}),
            await client.patch("/api/scheduled-tasks/legacy-owner-task", json={}),
            await client.post("/api/scheduled-tasks/legacy-owner-task/pause"),
            await client.post("/api/scheduled-tasks/legacy-owner-task/resume"),
            await client.post("/api/scheduled-tasks/legacy-owner-task/trigger"),
            await client.delete("/api/scheduled-tasks/legacy-owner-task"),
        ]
    after = await snapshot()

    assert before == after
    for response in responses:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == ("AUTOMATION_MIGRATION_REQUIRED")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_final_projection_fails_closed_when_one_owner_spans_projects(
    migrated_postgres_database_url: str,
) -> None:
    from app.automations.legacy_reads import LegacyAutomationReadAdapter

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        async with seed.engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO scheduled_tasks
                    (id,project_id,owner_user_id,thread_id,context_mode,
                     agent_asset_id,agent_scope,title,prompt,schedule_type,
                     schedule_spec,timezone,status,overlap_policy,next_run_at,
                     last_run_at,last_outcome,last_error_code,run_count,version,
                     frozen_at,deleted_at,created_at,updated_at)
                    VALUES
                    ('final-project-a',:project_a,:owner_a,NULL,
                     'fresh_thread_per_run',:system_agent,'system','Project A',
                     'private','cron','{"cron":"0 9 * * *"}'::json,'UTC',
                     'enabled','skip',now(),NULL,NULL,NULL,0,1,NULL,NULL,now(),now()),
                    ('final-project-b',:project_b,:owner_a,NULL,
                     'fresh_thread_per_run',:system_agent,'system','Project B',
                     'private','cron','{"cron":"0 10 * * *"}'::json,'UTC',
                     'enabled','skip',now(),NULL,NULL,NULL,0,1,NULL,NULL,now(),now()),
                    ('final-owner-b',:project_a,:owner_b,NULL,
                     'fresh_thread_per_run',:system_agent,'system','Owner B',
                     'private','cron','{"cron":"0 11 * * *"}'::json,'UTC',
                     'enabled','skip',now(),NULL,NULL,NULL,0,1,NULL,NULL,now(),now())"""
                ),
                {
                    "project_a": seed.owner_a.project_id,
                    "project_b": seed.project_b_owner_a.project_id,
                    "owner_a": str(seed.owner_a.user_id),
                    "owner_b": str(seed.owner_b.user_id),
                    "system_agent": seed.system_agent_id,
                },
            )

        adapter = LegacyAutomationReadAdapter(seed.factory)
        assert await adapter.list_by_user(str(seed.owner_a.user_id)) == []
        assert (
            await adapter.get(
                "final-project-a",
                user_id=str(seed.owner_a.user_id),
            )
            is None
        )
        assert (
            await adapter.list_by_user_and_thread(
                str(seed.owner_a.user_id),
                "any-thread",
            )
            == []
        )
        assert (
            await adapter.list_by_task(
                "final-project-a",
                user_id=str(seed.owner_a.user_id),
            )
            == []
        )

        owner_b = await adapter.list_by_user(str(seed.owner_b.user_id))
        assert [item["id"] for item in owner_b] == ["final-owner-b"]
    finally:
        await seed.engine.dispose()
