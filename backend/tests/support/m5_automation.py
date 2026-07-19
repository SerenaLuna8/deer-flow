from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
from alembic import command
from fastapi import FastAPI, Request
from postgres_utils import RedactedURL, temporary_postgres_database
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.audit.service import AuditService, _bind_gateway_audit_process
from app.audit.sinks import OperationalAuditSink
from app.automations.occurrences import AutomationOccurrenceService
from app.automations.readiness import AutomationReadinessService
from app.automations.service import ProjectAutomationService
from app.gateway.deps import (
    get_current_user_from_request,
    project_session,
)
from app.gateway.routers import project_automations
from app.private_work.context import PrivateWorkContext
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.context import ProjectContext, resolve_project_context
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.persistence.bootstrap import _get_alembic_config, bootstrap_schema
from deerflow.persistence.scheduled_task_runs import (
    ScheduledTaskRunCreate,
    ScheduledTaskRunRecord,
    ScheduledTaskRunRepository,
)
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskCreate,
    ScheduledTaskRecord,
    ScheduledTaskRepository,
)
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

M5_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
M5_FINAL_REVISION = "0013_project_automation_finalize"


def canonical_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class M5Database:
    url: str
    engine: AsyncEngine
    factory: async_sessionmaker
    database_name: str


@asynccontextmanager
async def isolated_m5_database(admin_url: str) -> AsyncIterator[M5Database]:
    """Create one final-schema database from the explicit test admin URL."""

    async with temporary_postgres_database(admin_url) as database_url:
        database_name = make_url(database_url).database or ""
        if not database_name.startswith("deerflow_test_"):
            raise RuntimeError("M5 tests require a disposable deerflow_test database")
        engine = create_async_engine(database_url)
        try:
            await bootstrap_schema(engine)
            yield M5Database(
                url=database_url,
                engine=engine,
                factory=async_sessionmaker(engine, expire_on_commit=False),
                database_name=database_name,
            )
        finally:
            await engine.dispose()


@dataclass(frozen=True, slots=True)
class M5LegacyDatabaseSnapshot:
    revision: str
    content_fingerprint: str
    schema_fingerprint: str
    control_relations: dict[str, bool]


