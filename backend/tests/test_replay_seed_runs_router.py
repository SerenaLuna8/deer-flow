from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import seed_runs_router as seed_router
from pydantic import ValidationError
from seed_runs_router import SeedRunsBody, seed_project_runs

from app.private_work.context import PrivateWorkContext
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.runtime.private_scope import PrivateResourceScope


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.UUID("10000000-0000-4000-8000-000000000001"),
            project_id=uuid.UUID("20000000-0000-4000-8000-000000000002"),
            membership_id=uuid.UUID("30000000-0000-4000-8000-000000000003"),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=7,
            request_id="replay-seed-test",
        )
    )


def _body() -> SeedRunsBody:
    return SeedRunsBody.model_validate(
        {
            "thread_id": "40000000-0000-4000-8000-000000000004",
            "agent_asset_id": "50000000-0000-4000-8000-000000000005",
            "agent_scope": "system",
            "runs": [
                {
                    "run_id": "60000000-0000-4000-8000-000000000006",
                    "created_at": "2026-01-01T00:00:00Z",
                    "messages": [
                        {
                            "role": "human",
                            "content": "first question",
                            "id": "first-human",
                        },
                        {
                            "role": "ai",
                            "content": "first answer",
                            "id": "first-ai",
                        },
                    ],
                },
                {
                    "run_id": "70000000-0000-4000-8000-000000000007",
                    "created_at": "2026-01-01T00:01:00Z",
                    "messages": [
                        {
                            "role": "human",
                            "content": "second question",
                            "id": "second-human",
                        },
                        {
                            "role": "ai",
                            "content": "second answer",
                            "id": "second-ai",
                        },
                    ],
                },
            ],
        }
    )


class _RunStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def put(self, run_id: str, **kwargs: Any) -> None:
        self.calls.append((run_id, kwargs))


class _EventStore:
    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, Any]], PrivateResourceScope]] = []

    async def put_batch(
        self,
        events: list[dict[str, Any]],
        *,
        scope: PrivateResourceScope,
    ) -> list[dict[str, Any]]:
        self.calls.append((events, scope))
        return events


@pytest.mark.anyio
async def test_seed_project_runs_uses_one_issued_scope_for_thread_runs_and_events() -> None:
    context = _context()
    body = _body()
    run_store = _RunStore()
    event_store = _EventStore()
    thread_calls: list[tuple[PrivateWorkContext, SeedRunsBody]] = []

    async def create_thread(
        issued: PrivateWorkContext,
        seed: SeedRunsBody,
    ) -> None:
        thread_calls.append((issued, seed))

    result = await seed_project_runs(
        body,
        context=context,
        create_thread=create_thread,
        run_store=run_store,
        event_store=event_store,
    )

    expected_scope = context.resource_scope
    assert thread_calls == [(context, body)]
    assert [run_id for run_id, _ in run_store.calls] == [
        "60000000-0000-4000-8000-000000000006",
        "70000000-0000-4000-8000-000000000007",
    ]
    for _, kwargs in run_store.calls:
        assert kwargs["thread_id"] == "40000000-0000-4000-8000-000000000004"
        assert kwargs["scope"] == expected_scope
        assert kwargs["status"] == "success"
        assert "user_id" not in kwargs
    assert [scope for _, scope in event_store.calls] == [
        expected_scope,
        expected_scope,
    ]
    assert [event["event_type"] for event in event_store.calls[0][0]] == [
        "llm.human.input",
        "llm.ai.response",
    ]
    assert result == {
        "ok": True,
        "thread_id": "40000000-0000-4000-8000-000000000004",
        "runs": 2,
    }


def test_seed_body_rejects_client_supplied_authority() -> None:
    payload = _body().model_dump(mode="json")
    payload["project_id"] = "80000000-0000-4000-8000-000000000008"
    payload["owner_user_id"] = "90000000-0000-4000-8000-000000000009"

    with pytest.raises(ValidationError):
        SeedRunsBody.model_validate(payload)


@pytest.mark.anyio
async def test_prepare_replay_agent_matches_the_golden_project_agent_profile() -> None:
    context = _context()
    calls: list[tuple[object, dict[str, object]]] = []
    asset_id = uuid.UUID("a0000000-0000-4000-8000-00000000000a")

    async def create_agent(
        session_factory: object,
        **kwargs: object,
    ) -> object:
        calls.append((session_factory, kwargs))
        return SimpleNamespace(
            asset=SimpleNamespace(id=asset_id, version=3),
        )

    result = await seed_router.prepare_replay_agent(
        context,
        session_factory=object(),
        create_agent=create_agent,
    )

    assert result == {
        "id": str(asset_id),
        "scope": "project",
        "version": 3,
    }
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["user_id"] == context.user_id
    assert kwargs["project_id"] == context.project_id
    assert kwargs["slug"] == "replay-agent"
    payload = kwargs["payload"]
    assert payload.model_ref == "scenario-model"  # type: ignore[union-attr]
    assert payload.soul == "Use the exact project tools to complete the request."  # type: ignore[union-attr]
    assert payload.tool_groups == ("file:read", "file:write")  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_seed_route_uses_the_project_path_and_current_private_event_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    body = _body()
    run_store = _RunStore()
    current_event_store = _EventStore()
    legacy_event_store = _EventStore()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                run_store=run_store,
                private_run_event_store=current_event_store,
                run_event_store=legacy_event_store,
            )
        )
    )
    calls: list[dict[str, object]] = []

    async def fake_seed(
        seed: SeedRunsBody,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append({"body": seed, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(seed_router, "seed_project_runs", fake_seed)

    result = await seed_router.seed_runs(
        body,
        request,  # type: ignore[arg-type]
        context.project_id,
        context,
    )

    assert result == {"ok": True}
    assert calls == [
        {
            "body": body,
            "context": context,
            "create_thread": seed_router._create_seed_thread,
            "run_store": run_store,
            "event_store": current_event_store,
        }
    ]
    assert legacy_event_store.calls == []
    assert any(route.path == "/api/projects/{project_id}/test-only/seed-runs" for route in seed_router.router.routes)
