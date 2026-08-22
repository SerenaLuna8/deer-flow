from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.gateway.deps import (
    get_memory_dream_prepare_service,
    private_work_context,
    require_project_private_open,
)
from app.gateway.routers import project_memory as memory_router
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkInvalid,
)
from app.private_work.memory_injection import MemoryInjectionAssessment
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentVersionRecord,
    MemoryDreamAdmissionRecord,
    MemoryEpisodePage,
)
from deerflow.persistence.private_work.memory_dream_prepare_repository import (
    MemoryDreamPrepareAdmission,
    MemoryDreamPrepareRecord,
)
from deerflow.skills.slash import parse_slash_skill_reference


def test_dream_command_is_reserved_from_skill_activation() -> None:
    assert parse_slash_skill_reference("/dream") is None
    assert parse_slash_skill_reference("/dream now") is None
    assert parse_slash_skill_reference("/Dream") is None


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
        self.dream_result = MemoryDreamAdmissionRecord(
            disposition="queued",
            job_id=self.job_id,
            history_count=7,
        )
        self.dream_calls = 0
        self.restore_calls: list[tuple[int, int]] = []
        self.restore_result: MemoryDocumentVersionRecord | None = None
        self.versions: tuple[MemoryDocumentVersionRecord, ...] = ()
        self.episode_calls: list[dict[str, object]] = []
        self.episodes: tuple = ()
        self.episode_next_cursor: str | None = None
        self.pending_calls: list[dict[str, object]] = []
        self.pending: tuple = ()
        self.error: Exception | None = None
        self.read_calls: list[str] = []

    async def get(self, _context):
        if self.error is not None:
            raise self.error
        self.read_calls.append("legacy")
        state = SimpleNamespace(
            document=SimpleNamespace(
                content="# 用户偏好与协作方式",
                version=1,
                updated_at=datetime(2026, 8, 5, 1, 2, 3, tzinfo=UTC),
                active_dream_job_id=None,
            ),
            pending_count=2,
        )
        return state, "ok"

    async def get_with_injection_advisory(self, _context):
        if self.error is not None:
            raise self.error
        self.read_calls.append("advisory")
        return (
            SimpleNamespace(
                document=SimpleNamespace(
                    content="# 用户偏好与协作方式",
                    version=1,
                    updated_at=datetime(2026, 8, 5, 1, 2, 3, tzinfo=UTC),
                    active_dream_job_id=None,
                ),
                pending_count=2,
            ),
            MemoryInjectionAssessment(
                status="eligible",
                reason="within_budget",
            ),
        )

    async def list_episodes(self, context, *, q, tags, cursor, limit, before=None):
        if self.error is not None:
            raise self.error
        self.episode_calls.append(
            {
                "q": q,
                "tags": tags,
                "cursor": cursor,
                "before": before,
                "limit": limit,
            }
        )
        return MemoryEpisodePage(
            items=self.episodes,
            next_cursor=self.episode_next_cursor,
        )

    async def list_pending(self, context, *, limit, offset):
        if self.error is not None:
            raise self.error
        self.pending_calls.append({"limit": limit, "offset": offset})
        return self.pending

    async def dream(self, context):
        if self.error is not None:
            raise self.error
        self.dream_calls += 1
        return self.dream_result

    async def list_versions(self, context, *, limit, offset):
        if self.error is not None:
            raise self.error
        return self.versions[offset : offset + limit]

    async def get_version(self, context, version):
        if self.error is not None:
            raise self.error
        return next(row for row in self.versions if row.version == version)

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
        if self.restore_result is not None:
            return self.restore_result
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
            needs_review=False,
            created_at=datetime(2026, 8, 5, 1, 2, 3, tzinfo=UTC),
        )


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, _Service]:
    service = _Service()
    value = FastAPI()
    value.include_router(memory_router.router)
    value.dependency_overrides[private_work_context] = _context
    value.dependency_overrides[require_project_private_open] = lambda: None
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
async def test_memory_document_advisory_is_opt_in_for_rolling_compatibility(
    app: tuple[FastAPI, _Service],
) -> None:
    value, _service = app
    project_id = uuid.uuid4()
    path = f"/api/projects/{project_id}/memory"

    legacy = await _get(value, path)
    advisory = await _get(
        value,
        path,
        params={"injectionContract": "advisory_v1"},
    )

    assert legacy.status_code == advisory.status_code == 200
    assert legacy.json()["injectionStatus"] == "ok"
    assert "injectionAdvisory" not in legacy.json()
    assert advisory.json()["injectionAdvisory"] == {
        "basis": "current_non_continuation",
        "status": "eligible",
        "reason": "within_budget",
    }
    assert _service.read_calls == ["legacy", "advisory"]


