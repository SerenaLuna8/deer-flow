from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from app.gateway.app import create_app
from app.gateway.deps import langgraph_runtime
from app.private_work.connection_service import ProjectConnectionService
from app.private_work.file_service import PrivateFileService
from app.private_work.file_streaming import PrivateFileStreamer
from app.private_work.memory_service import PrivateMemoryService
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_service import PrivateThreadService
from deerflow.persistence.channel_connections import ChannelConnectionRepository


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
        run_events=None,
        stream_bridge=None,
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
        async with langgraph_runtime(app, config):
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
