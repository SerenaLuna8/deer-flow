from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from support.m4_private_threads import seed_m4_thread_database

from app.automations.cutover import AutomationCutoverGuard
from app.automations.dispatcher import AutomationDispatcher
from app.automations.occurrences import AutomationOccurrenceService
from app.automations.readiness import AutomationReadinessService
from app.automations.reconciliation import AutomationReconciler
from app.gateway.app import create_app
from app.gateway.deps import langgraph_runtime
from app.private_work.connection_service import ProjectConnectionService
from app.private_work.cutover import PrivateWorkCutoverGuard
from app.private_work.file_service import PrivateFileService
from app.private_work.file_streaming import PrivateFileStreamer
from app.private_work.memory_service import PrivateMemoryService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.private_work.thread_service import PrivateThreadService
from app.scheduler import ScheduledTaskService
from deerflow.config.scheduler_config import SchedulerConfig
from deerflow.persistence.channel_connections import ChannelConnectionRepository
from deerflow.runtime.events.store.db import DbRunEventStore


@asynccontextmanager
async def _context(value):
    yield value


def test_create_app_mounts_project_private_work_routes() -> None:
    paths = {route.path for route in create_app().routes}

    assert "/api/projects/{project_id}/private-work/threads" in paths
    assert "/api/projects/{project_id}/memory" in paths
    assert "/api/projects/{project_id}/connections" in paths


