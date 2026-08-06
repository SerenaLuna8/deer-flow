from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.deps import (
    get_current_agent_runtime_config,
    private_work_context,
    require_project_private_open,
)
from app.gateway.routers import project_memory as memory_router
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkConflict, PrivateWorkNotFound
from app.private_work.memory_service import PrivateMemoryDocumentService
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentVersionRecord,
    MemoryDreamAdmissionRecord,
)
from deerflow.runtime.context_compaction import ThreadCompactionResult


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="memory-dream-api",
        )
    )


class _Service:
    def __init__(self) -> None:
        self.job_id = uuid.uuid4()
        self.dream_calls: list[str | None] = []
        self.dream_configs: list[object] = []
        self.restore_calls: list[tuple[int, int]] = []
        self.error: Exception | None = None

    async def dream(self, context, *, thread_id=None, app_config=None):
        if self.error is not None:
            raise self.error
        self.dream_calls.append(thread_id)
        self.dream_configs.append(app_config)
        return MemoryDreamAdmissionRecord(
            disposition="queued",
            job_id=self.job_id,
            history_count=7,
        )

    async def restore(
        self,
        context,
        *,
        target_version: int,
        expected_current_version: int,
    ):
        if self.error is not None:
            raise self.error
        self.restore_calls.append((target_version, expected_current_version))
        return MemoryDocumentVersionRecord(
            version=13,
            content="# 用户偏好与协作方式\n\n# 项目背景\n\n# 长期约束与架构决策\n\n# 当前仍有效的目标",
            content_digest="a" * 64,
            unified_diff="--- memory-before.md\n+++ memory-after.md\n",
            trigger="restore",
            dream_job_id=None,
            history_from=None,
            history_to=None,
            history_count=None,
            prompt_version=None,
            model_ref=None,
            created_at=datetime(2026, 8, 5, 1, 2, 3, tzinfo=UTC),
        )


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, _Service]:
    service = _Service()
    value = FastAPI()
    value.include_router(memory_router.router)
    value.dependency_overrides[private_work_context] = _context
    value.dependency_overrides[require_project_private_open] = lambda: None
    value.dependency_overrides[get_current_agent_runtime_config] = lambda: "runtime-config"
    monkeypatch.setattr(memory_router, "_service", lambda _request: service)
    return value, service


async def _post(
    app: FastAPI,
    path: str,
    *,
    json: dict[str, object] | None | object = ...,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        if json is ...:
            return await client.post(path)
        return await client.post(path, json=json)


@pytest.mark.asyncio
async def test_manual_dream_accepts_no_body_and_exact_optional_thread_id(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    project_id = uuid.uuid4()

    without_body = await _post(
        value,
        f"/api/projects/{project_id}/memory/dream",
    )
    with_thread = await _post(
        value,
        f"/api/projects/{project_id}/memory/dream",
        json={"threadId": "thread-7"},
    )

    assert without_body.status_code == 200
    assert without_body.json() == {
        "disposition": "queued",
        "jobId": str(service.job_id),
        "historyCount": 7,
    }
    assert with_thread.status_code == 200
    assert service.dream_calls == [None, "thread-7"]
    assert service.dream_configs == ["runtime-config", "runtime-config"]


@pytest.mark.asyncio
async def test_manual_dream_rejects_unknown_or_invalid_body_fields(
    app: tuple[FastAPI, _Service],
) -> None:
    value, _service = app
    project_id = uuid.uuid4()

    extra = await _post(
        value,
        f"/api/projects/{project_id}/memory/dream",
        json={"unexpected": True},
    )
    empty_thread = await _post(
        value,
        f"/api/projects/{project_id}/memory/dream",
        json={"threadId": ""},
    )

    assert extra.status_code == empty_thread.status_code == 422
    assert extra.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"


@pytest.mark.asyncio
async def test_restore_uses_cas_and_returns_complete_version_detail(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    response = await _post(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/versions/4/restore",
        json={"expectedCurrentVersion": 12},
    )

    assert response.status_code == 200
    assert response.json() == {
        "version": 13,
        "trigger": "restore",
        "historyCount": None,
        "changed": True,
        "createdAt": "2026-08-05T01:02:03Z",
        "content": "# 用户偏好与协作方式\n\n# 项目背景\n\n# 长期约束与架构决策\n\n# 当前仍有效的目标",
        "unifiedDiff": "--- memory-before.md\n+++ memory-after.md\n",
    }
    assert service.restore_calls == [(4, 12)]


@pytest.mark.asyncio
async def test_restore_conflict_maps_to_private_409(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    service.error = PrivateWorkConflict("memory-dream-api")

    response = await _post(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/versions/4/restore",
        json={"expectedCurrentVersion": 11},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_CONFLICT"


@pytest.mark.asyncio
async def test_manual_dream_unknown_scoped_thread_maps_to_private_404(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    service.error = PrivateWorkNotFound("memory-dream-api")

    response = await _post(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/dream",
        json={"threadId": "missing-thread"},
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_NOT_FOUND"


@pytest.mark.asyncio
async def test_direct_dream_endpoint_runs_server_archive_barrier_before_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Transaction:
        def __init__(self, session) -> None:
            self.session = session

        async def __aenter__(self):
            self.session.in_tx = True
            return self

        async def __aexit__(self, *_args):
            self.session.in_tx = False
            return False

    class Session:
        def __init__(self) -> None:
            self.in_tx = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def begin(self):
            return Transaction(self)

    session = Session()

    class Barrier:
        def __init__(self) -> None:
            self.results = [
                ThreadCompactionResult(
                    thread_id="thread-7",
                    compacted=True,
                    removed_message_count=2,
                    checkpoint_id="checkpoint-1",
                ),
                ThreadCompactionResult(
                    thread_id="thread-7",
                    compacted=False,
                    reason="not_enough_messages",
                ),
            ]

        async def compact(self, *_args, **_kwargs):
            assert session.in_tx is False
            events.append("compact")
            return self.results.pop(0)

        async def lock_and_verify_dream_archive_ready(
            self,
            barrier_session,
            *_args,
            **_kwargs,
        ):
            assert barrier_session is session
            assert session.in_tx is True
            events.append("seal")
            return True

    class Admission:
        async def admit(self, admission_session, _scope, **_kwargs):
            assert admission_session is session
            assert session.in_tx is True
            events.append("admit")
            return MemoryDreamAdmissionRecord(
                disposition="queued",
                job_id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                history_count=1,
            )

    service = PrivateMemoryDocumentService(
        lambda: session,  # type: ignore[arg-type]
        dream_admission=Admission(),  # type: ignore[arg-type]
        dream_archive_barrier=Barrier(),
    )
    value = FastAPI()
    value.include_router(memory_router.router)
    value.dependency_overrides[private_work_context] = _context
    value.dependency_overrides[require_project_private_open] = lambda: None
    value.dependency_overrides[get_current_agent_runtime_config] = lambda: object()
    monkeypatch.setattr(memory_router, "_service", lambda _request: service)

    response = await _post(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/dream",
        json={"threadId": "thread-7"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "disposition": "queued",
        "jobId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "historyCount": 1,
    }
    assert events == ["compact", "compact", "seal", "admit"]
