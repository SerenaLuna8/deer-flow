from __future__ import annotations

import uuid
from types import MethodType, SimpleNamespace

import pytest

from app.private_work import chat_controls as chat_controls_module
from app.private_work.chat_controls import ProjectChatControlService
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole


def _context() -> PrivateWorkContext:
    role = ProjectRole.ADMIN
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=role,
            capabilities=capabilities_for(role),
            membership_version=1,
            request_id="context-usage-service",
        )
    )


@pytest.mark.asyncio
async def test_context_usage_reuses_compact_authority_without_blocking_active_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(ProjectChatControlService)
    events: list[object] = []
    source_config = object()
    runtime_config = object()
    snapshot = SimpleNamespace(
        values={"messages": []},
        config={"configurable": {"checkpoint_id": "checkpoint-1"}},
    )
    expected = object()

    async def validate(
        _service,
        _context,
        thread_id: str,
        *,
        reject_incomplete_run: bool,
    ) -> None:
        events.append(("authorize", thread_id, reject_incomplete_run))

    async def materialize(
        _service,
        _context,
        thread_id: str,
        app_config,
    ):
        events.append(("materialize", thread_id, app_config))
        return runtime_config

    class _State:
        async def aget(self, config):
            events.append(("read", config))
            return snapshot

    def state(_service, _context, app_config, *, as_node: str):
        events.append(("state", app_config, as_node))
        return _State()

    monkeypatch.setattr(
        service,
        "_validate_control_authority",
        MethodType(validate, service),
    )
    monkeypatch.setattr(
        service,
        "_materialize_compaction_config",
        MethodType(materialize, service),
    )
    monkeypatch.setattr(service, "_state", MethodType(state, service))
    monkeypatch.setattr(
        chat_controls_module,
        "measure_thread_context_usage",
        lambda actual_snapshot, *, app_config: events.append(("measure", actual_snapshot, app_config)) or expected,
        raising=False,
    )

    result = await service.context_usage(
        _context(),
        "thread-1",
        app_config=source_config,
    )

    assert result is expected
    assert events == [
        ("authorize", "thread-1", False),
        ("materialize", "thread-1", source_config),
        ("state", runtime_config, "context_usage"),
        (
            "read",
            {
                "configurable": {
                    "thread_id": "thread-1",
                    "checkpoint_ns": "",
                }
            },
        ),
        ("measure", snapshot, runtime_config),
    ]
