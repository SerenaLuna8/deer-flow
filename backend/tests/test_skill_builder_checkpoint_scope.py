"""Skill Builder checkpoint authority must stay bound to its hidden thread kind."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkNotFound
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()


class _SessionFactory:
    def __call__(self) -> _Session:
        return _Session()


def _context() -> PrivateWorkContext:
    project = ProjectContext(
        user_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        project_id=uuid.UUID("00000000-0000-4000-8000-000000000002"),
        membership_id=uuid.UUID("00000000-0000-4000-8000-000000000003"),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="skill-builder-checkpoint-scope",
    )
    return PrivateWorkContext.from_project(project)


@pytest.mark.anyio
async def test_skill_builder_scoped_checkpointer_reads_skill_builder_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_thread_kinds: list[str] = []

    async def require(*_args: object, **_kwargs: object) -> None:
        return None

    async def get_thread(*_args: object, **kwargs: object) -> object:
        observed_thread_kinds.append(str(kwargs.get("thread_kind")))
        return SimpleNamespace(thread_kind="skill_builder")

    monkeypatch.setattr(
        "app.private_work.checkpointer.PrivateWorkRevalidator.require",
        require,
    )
    monkeypatch.setattr(
        "app.private_work.checkpointer.PrivateThreadRepository.get",
        get_thread,
    )

    saver = ProjectScopedCheckpointer(
        InMemorySaver(),
        _SessionFactory(),  # type: ignore[arg-type]
    ).for_context(
        _context(),
        thread_kind="skill_builder",
    )

    assert (
        await saver.aget_tuple(
            {
                "configurable": {
                    "thread_id": "skill-builder-thread",
                    "checkpoint_ns": "",
                }
            }
        )
        is None
    )
    assert observed_thread_kinds == ["skill_builder"]


@pytest.mark.anyio
async def test_default_scoped_checkpointer_keeps_chat_thread_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_thread_kinds: list[str] = []

    async def require(*_args: object, **_kwargs: object) -> None:
        return None

    async def get_thread(*_args: object, **kwargs: object) -> None:
        observed_thread_kinds.append(str(kwargs.get("thread_kind")))
        return None

    monkeypatch.setattr(
        "app.private_work.checkpointer.PrivateWorkRevalidator.require",
        require,
    )
    monkeypatch.setattr(
        "app.private_work.checkpointer.PrivateThreadRepository.get",
        get_thread,
    )

    saver = ProjectScopedCheckpointer(
        InMemorySaver(),
        _SessionFactory(),  # type: ignore[arg-type]
    ).for_context(_context())

    with pytest.raises(PrivateWorkNotFound):
        await saver.aget_tuple(
            {
                "configurable": {
                    "thread_id": "skill-builder-thread",
                    "checkpoint_ns": "",
                }
            }
        )
    assert observed_thread_kinds == ["chat"]
