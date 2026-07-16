from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request
from postgres_utils import temporary_postgres_database
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.automations.cutover import AutomationCutoverGuard
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
from deerflow.persistence.bootstrap import bootstrap_schema
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
    app.state.automation_cutover_guard = AutomationCutoverGuard(seed.factory)
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
    "M5Seed",
    "M5_NOW",
    "build_m5_app",
    "isolated_m5_database",
    "seed_m5_database",
]
