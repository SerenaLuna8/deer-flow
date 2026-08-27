from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.private_work import chat_controls as chat_controls_module
from app.private_work.chat_controls import ProjectChatControlService
from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.persistence.context_evidence import ContextSubjectRef
from deerflow.runtime.context_evidence import ContextProjectionHead

THREAD_ID = "11111111-1111-4111-8111-111111111111"
EXECUTION_ID = "22222222-2222-4222-8222-222222222222"


def _context() -> PrivateWorkContext:
    role = ProjectRole.VIEWER
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=role,
            capabilities=capabilities_for(role),
            membership_version=1,
            request_id="context-projection-service",
        )
    )


def _projection(
    *,
    projection_seq: str,
    execution_id: str | None = None,
    phase: str | None = None,
) -> dict[str, object]:
    resolved_phase = phase or ("idle" if execution_id is None else "settled")
    return {
        "contract_version": 2,
        "thread_id": THREAD_ID,
        "subject": {
            "kind": "lead_thread" if execution_id is None else "subagent_task",
            "thread_id": THREAD_ID,
            "execution_id": execution_id,
        },
        "phase": resolved_phase,
        "projection_seq": projection_seq,
        "evidence_seq": "5",
        "context_window_generation": "33333333-3333-4333-8333-333333333333",
        "checkpoint_id": "checkpoint-1",
        "projector_revision": "context-projector-v1",
        "model": {
            "identity_digest": "a" * 64,
            "context_window_tokens": 300_000,
        },
        "basis": "estimated",
        "coverage": "complete",
        "freshness": "current",
        "totals": {
            "projected_tokens": 100,
            "lower_bound_tokens": 100,
            "safety_upper_bound_tokens": 120,
            "context_window_tokens": 300_000,
            "remaining_tokens": 299_900,
            "progress_percent": 0.0,
        },
        "lanes": [
            {
                "lane": "conversation",
                "projected_tokens": 100,
                "lower_bound_tokens": 100,
                "safety_upper_bound_tokens": 120,
            }
        ],
        "last_provider_observation": None,
        "compaction": {
            "enabled": True,
            "threshold_tokens": 240_000,
            "reached": False,
            "authority": ("idle_history" if resolved_phase == "idle" else "frozen_run"),
            "blocked_reason": None,
        },
        "notices": [],
        "as_of": "2026-08-27T00:00:00Z",
    }


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


class _Revalidator:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def require(self, *args: object, **kwargs: object) -> object:
        self.calls.append((*args[2:], kwargs))
        return object()


class _EvidenceRepository:
    def __init__(self, rows: tuple[SimpleNamespace, ...]) -> None:
        self.rows = rows
        self.subjects: list[ContextSubjectRef] = []
        self.cursors: list[int] = []
        self.head_locks: list[bool] = []

    async def read_head(
        self,
        _scope: object,
        subject: ContextSubjectRef,
        *,
        lock: bool,
    ) -> SimpleNamespace | None:
        self.head_locks.append(lock)
        self.subjects.append(subject)
        return self.rows[0] if self.rows else None

    async def page_heads_after(
        self,
        _scope: object,
        *,
        after_projection_seq: int,
        limit: int,
    ) -> tuple[SimpleNamespace, ...]:
        assert limit == 100
        self.cursors.append(after_projection_seq)
        return self.rows


def _service(
    monkeypatch: pytest.MonkeyPatch,
    repository: _EvidenceRepository,
) -> tuple[ProjectChatControlService, _Revalidator]:
    revalidator = _Revalidator()
    service = object.__new__(ProjectChatControlService)
    service._session_factory = lambda: _Session()  # type: ignore[attr-defined]
    service._revalidator = revalidator  # type: ignore[attr-defined]
    monkeypatch.setattr(
        chat_controls_module,
        "PrivateThreadRepository",
        lambda _session: SimpleNamespace(get=lambda **_kwargs: _async_value(SimpleNamespace(thread_id=THREAD_ID))),
    )
    monkeypatch.setattr(
        chat_controls_module,
        "ContextEvidenceRepository",
        lambda _session: repository,
    )
    return service, revalidator


async def _async_value(value: object) -> object:
    return value


@pytest.mark.asyncio
async def test_context_projection_is_an_independent_read_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SimpleNamespace(
        projection_seq=7,
        projection=_projection(projection_seq="7"),
        phase="idle",
        active_run_id=None,
    )
    repository = _EvidenceRepository((row,))
    service, revalidator = _service(monkeypatch, repository)

    projection = await service.context_projection(
        _context(),
        THREAD_ID,
        subject_kind="lead_thread",
        execution_id=None,
    )

    assert projection.projection_seq == "7"
    assert projection.subject.execution_id is None
    assert repository.subjects == [ContextSubjectRef.lead_thread(THREAD_ID)]
    assert revalidator.calls[0][0] is Capability.PRIVATE_WORK_READ_OWN
    assert Capability.PRIVATE_WORK_CREATE not in revalidator.calls[0]


