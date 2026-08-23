from __future__ import annotations

import uuid
from types import MethodType, SimpleNamespace

import pytest

from app.private_work import chat_controls as chat_controls_module
from app.private_work.chat_controls import ProjectChatControlService
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkCompactionDisabled,
    PrivateWorkThreadBusy,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.runtime.context_compaction import ContextCompactionDisabled


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Rows:
    def scalar_one_or_none(self):
        return "run-incomplete"


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return _Transaction()

    async def execute(self, _statement):
        return _Rows()


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
            request_id="compaction-outcomes",
        )
    )


def _service() -> ProjectChatControlService:
    service = object.__new__(ProjectChatControlService)
    service._session_factory = lambda: _Session()
    service._project_scoped_checkpointer = SimpleNamespace(
        for_context=lambda _context: object(),
    )
    return service


@pytest.mark.asyncio
async def test_compact_reports_incomplete_run_as_thread_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()

    async def require(*_args, **_kwargs):
        return None

    class _ThreadRepository:
        async def get(self, **_kwargs):
            return object()

    service._revalidator = SimpleNamespace(require=require)
    monkeypatch.setattr(
        chat_controls_module,
        "PrivateThreadRepository",
        lambda _session: _ThreadRepository(),
    )

    with pytest.raises(PrivateWorkThreadBusy):
        await service.compact(
            _context(),
            "thread-1",
            force=True,
            keep=("messages", 0),
            app_config=object(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_compact_reports_disabled_policy_as_compaction_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()

    async def lock_thread(*_args, **_kwargs):
        raise ContextCompactionDisabled("disabled")

    monkeypatch.setattr(
        service,
        "_lock_thread",
        MethodType(lock_thread, service),
    )

    with pytest.raises(PrivateWorkCompactionDisabled):
        await service.compact(
            _context(),
            "thread-1",
            force=True,
            keep=("messages", 0),
            app_config=object(),  # type: ignore[arg-type]
        )