@dataclass(frozen=True, slots=True)
class M5LegacyMigrationDatabase:
    """A real 0011 Automation source inside one disposable PostgreSQL DB."""

    url: str
    engine: AsyncEngine
    seed: M4ThreadSeed
    owner_map: dict[str, object]
    backup_dir: Path
    expected_counts: dict[str, int]
    reuse_thread_id: str
    history_thread_id: str
    history_run_id: str

    async def current_revision(self) -> str:
        async with self.engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert isinstance(revision, str)
        return revision

    async def upgrade(self, revision: str) -> None:
        await asyncio.to_thread(
            command.upgrade,
            _get_alembic_config(self.engine),
            M5_FINAL_REVISION if revision == "head" else revision,
        )

    async def control_relations(self) -> dict[str, bool]:
        """Preserve the distinction between absent and present-empty controls."""

        names = (
            "automation_migration_runs",
            "automation_migration_ledger",
            "automation_cutover_state",
        )
        async with self.engine.connect() as connection:
            return {
                name: (
                    await connection.scalar(
                        text("SELECT to_regclass(:name) IS NOT NULL"),
                        {"name": name},
                    )
                )
                is True
                for name in names
            }

    async def legacy_content_fingerprint(self) -> str:
        """Hash all 0011 Automation source rows and referenced M4 authority."""

        async with self.engine.connect() as connection:
            source: dict[str, object] = {}
            for table in ("scheduled_tasks", "scheduled_task_runs"):
                columns = tuple(
                    (
                        await connection.execute(
                            text(
                                """SELECT column_name
                                FROM information_schema.columns
                                WHERE table_schema=current_schema()
                                  AND table_name=:table
                                ORDER BY ordinal_position"""
                            ),
                            {"table": table},
                        )
                    ).scalars()
                )
                projection = ",".join(f'"{column}"' for column in columns)
                rows = (
                    (
                        await connection.execute(
                            text(
                                f'SELECT {projection} FROM "{table}" ORDER BY id'  # noqa: S608 - fixed test table allowlist
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                source[table] = {
                    "columns": columns,
                    "rows": [dict(row) for row in rows],
                }

            thread_columns = tuple(
                (
                    await connection.execute(
                        text(
                            """SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema=current_schema()
                              AND table_name='threads_meta'
                            ORDER BY ordinal_position"""
                        )
                    )
                ).scalars()
            )
            thread_projection = ",".join(f'"{column}"' for column in thread_columns)
            thread_rows = (
                (
                    await connection.execute(
                        text(
                            f"""SELECT {thread_projection} FROM threads_meta
                            WHERE thread_id IN (:reuse_thread,:history_thread)
                            ORDER BY thread_id"""  # noqa: S608 - fixed metadata projection
                        ),
                        {
                            "reuse_thread": self.reuse_thread_id,
                            "history_thread": self.history_thread_id,
                        },
                    )
                )
                .mappings()
                .all()
            )
            run_columns = tuple(
                (
                    await connection.execute(
                        text(
                            """SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema=current_schema()
                              AND table_name='runs'
                            ORDER BY ordinal_position"""
                        )
                    )
                ).scalars()
            )
            run_projection = ",".join(f'"{column}"' for column in run_columns)
            run_rows = (
                (
                    await connection.execute(
                        text(
                            f"""SELECT {run_projection} FROM runs
                            WHERE run_id=:run_id ORDER BY run_id"""  # noqa: S608 - fixed metadata projection
                        ),
                        {"run_id": self.history_run_id},
                    )
                )
                .mappings()
                .all()
            )
            source["referenced_m4_authority"] = {
                "threads_meta": {
                    "columns": thread_columns,
                    "rows": [dict(row) for row in thread_rows],
                },
                "runs": {
                    "columns": run_columns,
                    "rows": [dict(row) for row in run_rows],
                },
            }
        return canonical_digest(source)

    async def schema_fingerprint(self) -> str:
        """Hash finalization-visible schema objects, excluding table data."""

        async with self.engine.connect() as connection:
            columns = (
                await connection.execute(
                    text(
                        """SELECT table_name,column_name,ordinal_position,data_type,
                                  udt_name,is_nullable,column_default
                        FROM information_schema.columns
                        WHERE table_schema=current_schema()
                          AND table_name IN
                            ('scheduled_tasks','scheduled_task_runs',
                             'automation_migration_runs',
                             'automation_migration_ledger',
                             'automation_cutover_state')
                        ORDER BY table_name,ordinal_position"""
                    )
                )
            ).all()
            constraints = (
                await connection.execute(
                    text(
                        """SELECT relation.relname,con.conname,
                                  con.contype,
                                  pg_get_constraintdef(con.oid,true)
                        FROM pg_constraint con
                        JOIN pg_class relation
                          ON relation.oid=con.conrelid
                        JOIN pg_namespace namespace
                          ON namespace.oid=relation.relnamespace
                        WHERE namespace.nspname=current_schema()
                          AND relation.relname IN
                            ('scheduled_tasks','scheduled_task_runs',
                             'automation_migration_runs',
                             'automation_migration_ledger',
                             'automation_cutover_state')
                        ORDER BY relation.relname,con.conname"""
                    )
                )
            ).all()
            indexes = (
                await connection.execute(
                    text(
                        """SELECT tablename,indexname,indexdef
                        FROM pg_indexes
                        WHERE schemaname=current_schema()
                          AND tablename IN
                            ('scheduled_tasks','scheduled_task_runs',
                             'automation_migration_runs',
                             'automation_migration_ledger',
                             'automation_cutover_state')
                        ORDER BY tablename,indexname"""
                    )
                )
            ).all()
            triggers = (
                await connection.execute(
                    text(
                        """SELECT event_object_table,trigger_name,
                                  action_timing,event_manipulation,action_statement
                        FROM information_schema.triggers
                        WHERE trigger_schema=current_schema()
                          AND event_object_table IN
                            ('scheduled_tasks','scheduled_task_runs','agents')
                        ORDER BY event_object_table,trigger_name,event_manipulation"""
                    )
                )
            ).all()
        payload = json.dumps(
            {
                "columns": [tuple(row) for row in columns],
                "constraints": [tuple(row) for row in constraints],
                "indexes": [tuple(row) for row in indexes],
                "triggers": [tuple(row) for row in triggers],
            },
            default=str,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def snapshot(self) -> M5LegacyDatabaseSnapshot:
        return M5LegacyDatabaseSnapshot(
            revision=await self.current_revision(),
            content_fingerprint=await self.legacy_content_fingerprint(),
            schema_fingerprint=await self.schema_fingerprint(),
            control_relations=await self.control_relations(),
        )


def _m5_owner_map_item(
    seed: M4ThreadSeed,
    *,
    project_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    agent_scope: str = "project",
) -> dict[str, object]:
    return {
        "project_id": str(project_id or seed.owner_a.project_id),
        "fresh_thread_agent": {
            "asset_id": str(agent_id or seed.project_agent_id),
            "scope": agent_scope,
        },
    }


async def _seed_m5_migration_thread_and_run(
    seed: M4ThreadSeed,
    *,
    thread_id: str,
    run_id: str | None = None,
) -> None:
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO threads_meta
                (thread_id,assistant_id,owner_user_id,display_name,status,metadata_json,
                 project_id,agent_asset_id,agent_scope,checkpoint_delete_status,version,
                 created_at,updated_at)
                VALUES (:thread_id,NULL,:owner,'M5 migration thread','idle','{}'::jsonb,
                        :project,:agent,'project','not_requested',1,now(),now())"""
            ),
            {
                "thread_id": thread_id,
                "owner": str(seed.owner_a.user_id),
                "project": seed.owner_a.project_id,
                "agent": seed.project_agent_id,
            },
        )
        if run_id is not None:
            await connection.execute(
                text(
                    """INSERT INTO runs
                    (run_id,thread_id,assistant_id,owner_user_id,status,model_name,
                     multitask_strategy,metadata_json,kwargs_json,message_count,
                     total_input_tokens,total_output_tokens,total_tokens,llm_call_count,
                     lead_agent_tokens,subagent_tokens,middleware_tokens,
                     token_usage_by_model,project_id,finalization_status,
                     created_at,updated_at)
                    VALUES (:run_id,:thread_id,NULL,:owner,'success','test-model',
                            'reject','{}'::jsonb,'{}'::jsonb,0,0,0,0,0,0,0,0,
                            '{}'::jsonb,:project,'complete',now(),now())"""
                ),
                {
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "owner": str(seed.owner_a.user_id),
                    "project": seed.owner_a.project_id,
                },
            )


async def _upgrade_empty_database_to_m4_final(engine: AsyncEngine) -> None:
    """Build the reusable legacy fixture by forward-only migrations."""

    config = _get_alembic_config(engine)
    await asyncio.to_thread(
        command.upgrade,
        config,
        "0008_project_private_work_expand",
    )
    private_finalize = importlib.import_module("deerflow.persistence.migrations.versions.0009_project_private_work_finalize")
    migration_run_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO private_work_migration_runs
                   (id,mode,status,source_fingerprint,owner_map_digest,
                    legacy_source_probe_complete,checkpoint_marker_probe_complete,
                    cross_scope_probe_complete,completed_at)
                   VALUES (:id,'execute','completed',:digest,:digest,true,true,true,now())"""
            ),
            {"id": migration_run_id, "digest": "c" * 64},
        )
        await connection.execute(
            text(
                """INSERT INTO private_work_migration_ledger
                   (migration_run_id,domain,source_key_hash,source_fingerprint,
                    target_digest,status,row_count,byte_count)
                   VALUES (:run_id,:domain,:digest,:digest,:digest,'complete',0,0)"""
            ),
            [
                {
                    "run_id": migration_run_id,
                    "domain": domain,
                    "digest": f"{index:064x}",
                }
                for index, domain in enumerate(
                    sorted(private_finalize.FINALIZE_LEDGER_DOMAINS),
                    start=1,
                )
            ],
        )
    await asyncio.to_thread(
        command.upgrade,
        config,
        "0011_private_artifact_tombstone",
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO private_work_cutover_state
                   (id,stage,migration_run_id,empty_domain_probe_complete,
                    checkpoint_marker_probe_complete,cutover_at,updated_at)
                   VALUES (1,'cutover_complete',:id,false,true,now(),now())"""
            ),
            {"id": migration_run_id},
        )


@asynccontextmanager
async def isolated_m5_legacy_migration_database(
    admin_url: str,
    backup_dir: Path,
) -> AsyncIterator[M5LegacyMigrationDatabase]:
    """Create a disposable, non-empty 0011 source for the M5 migration gate."""

    async with temporary_postgres_database(admin_url) as database_url:
        database_name = make_url(database_url).database or ""
        if not database_name.startswith("deerflow_test_"):
            raise RuntimeError("M5 migration tests require a disposable database")

        bootstrap_engine = create_async_engine(database_url)
        try:
            await _upgrade_empty_database_to_m4_final(bootstrap_engine)
        finally:
            await bootstrap_engine.dispose()

        seed = await seed_m4_thread_database(database_url)
        reuse_thread_id = f"m5-reuse-{uuid.uuid4().hex}"
        history_thread_id = f"m5-history-{uuid.uuid4().hex}"
        history_run_id = f"m5-run-{uuid.uuid4().hex}"
        try:
            await _seed_m5_migration_thread_and_run(
                seed,
                thread_id=reuse_thread_id,
            )
            await _seed_m5_migration_thread_and_run(
                seed,
                thread_id=history_thread_id,
                run_id=history_run_id,
            )
            async with seed.engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO scheduled_tasks
                        (id,user_id,thread_id,context_mode,assistant_id,title,prompt,
                         schedule_type,schedule_spec,timezone,status,overlap_policy,
                         next_run_at,last_run_at,last_run_id,last_thread_id,last_error,
                         lease_owner,lease_expires_at,run_count,created_at,updated_at)
                        VALUES
                        ('m5-legacy-fresh',:owner,NULL,'fresh_thread_per_run',
                         'legacy-agent','Legacy fresh','Private migration prompt',
                         'cron','{"cron":"0 9 * * *"}'::json,'UTC','enabled','skip',
                         now(),now(),:run_id,:history_thread,NULL,NULL,NULL,2,now(),now()),
                        ('m5-legacy-reuse',:owner,:reuse_thread,'reuse_thread',
                         'ignored-agent','Legacy reuse','Reuse private prompt',
                         'once','{}'::json,'UTC','paused','skip',NULL,NULL,NULL,NULL,
                         NULL,NULL,NULL,0,now(),now())"""
                    ),
                    {
                        "owner": str(seed.owner_a.user_id),
                        "run_id": history_run_id,
                        "history_thread": history_thread_id,
                        "reuse_thread": reuse_thread_id,
                    },
                )
                await connection.execute(
                    text(
                        """INSERT INTO scheduled_task_runs
                        (id,task_id,thread_id,run_id,scheduled_for,trigger,status,error,
                         started_at,finished_at,created_at)
                        VALUES
                        ('m5-legacy-success','m5-legacy-fresh',:history_thread,:run_id,
                         now(),'scheduled','success',NULL,now(),now(),now()),
                        ('m5-legacy-skipped','m5-legacy-fresh',:missing_thread,NULL,
                         now(),'scheduled','skipped','private legacy detail',
                         now(),now(),now())"""
                    ),
                    {
                        "history_thread": history_thread_id,
                        "run_id": history_run_id,
                        "missing_thread": f"m5-missing-{uuid.uuid4().hex}",
                    },
                )

            backup_dir.mkdir(parents=True, exist_ok=True)
            (backup_dir / "operator-restore-proof.txt").write_text(
                "verified external PostgreSQL backup and restore rehearsal",
                encoding="utf-8",
            )
            yield M5LegacyMigrationDatabase(
                url=RedactedURL(database_url),
                engine=seed.engine,
                seed=seed,
                owner_map={str(seed.owner_a.user_id): _m5_owner_map_item(seed)},
                backup_dir=backup_dir,
                expected_counts={
                    "scheduled_tasks": 2,
                    "scheduled_task_runs": 2,
                },
                reuse_thread_id=reuse_thread_id,
                history_thread_id=history_thread_id,
                history_run_id=history_run_id,
            )
        finally:
            await seed.engine.dispose()


@dataclass(frozen=True, slots=True)
class M5Actor:
    name: str
    user_id: uuid.UUID
    project_id: uuid.UUID | None
    project_context: ProjectContext | None
    private_context: PrivateWorkContext | None


@dataclass(frozen=True, slots=True)
class M5Seed:
    database: M5Database
    m4: M4ThreadSeed
    actors: dict[str, M5Actor]
    tasks: dict[str, ScheduledTaskRecord]
    threads: dict[str, str]
    history: ScheduledTaskRunRecord | None

    @property
    def factory(self):
        return self.database.factory

    def actor(self, name: str) -> M5Actor:
        return self.actors[name]

    def context(self, name: str) -> PrivateWorkContext:
        context = self.actor(name).private_context
        if context is None:
            raise LookupError(f"{name} has no project-private authority")
        return context

    def project_context(self, name: str) -> ProjectContext:
        context = self.actor(name).project_context
        if context is None:
            raise LookupError(f"{name} has no project authority")
        return context

    def project_for(self, name: str) -> uuid.UUID:
        project_id = self.actor(name).project_id
        if project_id is None:
            raise LookupError(f"{name} has no project")
        return project_id

    def task_for(self, name: str) -> ScheduledTaskRecord:
        return self.tasks[name]

    def history_record(self) -> ScheduledTaskRunRecord:
        if self.history is None:
            raise LookupError("M5 history has not been seeded")
        return self.history

    async def create_task(
        self,
        actor_name: str,
        *,
        task_id: str | None = None,
        next_run_at: datetime | None = None,
        context_mode: str = "fresh_thread_per_run",
        thread_id: str | None = None,
    ) -> ScheduledTaskRecord:
        context = self.context(actor_name)
        agent_asset_id = self.m4.project_b_agent_id if context.project_id == self.m4.project_b_owner_a.project_id else self.m4.project_agent_id
        async with self.factory() as session, session.begin():
            return await ScheduledTaskRepository(session).create(
                context.resource_scope,
                ScheduledTaskCreate(
                    task_id=task_id or f"m5-task-{uuid.uuid4().hex[:20]}",
                    thread_id=thread_id,
                    context_mode=context_mode,
                    agent_asset_id=agent_asset_id,
                    agent_scope="project",
                    title=f"{actor_name} private automation",
                    prompt="Process project-private work.",
                    schedule_type="cron",
                    schedule_spec={"cron": "0 * * * *"},
                    timezone="UTC",
                    next_run_at=next_run_at,
                ),
            )

    async def create_occurrence(
        self,
        actor_name: str,
        task: ScheduledTaskRecord,
        *,
        status: str = "queued",
        trigger: str = "scheduled",
        occurrence_id: str | None = None,
        scheduled_for: datetime = M5_NOW,
    ) -> ScheduledTaskRunRecord:
        occurrence_id = occurrence_id or f"m5-occ-{uuid.uuid4().hex[:20]}"
        async with self.factory() as session, session.begin():
            return await ScheduledTaskRunRepository(session).create(
                self.context(actor_name).resource_scope,
                ScheduledTaskRunCreate(
                    occurrence_id=occurrence_id,
                    task_id=task.id,
                    task_version=task.version,
                    occurrence_key=hashlib.sha256(occurrence_id.encode("ascii")).hexdigest(),
                    manual_idempotency_hash=None,
                    scheduled_for=scheduled_for,
                    trigger=trigger,
                    status=status,
                    finished_at=(scheduled_for if status not in {"queued", "launching", "running"} else None),
                    created_at=scheduled_for,
                ),
            )


async def _resolved_actor(
    database: M5Database,
    *,
    name: str,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
) -> M5Actor:
    async with database.factory() as session:
        project = await resolve_project_context(
            session,
            user_id,
            project_id,
            f"m5-{name}",
        )
    return M5Actor(
        name=name,
        user_id=user_id,
        project_id=project_id,
        project_context=project,
        private_context=PrivateWorkContext.from_project(project),
    )


async def seed_m5_database(database: M5Database) -> M5Seed:
    m4 = await seed_m4_thread_database(database.url)
    project_b_owner_id = uuid.uuid4()
    project_b_owner_membership_id = uuid.uuid4()
    system_admin_id = uuid.uuid4()
    try:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(
                """INSERT INTO users
                (id,email,system_role,created_at,needs_setup,token_version)
                VALUES ($1,$2,'user',now(),false,0),
                       ($3,$4,'system_admin',now(),false,0)""",
                (
                    str(project_b_owner_id),
                    f"{project_b_owner_id}@example.invalid",
                    str(system_admin_id),
                    f"{system_admin_id}@example.invalid",
                ),
            )
            await connection.exec_driver_sql(
                """INSERT INTO project_memberships
                (id,project_id,user_id,role,status,version)
                VALUES ($1,$2,$3,'admin','active',1)""",
                (
                    project_b_owner_membership_id,
                    m4.project_b_owner_a.project_id,
                    str(project_b_owner_id),
                ),
            )

        actors = {
            "owner_a": M5Actor(
                "owner_a",
                m4.owner_a.user_id,
                m4.owner_a.project_id,
                ProjectContext(
                    user_id=m4.owner_a.user_id,
                    project_id=m4.owner_a.project_id,
                    membership_id=m4.owner_a.membership_id,
                    role=m4.owner_a.role,
                    capabilities=m4.owner_a.capabilities,
                    membership_version=m4.owner_a.membership_version,
                    request_id="m5-owner-a",
                ),
                m4.owner_a,
            ),
            "owner_b": M5Actor(
                "owner_b",
                m4.owner_b.user_id,
                m4.owner_b.project_id,
                ProjectContext(
                    user_id=m4.owner_b.user_id,
                    project_id=m4.owner_b.project_id,
                    membership_id=m4.owner_b.membership_id,
                    role=m4.owner_b.role,
                    capabilities=m4.owner_b.capabilities,
                    membership_version=m4.owner_b.membership_version,
                    request_id="m5-owner-b",
                ),
                m4.owner_b,
            ),
            "viewer": M5Actor(
                "viewer",
                m4.viewer.user_id,
                m4.viewer.project_id,
                ProjectContext(
                    user_id=m4.viewer.user_id,
                    project_id=m4.viewer.project_id,
                    membership_id=m4.viewer.membership_id,
                    role=m4.viewer.role,
                    capabilities=m4.viewer.capabilities,
                    membership_version=m4.viewer.membership_version,
                    request_id="m5-viewer",
                ),
                m4.viewer,
            ),
            "owner_a_project_b": await _resolved_actor(
                database,
                name="owner-a-project-b",
                user_id=m4.project_b_owner_a.user_id,
                project_id=m4.project_b_owner_a.project_id,
            ),
            "project_b_owner": await _resolved_actor(
                database,
                name="project-b-owner",
                user_id=project_b_owner_id,
                project_id=m4.project_b_owner_a.project_id,
            ),
            "system_admin": M5Actor(
                "system_admin",
                system_admin_id,
                None,
                None,
                None,
            ),
        }

        owner_thread_id = str(uuid.uuid4())
        owner_b_thread_id = str(uuid.uuid4())
        viewer_thread_id = str(uuid.uuid4())
        owner_a_project_b_thread_id = str(uuid.uuid4())
        async with database.factory() as session, session.begin():
            threads = PrivateThreadRepository(session)
            await threads.create(
                scope=actors["owner_a"].private_context.resource_scope,  # type: ignore[union-attr]
                thread_id=owner_thread_id,
                agent=ThreadAgentRef(m4.project_agent_id, "project"),
            )
            await threads.create(
                scope=actors["owner_b"].private_context.resource_scope,  # type: ignore[union-attr]
                thread_id=owner_b_thread_id,
                agent=ThreadAgentRef(m4.project_agent_id, "project"),
            )
            await threads.create(
                scope=actors["viewer"].private_context.resource_scope,  # type: ignore[union-attr]
                thread_id=viewer_thread_id,
                agent=ThreadAgentRef(m4.project_agent_id, "project"),
            )
            await threads.create(
                scope=actors["owner_a_project_b"].private_context.resource_scope,  # type: ignore[union-attr]
                thread_id=owner_a_project_b_thread_id,
                agent=ThreadAgentRef(m4.project_b_agent_id, "project"),
            )

        provisional = M5Seed(
            database=database,
            m4=m4,
            actors=actors,
            tasks={},
            threads={
                "owner_a": owner_thread_id,
                "owner_b": owner_b_thread_id,
                "viewer": viewer_thread_id,
                "owner_a_project_b": owner_a_project_b_thread_id,
            },
            history=None,
        )
        tasks = {
            "owner_a": await provisional.create_task(
                "owner_a",
                task_id="m5-owner-a-primary",
                next_run_at=M5_NOW + timedelta(days=1),
                context_mode="reuse_thread",
                thread_id=owner_thread_id,
            ),
            "owner_a_secondary": await provisional.create_task(
                "owner_a",
                task_id="m5-owner-a-secondary",
                next_run_at=M5_NOW + timedelta(days=1),
            ),
            "owner_b": await provisional.create_task(
                "owner_b",
                task_id="m5-owner-b-primary",
                next_run_at=M5_NOW + timedelta(days=1),
                context_mode="reuse_thread",
                thread_id=owner_b_thread_id,
            ),
            "viewer": await provisional.create_task(
                "viewer",
                task_id="m5-viewer-primary",
                next_run_at=M5_NOW + timedelta(days=1),
                context_mode="reuse_thread",
                thread_id=viewer_thread_id,
            ),
            "owner_a_project_b": await provisional.create_task(
                "owner_a_project_b",
                task_id="m5-owner-a-project-b-primary",
                next_run_at=M5_NOW + timedelta(days=1),
                context_mode="reuse_thread",
                thread_id=owner_a_project_b_thread_id,
            ),
            "project_b_owner": await provisional.create_task(
                "project_b_owner",
                task_id="m5-project-b-primary",
                next_run_at=M5_NOW + timedelta(days=1),
            ),
        }
        history = await provisional.create_occurrence(
            "owner_a",
            tasks["owner_a"],
            status="success",
            occurrence_id="m5-owner-a-history",
            scheduled_for=M5_NOW - timedelta(hours=1),
        )
        return M5Seed(
            database=database,
            m4=m4,
            actors=actors,
            tasks=tasks,
            threads=provisional.threads,
            history=history,
        )
    except BaseException:
        await m4.engine.dispose()
        raise


class _NeverDispatch:
    async def dispatch(self, *_args, **_kwargs):
        raise AssertionError("scope/Viewer tests must not dispatch an Agent run")


@dataclass(slots=True)
class M5App:
    seed: M5Seed
    app: FastAPI
    client: httpx.AsyncClient

    async def request(
        self,
        method: str,
        path: str,
        *,
        actor: str,
        **kwargs,
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["x-test-user"] = str(self.seed.actor(actor).user_id)
        return await self.client.request(method, path, headers=headers, **kwargs)

    async def aclose(self) -> None:
        await self.client.aclose()


async def build_m5_app(seed: M5Seed) -> M5App:
    app = FastAPI()
    app.include_router(project_automations.readiness_router)
    app.include_router(project_automations.router)

    async def request_session():
        async with seed.factory() as session:
            yield session

    async def request_user(request: Request):
        return SimpleNamespace(id=request.headers["x-test-user"])

    app.dependency_overrides[project_session] = request_session
    app.dependency_overrides[get_current_user_from_request] = request_user
    audit_service = AuditService(seed.factory, AuditHmacKeyring.from_environment())
    app.state.operational_audit_sink = OperationalAuditSink(
        audit_service,
        process_context=_bind_gateway_audit_process(audit_service),
    )
    app.state.automation_service = ProjectAutomationService(seed.factory, clock=lambda: M5_NOW)
    app.state.automation_occurrence_service = AutomationOccurrenceService(
        seed.factory,
        max_concurrent_runs=3,
    )
    app.state.automation_dispatcher = _NeverDispatch()
    app.state.automation_readiness_service = AutomationReadinessService(
        scheduler_status_provider=lambda: "stopped",
    )
    app.state.automation_scheduler_enabled = True
    app.state.automation_lease_seconds = 60
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://m5.test",
    )
    return M5App(seed=seed, app=app, client=client)


__all__ = [
    "M5Actor",
    "M5App",
    "M5Database",
    "M5LegacyDatabaseSnapshot",
    "M5LegacyMigrationDatabase",
    "M5Seed",
    "M5_NOW",
    "build_m5_app",
    "isolated_m5_database",
    "isolated_m5_legacy_migration_database",
    "seed_m5_database",
]