@pytest.mark.asyncio
async def test_context_projection_updates_keep_subject_values_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = (
        SimpleNamespace(
            projection_seq=7,
            projection=_projection(projection_seq="7"),
        ),
        SimpleNamespace(
            projection_seq=9,
            projection=_projection(
                projection_seq="9",
                execution_id=EXECUTION_ID,
            ),
        ),
    )
    repository = _EvidenceRepository(rows)
    service, _revalidator = _service(monkeypatch, repository)

    projections = await service.context_projection_updates(
        _context(),
        THREAD_ID,
        after_projection_seq=6,
    )

    assert repository.cursors == [6]
    assert [item.projection_seq for item in projections] == ["7", "9"]
    assert projections[0].subject.kind.value == "lead_thread"
    assert projections[1].subject.execution_id == EXECUTION_ID


@pytest.mark.asyncio
async def test_missing_projection_head_is_rebuilt_from_evidence_without_runtime_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _EvidenceRepository(())
    service, revalidator = _service(monkeypatch, repository)
    repaired = ContextProjectionHead.from_safe_mapping(_projection(projection_seq="12"))
    calls: list[dict[str, object]] = []

    async def rebuild(_repository: object, **kwargs: object):
        calls.append(kwargs)
        return repaired

    monkeypatch.setattr(
        chat_controls_module,
        "rebuild_context_projection_head",
        rebuild,
    )

    async def no_checkpoint_repair(*_args: object, **_kwargs: object):
        return None

    service._context_checkpoint_repair_snapshot = no_checkpoint_repair  # type: ignore[method-assign]

    projection = await service.context_projection(
        _context(),
        THREAD_ID,
        subject_kind="lead_thread",
        execution_id=None,
    )

    assert projection.projection_seq == "12"
    assert len(revalidator.calls) == 2
    assert calls[0]["subject"] == ContextSubjectRef.lead_thread(THREAD_ID)
    assert calls[0]["discard_existing_head"] is False


@pytest.mark.asyncio
async def test_active_projection_for_terminal_run_is_rebuilt_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SimpleNamespace(
        projection_seq=7,
        projection=_projection(
            projection_seq="7",
            phase="active",
        ),
        phase="active",
        active_run_id="run-terminal",
    )
    repository = _EvidenceRepository((row,))
    service, revalidator = _service(monkeypatch, repository)
    run_reads: list[bool] = []

    class _Runs:
        async def get(self, **kwargs: object) -> object:
            assert kwargs["run_id"] == "run-terminal"
            run_reads.append(bool(kwargs["lock"]))
            return SimpleNamespace(status="error")

    monkeypatch.setattr(
        chat_controls_module,
        "PrivateRunRepository",
        lambda _session: _Runs(),
    )
    repaired = ContextProjectionHead.from_safe_mapping(
        _projection(projection_seq="12"),
    )
    rebuild_calls: list[dict[str, object]] = []

    async def rebuild(_repository: object, **kwargs: object):
        rebuild_calls.append(kwargs)
        return repaired

    monkeypatch.setattr(
        chat_controls_module,
        "rebuild_context_projection_head",
        rebuild,
    )

    async def no_checkpoint_repair(*_args: object, **_kwargs: object):
        return None

    service._context_checkpoint_repair_snapshot = no_checkpoint_repair  # type: ignore[method-assign]

    projection = await service.context_projection(
        _context(),
        THREAD_ID,
        subject_kind="lead_thread",
        execution_id=None,
    )

    assert projection.phase.value == "idle"
    assert projection.projection_seq == "12"
    assert run_reads == [False, False]
    assert len(revalidator.calls) == 2
    assert rebuild_calls[0]["discard_existing_head"] is True


@pytest.mark.asyncio
async def test_empty_lead_thread_returns_zero_projection_instead_of_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _EvidenceRepository(())
    service, _revalidator = _service(monkeypatch, repository)

    async def no_evidence(*_args: object, **_kwargs: object):
        raise LookupError("no Context Evidence yet")

    monkeypatch.setattr(
        chat_controls_module,
        "rebuild_context_projection_head",
        no_evidence,
    )

    async def no_checkpoint(*_args: object, **_kwargs: object):
        return None

    service._context_checkpoint_repair_snapshot = no_checkpoint  # type: ignore[method-assign]

    projection = await service.context_projection(
        _context(),
        THREAD_ID,
        subject_kind="lead_thread",
        execution_id=None,
    )

    assert projection.projection_seq == "0"
    assert projection.evidence_seq == "0"
    assert projection.basis.value == "empty"
    assert projection.totals.projected_tokens == 0
    assert projection.totals.context_window_tokens is None
    assert [notice.code.value for notice in projection.notices] == ["CAPACITY_UNKNOWN"]