class _PrepareService:
    def __init__(self) -> None:
        self.job_id = uuid.uuid4()
        self.dream_job_id = uuid.uuid4()
        self.calls: list[tuple[str, object]] = []
        self.record = MemoryDreamPrepareRecord(
            job_id=self.job_id,
            thread_id="thread-prepare",
            phase="draining",
            compacted_passes=2,
            dream_job_id=self.dream_job_id,
            history_count=3,
            admission_kind="history",
            result_disposition="queued",
            job_status="running",
            public_error_code=None,
            cancel_requested=False,
            updated_at=datetime(2026, 8, 13, 1, 2, 3, tzinfo=UTC),
        )

    async def admit(self, _context, *, thread_id, operation_id):
        self.calls.append(("admit", (thread_id, operation_id)))
        return MemoryDreamPrepareAdmission(
            disposition="queued",
            record=replace(self.record, phase="queued", job_status="queued"),
        )

    async def read_latest(self, _context, *, thread_id):
        self.calls.append(("latest", thread_id))
        return self.record

    async def read(self, _context, job_id):
        self.calls.append(("read", job_id))
        return self.record

    async def cancel(self, _context, job_id):
        self.calls.append(("cancel", job_id))
        return replace(self.record, cancel_requested=True)


@pytest.mark.asyncio
async def test_dream_preparation_owner_scoped_admit_status_and_cancel_contract() -> None:
    service = _PrepareService()
    value = FastAPI()
    value.include_router(memory_router.router)
    value.dependency_overrides[private_work_context] = _context
    value.dependency_overrides[require_project_private_open] = lambda: None
    value.dependency_overrides[get_memory_dream_prepare_service] = lambda: service
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    base = f"/api/projects/{project_id}/memory/dream-preparations"

    admitted = await _post(
        value,
        base,
        json={"threadId": "thread-prepare", "operationId": str(operation_id)},
    )
    latest = await _get(value, f"{base}/latest", params={"threadId": "thread-prepare"})
    exact = await _get(value, f"{base}/{service.job_id}")
    cancelled = await _post(value, f"{base}/{service.job_id}/cancel")

    assert admitted.status_code == 202
    assert admitted.json() == {
        "disposition": "queued",
        "jobId": str(service.job_id),
        "status": "queued",
    }
    assert latest.status_code == exact.status_code == cancelled.status_code == 200
    assert latest.json()["compactedPasses"] == 2
    assert latest.json()["dreamJobId"] == str(service.dream_job_id)
    assert exact.headers["Cache-Control"] == "no-store"
    assert cancelled.json()["cancelRequested"] is True
    assert service.calls == [
        ("admit", ("thread-prepare", operation_id)),
        ("latest", "thread-prepare"),
        ("read", service.job_id),
        ("cancel", service.job_id),
    ]


