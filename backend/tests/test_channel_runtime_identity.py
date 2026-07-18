"""Project-scoped IM-channel runtime identity contracts."""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

from app.channels.message_bus import InboundMessage
from app.private_work import connection_inbound
from app.private_work.connection_inbound import build_gateway_project_run_launcher
from app.private_work.context import PrivateWorkContext
from app.private_work.run_service import PrivateRunService
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="channel-private-runtime",
        )
    )


def test_channel_launcher_imports_only_project_private_http_runtime() -> None:
    source = inspect.getsource(connection_inbound)

    assert "from app.private_work.http_runtime import start_private_run" in source
    assert "app.gateway.services" not in source
    assert "get_run_manager" not in source
    assert "get_stream_bridge" not in source


@pytest.mark.asyncio
async def test_project_channel_launcher_uses_resolved_owner_scope_and_durable_run() -> None:
    context = _context()
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    captured: dict[str, object] = {}

    async def private_start(body, selected_thread_id, request, selected_context):
        captured.update(
            body=body,
            thread_id=selected_thread_id,
            request=request,
            context=selected_context,
        )
        return SimpleNamespace(run_id=run_id)

    class DurableRunService(PrivateRunService):
        def __init__(self) -> None:
            pass

        async def get(self, selected_context, selected_thread_id, selected_run_id):
            assert selected_context is context
            assert selected_thread_id == thread_id
            assert selected_run_id == run_id
            return SimpleNamespace(status="success")

    class Checkpointer:
        def for_context(self, selected_context):
            assert selected_context is context
            return self

        async def aget_tuple(self, config):
            assert config["configurable"]["thread_id"] == thread_id
            return SimpleNamespace(
                checkpoint={
                    "channel_values": {
                        "messages": [
                            {"role": "assistant", "content": "done"},
                        ]
                    }
                }
            )

    app = SimpleNamespace(
        state=SimpleNamespace(
            private_run_service=DurableRunService(),
            project_scoped_checkpointer=Checkpointer(),
        )
    )
    message = InboundMessage(
        channel_name="slack",
        chat_id="conversation-1",
        user_id="external-user",
        text="hello",
    )

    state = await build_gateway_project_run_launcher(
        app=app,
        start_private_run_fn=private_start,
    )(context, thread_id, message)

    assert captured["thread_id"] == thread_id
    assert captured["context"] is context
    body = captured["body"]
    assert body.input == {
        "messages": [{"role": "user", "content": "hello"}],
    }
    assert body.context == {
        "channel_name": "slack",
        "channel_user_id": "external-user",
    }
    assert state == {
        "messages": [{"role": "assistant", "content": "done"}],
    }