@pytest.mark.asyncio
async def test_langgraph_runtime_installs_project_private_work_services_from_one_factory(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GATEWAY_WORKERS", "2")
    app = FastAPI()
    config = SimpleNamespace(
        database=SimpleNamespace(),
        run_events=SimpleNamespace(
            backend="memory",
            max_trace_content=321,
        ),
        stream_bridge=None,
        scheduler=SchedulerConfig(min_once_delay_seconds=73),
    )
    session_factory = MagicMock(name="session_factory")

    with (
        patch(
            "deerflow.persistence.engine.init_engine_from_config",
            new=AsyncMock(),
        ),
        patch(
            "deerflow.persistence.engine.get_session_factory",
            return_value=session_factory,
        ),
        patch("deerflow.persistence.engine.close_engine", new=AsyncMock()),
        patch(
            "deerflow.runtime.make_stream_bridge",
            return_value=_context(MagicMock()),
        ),
        patch(
            "deerflow.runtime.checkpointer.async_provider.make_checkpointer",
            return_value=_context(MagicMock()),
        ),
        patch(
            "deerflow.runtime.make_store",
            return_value=_context(MagicMock()),
        ),
        patch(
            "deerflow.persistence.thread_meta.make_thread_store",
            return_value=MagicMock(),
        ),
        patch(
            "deerflow.runtime.events.store.make_run_event_store",
            return_value=MagicMock(),
        ),
    ):
        legacy_read_adapter = None
        async with langgraph_runtime(app, config):
            assert isinstance(
                app.state.private_work_cutover_guard,
                PrivateWorkCutoverGuard,
            )
            assert isinstance(app.state.private_thread_service, PrivateThreadService)
            assert app.state.private_thread_service._session_factory is session_factory
            assert app.state.private_thread_service._project_scoped_checkpointer is app.state.project_scoped_checkpointer

            assert isinstance(app.state.private_run_service, PrivateRunService)
            assert app.state.private_run_service._session_factory is session_factory

            assert isinstance(app.state.private_file_service, PrivateFileService)
            assert app.state.private_file_service._session_factory is session_factory

            assert isinstance(app.state.private_file_streamer, PrivateFileStreamer)
            assert app.state.private_file_streamer._session_factory is session_factory

            assert isinstance(app.state.project_memory_service, PrivateMemoryService)
            assert app.state.project_memory_service._session_factory is session_factory

            assert isinstance(app.state.channel_connection_repo, ChannelConnectionRepository)
            assert app.state.channel_connection_repo.session_factory is session_factory

            assert isinstance(app.state.project_connection_service, ProjectConnectionService)
            assert app.state.project_connection_service._session_factory is session_factory
            assert app.state.project_connection_service._repository is app.state.channel_connection_repo

            assert isinstance(app.state.private_run_event_store, DbRunEventStore)
            assert app.state.private_run_event_store._sf is session_factory
            assert app.state.private_run_event_store._max_trace_content == 321

            assert isinstance(app.state.automation_cutover_guard, AutomationCutoverGuard)
            assert app.state.automation_service._min_once_delay_seconds == 73
            assert isinstance(
                app.state.automation_readiness_service,
                AutomationReadinessService,
            )
            assert isinstance(
                app.state.automation_occurrence_service,
                AutomationOccurrenceService,
            )
            assert app.state.automation_occurrence_service._session_factory is session_factory
            assert isinstance(app.state.automation_reconciler, AutomationReconciler)
            assert app.state.automation_reconciler._session_factory is session_factory
            assert isinstance(app.state.automation_dispatcher, AutomationDispatcher)
            assert app.state.automation_dispatcher._session_factory is session_factory
            assert isinstance(app.state.scheduled_task_service, ScheduledTaskService)
            assert app.state.automation_scheduler is app.state.scheduled_task_service
            assert app.state.automation_scheduler_task is None
            assert app.state.scheduled_task_service.app is app
            assert app.state.scheduled_task_service._ownership is app.state.automation_scheduler_ownership
            assert app.state.scheduled_task_repo is not None
            assert app.state.scheduled_task_repo is app.state.scheduled_task_run_repo
            legacy_read_adapter = app.state.scheduled_task_repo
            assert legacy_read_adapter.closed is False

        assert legacy_read_adapter is not None
        assert legacy_read_adapter.closed is True
        assert app.state.scheduled_task_repo is None
        assert app.state.scheduled_task_run_repo is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_private_events_survive_langgraph_runtime_rebuild(
    monkeypatch,
    migrated_postgres_database_url: str,
) -> None:
    monkeypatch.setenv("GATEWAY_WORKERS", "2")
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    config = SimpleNamespace(
        database=SimpleNamespace(),
        run_events=SimpleNamespace(
            backend="memory",
            max_trace_content=654,
        ),
        stream_bridge=None,
    )
    thread_id = "private-runtime-rebuild-thread"
    run_id = "private-runtime-rebuild-run"
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        await PrivateRunRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(run_id=run_id),
        )

    try:
        with (
            patch(
                "deerflow.persistence.engine.init_engine_from_config",
                new=AsyncMock(),
            ),
            patch(
                "deerflow.persistence.engine.get_session_factory",
                return_value=seed.factory,
            ),
            patch("deerflow.persistence.engine.close_engine", new=AsyncMock()),
            patch(
                "deerflow.runtime.make_stream_bridge",
                side_effect=lambda _config: _context(MagicMock()),
            ),
            patch(
                "deerflow.runtime.checkpointer.async_provider.make_checkpointer",
                side_effect=lambda _config: _context(MagicMock()),
            ),
            patch(
                "deerflow.runtime.make_store",
                side_effect=lambda _config: _context(MagicMock()),
            ),
            patch(
                "deerflow.persistence.thread_meta.make_thread_store",
                return_value=MagicMock(),
            ),
            patch(
                "deerflow.runtime.events.store.make_run_event_store",
                return_value=MagicMock(),
            ),
        ):
            first_app = FastAPI()
            async with langgraph_runtime(first_app, config):
                first_store = first_app.state.private_run_event_store
                assert isinstance(first_store, DbRunEventStore)
                await first_store.put(
                    thread_id=thread_id,
                    run_id=run_id,
                    event_type="human_message",
                    category="message",
                    content={"role": "user", "content": "survives restart"},
                    scope=seed.owner_a_scope,
                )

            second_app = FastAPI()
            async with langgraph_runtime(second_app, config):
                second_store = second_app.state.private_run_event_store
                assert isinstance(second_store, DbRunEventStore)
                assert second_store is not first_store
                messages = await second_store.list_messages(
                    thread_id,
                    scope=seed.owner_a_scope,
                )

        assert [message["content"] for message in messages] == [
            {"role": "user", "content": "survives restart"},
        ]
    finally:
        await seed.engine.dispose()