@pytest.mark.asyncio
async def test_manual_dream_accepts_no_body_and_rejects_legacy_thread_id(
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
    assert with_thread.status_code == 422
    assert service.dream_calls == 1


@pytest.mark.asyncio
async def test_manual_dream_exposes_zero_history_budget_rewrite_admission(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    service.dream_result = MemoryDreamAdmissionRecord(
        disposition="queued",
        job_id=service.job_id,
        history_count=0,
        admission_kind="budget_rewrite",
    )

    response = await _post(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/dream",
    )

    assert response.status_code == 200
    assert response.json() == {
        "disposition": "queued",
        "jobId": str(service.job_id),
        "historyCount": 0,
        "admissionKind": "budget_rewrite",
    }


@pytest.mark.asyncio
async def test_manual_dream_preserves_nothing_pending_response_shape(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    service.dream_result = MemoryDreamAdmissionRecord(
        disposition="nothing_pending",
        job_id=None,
        history_count=0,
    )

    response = await _post(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/dream",
    )

    assert response.status_code == 200
    assert response.json() == {
        "disposition": "nothing_pending",
        "jobId": None,
        "historyCount": 0,
    }


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
        f"/api/projects/{uuid.uuid4()}/memory/versions/4/restore?responseContract=preview_v1",
        json={"expectedCurrentVersion": 12},
    )

    assert response.status_code == 200
    assert response.json() == {
        "version": 13,
        "trigger": "restore",
        "historyCount": None,
        "changed": True,
        "needsReview": False,
        "createdAt": "2026-08-05T01:02:03Z",
        "content": "# 用户偏好与协作方式\n\n# 项目背景\n\n# 长期约束与架构决策\n\n# 当前仍有效的目标",
        "unifiedDiff": "--- memory-before.md\n+++ memory-after.md\n",
        "diffTruncated": False,
    }
    assert service.restore_calls == [(4, 12)]


@pytest.mark.asyncio
async def test_budget_rewrite_version_endpoints_accept_zero_history(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    created_at = datetime(2026, 8, 5, 2, 3, 4, tzinfo=UTC)
    service.versions = (
        MemoryDocumentVersionRecord(
            version=14,
            content="# 用户偏好与协作方式\n\n# 项目背景\n\n# 长期约束与架构决策\n\n# 当前仍有效的目标",
            content_digest="b" * 64,
            unified_diff="--- memory-before.md\n+++ memory-after.md\n",
            trigger="budget_rewrite",
            dream_job_id=uuid.uuid4(),
            history_from=None,
            history_to=None,
            history_count=0,
            prompt_version="dream-v3",
            needs_review=False,
            created_at=created_at,
        ),
    )
    project_id = uuid.uuid4()

    listed = await _get(
        value,
        f"/api/projects/{project_id}/memory/versions",
    )
    detail = await _get(
        value,
        f"/api/projects/{project_id}/memory/versions/14?responseContract=preview_v1",
    )

    expected_summary = {
        "version": 14,
        "trigger": "budget_rewrite",
        "historyCount": 0,
        "changed": True,
        "needsReview": False,
        "createdAt": "2026-08-05T02:03:04Z",
    }
    assert listed.status_code == detail.status_code == 200
    assert listed.json() == {"items": [expected_summary]}
    assert detail.json() == {
        **expected_summary,
        "content": service.versions[0].content,
        "unifiedDiff": service.versions[0].unified_diff,
        "diffTruncated": False,
    }


@pytest.mark.asyncio
async def test_version_detail_and_restore_bound_legacy_oversized_diff(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    oversized = "--- memory-before.md\n+++ memory-after.md\n" + ("+" + "x" * 100 + "\n") * 700
    record = MemoryDocumentVersionRecord(
        version=15,
        content="# 用户偏好与协作方式\n\n# 项目背景\n\n# 长期约束与架构决策\n\n# 当前仍有效的目标",
        content_digest="c" * 64,
        unified_diff=oversized,
        trigger="restore",
        dream_job_id=None,
        history_from=None,
        history_to=None,
        history_count=None,
        prompt_version=None,
        needs_review=False,
        created_at=datetime(2026, 8, 5, 3, 4, 5, tzinfo=UTC),
    )
    service.versions = (record,)
    service.restore_result = record
    project_id = uuid.uuid4()

    detail = await _get(
        value,
        f"/api/projects/{project_id}/memory/versions/15?responseContract=preview_v1",
    )
    restored = await _post(
        value,
        f"/api/projects/{project_id}/memory/versions/15/restore?responseContract=preview_v1",
        json={"expectedCurrentVersion": 14},
    )

    for response in (detail, restored):
        assert response.status_code == 200
        body = response.json()
        assert body["diffTruncated"] is True
        assert len(body["unifiedDiff"]) <= 64_000
        assert body["unifiedDiff"].endswith("\n")
        assert oversized.startswith(body["unifiedDiff"])


@pytest.mark.asyncio
async def test_version_detail_legacy_contract_omits_truncation_field(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    project_id = uuid.uuid4()
    record = await service.restore(
        None,
        target_version=4,
        expected_current_version=12,
    )
    service.versions = (record,)

    response = await _get(
        value,
        f"/api/projects/{project_id}/memory/versions/{record.version}",
    )

    assert response.status_code == 200
    assert "diffTruncated" not in response.json()


@pytest.mark.asyncio
async def test_version_detail_counts_unicode_code_points_in_preview_v1(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    astral_diff = "😀\n" * 22_000
    record = await service.restore(
        None,
        target_version=4,
        expected_current_version=12,
    )
    service.versions = (replace(record, unified_diff=astral_diff),)
    project_id = uuid.uuid4()

    current = await _get(
        value,
        f"/api/projects/{project_id}/memory/versions/{record.version}",
        params={"responseContract": "preview_v1"},
    )
    legacy = await _get(
        value,
        f"/api/projects/{project_id}/memory/versions/{record.version}",
    )

    assert current.status_code == legacy.status_code == 200
    assert current.json()["unifiedDiff"] == astral_diff
    assert current.json()["diffTruncated"] is False
    assert "diffTruncated" not in legacy.json()
    assert len(legacy.json()["unifiedDiff"].encode("utf-16-le")) // 2 <= 64_000


@pytest.mark.parametrize(
    ("trigger", "history_count"),
    (
        ("auto_dream", 0),
        ("manual_dream", None),
        ("budget_rewrite", 1),
        ("restore", 0),
    ),
)
def test_version_response_rejects_history_count_outside_trigger_contract(
    trigger: str,
    history_count: int | None,
) -> None:
    with pytest.raises(ValidationError):
        memory_router.ProjectMemoryVersionSummary(
            version=1,
            trigger=trigger,
            historyCount=history_count,
            changed=True,
            needsReview=False,
            createdAt=datetime(2026, 8, 5, tzinfo=UTC),
        )


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
async def test_manual_dream_legacy_thread_is_rejected_before_service(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    response = await _post(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/dream",
        json={"threadId": "missing-thread"},
    )

    assert response.status_code == 422
    assert service.dream_calls == 0


async def _get(app: FastAPI, path: str, params: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.get(path, params=params)


@pytest.mark.asyncio
async def test_episodes_endpoint_returns_ranked_items_with_exact_filters(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    episode_id = uuid.uuid4()
    occurred = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)
    service.episodes = (
        SimpleNamespace(
            id=episode_id,
            thread_id="thread-9",
            origin="snip",
            tagged_text="- [durable] deployment target is region-eu",
            occurred_at=occurred,
            created_at=occurred,
        ),
    )

    response = await _get(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/episodes",
        params=[
            ("q", " deployment "),
            ("tags", "durable"),
            ("tags", "permanent"),
            ("tags", "durable"),
            ("limit", "10"),
            ("pagination", "keyset_v1"),
        ],
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": str(episode_id),
                "threadId": "thread-9",
                "origin": "snip",
                "taggedText": "- [durable] deployment target is region-eu",
                "occurredAt": "2026-08-01T10:00:00Z",
                "createdAt": "2026-08-01T10:00:00Z",
            }
        ],
        "nextCursor": None,
    }
    assert service.episode_calls == [
        {
            "q": "deployment",
            "tags": ("durable", "permanent"),
            "cursor": None,
            "before": None,
            "limit": 10,
        }
    ]


@pytest.mark.asyncio
async def test_episodes_endpoint_preserves_legacy_response_and_before_cursor(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    before = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)

    response = await _get(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/episodes",
        params={"before": before.isoformat(), "limit": "20"},
    )

    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert service.episode_calls == [
        {
            "q": None,
            "tags": (),
            "cursor": None,
            "before": before,
            "limit": 20,
        }
    ]


@pytest.mark.asyncio
async def test_episodes_endpoint_browses_by_cursor_without_query(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    service.episode_next_cursor = "next-opaque-cursor"

    response = await _get(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/episodes",
        params={"pagination": "keyset_v1", "cursor": "opaque-cursor"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "nextCursor": "next-opaque-cursor",
    }
    assert service.episode_calls == [
        {
            "q": None,
            "tags": (),
            "cursor": "opaque-cursor",
            "before": None,
            "limit": 20,
        }
    ]


@pytest.mark.asyncio
async def test_episodes_endpoint_rejects_out_of_contract_parameters(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    base = f"/api/projects/{uuid.uuid4()}/memory/episodes"

    too_long = await _get(value, base, params={"q": "x" * 201})
    bad_tag = await _get(value, base, params={"tags": "skip"})
    zero_limit = await _get(value, base, params={"limit": "0"})
    big_limit = await _get(value, base, params={"limit": "51"})
    oversized_cursor = await _get(value, base, params={"cursor": "x" * 513})
    unversioned_cursor = await _get(value, base, params={"cursor": "opaque"})
    mixed_cursor = await _get(
        value,
        base,
        params={
            "pagination": "keyset_v1",
            "cursor": "opaque",
            "before": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
        },
    )

    assert too_long.status_code == 422
    assert bad_tag.status_code == 422
    assert zero_limit.status_code == 422
    assert big_limit.status_code == 422
    assert oversized_cursor.status_code == 422
    assert unversioned_cursor.status_code == 422
    assert mixed_cursor.status_code == 422
    assert service.episode_calls == []


@pytest.mark.asyncio
async def test_episodes_endpoint_maps_invalid_opaque_cursor_to_private_422(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    service.error = PrivateWorkInvalid("memory-dream-api")

    response = await _get(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/episodes",
        params={"pagination": "keyset_v1", "cursor": "invalid-cursor"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"


@pytest.mark.asyncio
async def test_pending_endpoint_exposes_the_backlog_in_dream_order(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    created = datetime(2026, 8, 6, 9, 0, 0, tzinfo=UTC)
    service.pending = (
        SimpleNamespace(
            sequence=41,
            origin="tool",
            tagged_text="- [durable] deployment target is region-eu",
            created_at=created,
        ),
        SimpleNamespace(
            sequence=42,
            origin="snip",
            tagged_text="- [ephemeral] debugging the flaky import",
            created_at=created,
        ),
    )

    response = await _get(
        value,
        f"/api/projects/{uuid.uuid4()}/memory/pending",
        params={"limit": "25", "offset": "50"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "sequence": 41,
                "origin": "tool",
                "taggedText": "- [durable] deployment target is region-eu",
                "createdAt": "2026-08-06T09:00:00Z",
            },
            {
                "sequence": 42,
                "origin": "snip",
                "taggedText": "- [ephemeral] debugging the flaky import",
                "createdAt": "2026-08-06T09:00:00Z",
            },
        ]
    }
    assert response.headers["Cache-Control"] == "no-store"
    assert service.pending_calls == [{"limit": 25, "offset": 50}]


@pytest.mark.asyncio
async def test_pending_endpoint_rejects_out_of_contract_pagination(
    app: tuple[FastAPI, _Service],
) -> None:
    value, service = app
    base = f"/api/projects/{uuid.uuid4()}/memory/pending"

    defaults = await _get(value, base)
    zero_limit = await _get(value, base, params={"limit": "0"})
    big_limit = await _get(value, base, params={"limit": "101"})
    negative_offset = await _get(value, base, params={"offset": "-1"})
    big_offset = await _get(value, base, params={"offset": "10001"})

    assert defaults.status_code == 200
    assert zero_limit.status_code == 422
    assert big_limit.status_code == 422
    assert negative_offset.status_code == 422
    assert big_offset.status_code == 422
    assert service.pending_calls == [{"limit": 50, "offset": 0}]
