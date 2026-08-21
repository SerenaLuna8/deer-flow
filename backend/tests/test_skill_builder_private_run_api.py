from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from support.private_thread_seed import (
    PrivateThreadSeed,
    seed_private_thread_database,
)

from app.gateway.deps import (
    get_config,
    private_work_context,
    require_project_private_open,
)
from app.gateway.routers import private_work as private_work_router
from app.private_work.run_repository import (
    PrivateRunCreate,
    PrivateRunRepository,
)
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from deerflow.persistence.run.sql import RunRepository
from deerflow.runtime import DisconnectMode
from deerflow.runtime.events.models import StreamFrame
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.events.stream import PostgresStreamBridge

_RAW_BUILDER_CANARIES = (
    "P0_RAW_REASONING_CANARY",
    "P0_RAW_TOOL_ARGS_CANARY",
    "P0_RAW_TOOL_RESULT_CANARY",
    "P0_RAW_FILE_CONTENT_CANARY",
)


async def _seed_run_feed(
    seed: PrivateThreadSeed,
    *,
    thread_kind: str,
) -> tuple[str, str]:
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    event_store = DbRunEventStore(
        seed.factory,
        run_event_notify_enabled=False,
    )
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(
                asset_id=seed.project_agent_id,
                scope="project",
            ),
            thread_kind=thread_kind,
        )
        await PrivateRunRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            request=PrivateRunCreate(
                run_id=run_id,
                status="success",
            ),
        )
        await event_store.append_stream_frame(
            session,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=run_id,
            frame=StreamFrame(
                event="messages",
                data=[
                    {
                        "type": "ai",
                        "additional_kwargs": {
                            "reasoning_content": _RAW_BUILDER_CANARIES[0],
                        },
                        "tool_calls": [
                            {
                                "id": "call-private-write",
                                "name": "upsert_candidate_file",
                                "args": {
                                    "path": "SKILL.md",
                                    "content": _RAW_BUILDER_CANARIES[3],
                                    "private_note": _RAW_BUILDER_CANARIES[1],
                                },
                            }
                        ],
                    },
                    {"langgraph_node": "agent"},
                ],
            ),
        )
        await event_store.append_stream_frame(
            session,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=run_id,
            frame=StreamFrame(
                event="messages",
                data=[
                    {
                        "type": "tool",
                        "tool_call_id": "call-private-write",
                        "name": "upsert_candidate_file",
                        "content": json.dumps(
                            {
                                "raw_result": _RAW_BUILDER_CANARIES[2],
                                "content": _RAW_BUILDER_CANARIES[3],
                            },
                            separators=(",", ":"),
                        ),
                    },
                    {"langgraph_node": "tools"},
                ],
            ),
        )
    await event_store.put(
        thread_id=thread_id,
        run_id=run_id,
        event_type="ai_message",
        category="message",
        content={
            "type": "ai",
            "additional_kwargs": {
                "reasoning_content": _RAW_BUILDER_CANARIES[0],
            },
            "tool_calls": [
                {
                    "id": "call-private-write",
                    "name": "upsert_candidate_file",
                    "args": {
                        "content": _RAW_BUILDER_CANARIES[3],
                        "private_note": _RAW_BUILDER_CANARIES[1],
                    },
                }
            ],
            "raw_tool_result": _RAW_BUILDER_CANARIES[2],
        },
        scope=seed.owner_a_scope,
    )
    async with seed.factory() as session, session.begin():
        await event_store.append_stream_frame(
            session,
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            run_id=run_id,
            frame=StreamFrame.end(status="success"),
        )
    return thread_id, run_id


async def _get(
    app: FastAPI,
    path: str,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.get(path)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_browser_run_feeds_keep_chat_public_but_hide_skill_builder_raw_frames(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        chat_thread_id, chat_run_id = await _seed_run_feed(
            seed,
            thread_kind="chat",
        )
        builder_thread_id, builder_run_id = await _seed_run_feed(
            seed,
            thread_kind="skill_builder",
        )

        app = FastAPI()
        app.include_router(private_work_router.router)
        app.dependency_overrides[private_work_context] = lambda: seed.owner_a
        app.dependency_overrides[require_project_private_open] = lambda: None
        app.state.private_run_service = PrivateRunService(seed.factory)
        app.state.private_run_event_store = DbRunEventStore(
            seed.factory,
            run_event_notify_enabled=False,
        )
        app.state.private_stream_bridge = PostgresStreamBridge(
            seed.factory,
            run_event_notify_enabled=False,
        )
        app.state.run_store = RunRepository(seed.factory)

        project_id = seed.owner_a.project_id
        responses = {
            "chat-runs": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{chat_thread_id}/runs",
            ),
            "chat-run": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{chat_thread_id}/runs/{chat_run_id}",
            ),
            "chat-run-messages": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{chat_thread_id}/runs/{chat_run_id}/messages",
            ),
            "chat-thread-messages": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{chat_thread_id}/messages",
            ),
            "chat-events": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{chat_thread_id}/runs/{chat_run_id}/events",
            ),
            "chat-thread-events": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{chat_thread_id}/events?run_id={chat_run_id}",
            ),
            "chat-stream": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{chat_thread_id}/runs/{chat_run_id}/stream",
            ),
            "chat-token-usage": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{chat_thread_id}/token-usage",
            ),
            "skill-builder-runs": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{builder_thread_id}/runs",
            ),
            "skill-builder-run": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{builder_thread_id}/runs/{builder_run_id}",
            ),
            "skill-builder-run-messages": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{builder_thread_id}/runs/{builder_run_id}/messages",
            ),
            "skill-builder-thread-messages": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{builder_thread_id}/messages",
            ),
            "skill-builder-events": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{builder_thread_id}/runs/{builder_run_id}/events",
            ),
            "skill-builder-thread-events": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{builder_thread_id}/events?run_id={builder_run_id}",
            ),
            "skill-builder-stream": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{builder_thread_id}/runs/{builder_run_id}/stream",
            ),
            "skill-builder-token-usage": await _get(
                app,
                f"/api/projects/{project_id}/private-work/threads/{builder_thread_id}/token-usage",
            ),
        }
        observed = {
            name: {
                "status": response.status_code,
                "leaked_canaries": tuple(canary for canary in _RAW_BUILDER_CANARIES if canary in response.text),
            }
            for name, response in responses.items()
        }

        assert observed == {
            "chat-runs": {
                "status": 200,
                "leaked_canaries": (),
            },
            "chat-run": {
                "status": 200,
                "leaked_canaries": (),
            },
            "chat-run-messages": {
                "status": 200,
                "leaked_canaries": _RAW_BUILDER_CANARIES,
            },
            "chat-thread-messages": {
                "status": 200,
                "leaked_canaries": _RAW_BUILDER_CANARIES,
            },
            "chat-events": {
                "status": 200,
                "leaked_canaries": _RAW_BUILDER_CANARIES,
            },
            "chat-thread-events": {
                "status": 200,
                "leaked_canaries": _RAW_BUILDER_CANARIES,
            },
            "chat-stream": {
                "status": 200,
                "leaked_canaries": _RAW_BUILDER_CANARIES,
            },
            "chat-token-usage": {
                "status": 200,
                "leaked_canaries": (),
            },
            "skill-builder-runs": {
                "status": 404,
                "leaked_canaries": (),
            },
            "skill-builder-run": {
                "status": 404,
                "leaked_canaries": (),
            },
            "skill-builder-run-messages": {
                "status": 404,
                "leaked_canaries": (),
            },
            "skill-builder-thread-messages": {
                "status": 404,
                "leaked_canaries": (),
            },
            "skill-builder-events": {
                "status": 404,
                "leaked_canaries": (),
            },
            "skill-builder-thread-events": {
                "status": 404,
                "leaked_canaries": (),
            },
            "skill-builder-stream": {
                "status": 404,
                "leaked_canaries": (),
            },
            "skill-builder-token-usage": {
                "status": 404,
                "leaked_canaries": (),
            },
        }
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_generic_browser_run_controls_reject_skill_builder_threads_before_launch_or_mutation(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    try:
        chat_thread_id, chat_run_id = await _seed_run_feed(
            seed,
            thread_kind="chat",
        )
        builder_thread_id, builder_run_id = await _seed_run_feed(
            seed,
            thread_kind="skill_builder",
        )

        app = FastAPI()
        app.include_router(private_work_router.router)
        app.dependency_overrides[private_work_context] = lambda: seed.owner_a
        app.dependency_overrides[require_project_private_open] = lambda: None
        app.dependency_overrides[get_config] = lambda: object()
        app.state.private_run_service = PrivateRunService(seed.factory)
        app.state.private_stream_bridge = PostgresStreamBridge(
            seed.factory,
            run_event_notify_enabled=False,
        )
        app.state.project_scoped_checkpointer = object()

        launched_thread_ids: list[str] = []

        async def launcher(
            _body: object,
            selected_thread_id: str,
            _request: object,
            _context: object,
        ) -> SimpleNamespace:
            launched_thread_ids.append(selected_thread_id)
            return SimpleNamespace(
                run_id=chat_run_id,
                thread_id=selected_thread_id,
                assistant_id=None,
                status="success",
                metadata={},
                multitask_strategy="reject",
                error=None,
                model_name=None,
                kwargs={},
                created_at="2026-08-22T00:00:00+00:00",
                updated_at="2026-08-22T00:00:00+00:00",
                on_disconnect=DisconnectMode.cancel,
            )

        async def consumer(**_kwargs: object):
            yield 'event: end\ndata: {"status":"success"}\n\n'

        async def wait_for_run(**_kwargs: object) -> tuple[bool, SimpleNamespace]:
            return False, SimpleNamespace(status="success", error=None)

        monkeypatch.setattr(private_work_router, "start_private_run", launcher)
        monkeypatch.setattr(
            private_work_router,
            "_durable_private_sse_consumer",
            consumer,
        )
        monkeypatch.setattr(
            private_work_router,
            "_wait_for_durable_private_run",
            wait_for_run,
        )

        project_id = seed.owner_a.project_id
        chat_base = f"/api/projects/{project_id}/private-work/threads/{chat_thread_id}/runs"
        builder_base = f"/api/projects/{project_id}/private-work/threads/{builder_thread_id}/runs"
        builder_thread_base = f"/api/projects/{project_id}/private-work/threads/{builder_thread_id}"
        file_id = uuid.uuid4()
        artifact_id = uuid.uuid4()
        body = {"input": {}}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            responses = {
                "chat-create": await client.post(chat_base, json=body),
                "skill-builder-create": await client.post(builder_base, json=body),
                "chat-stream": await client.post(
                    f"{chat_base}/stream",
                    json=body,
                ),
                "skill-builder-stream": await client.post(
                    f"{builder_base}/stream",
                    json=body,
                ),
                "chat-wait": await client.post(
                    f"{chat_base}/wait",
                    json=body,
                ),
                "skill-builder-wait": await client.post(
                    f"{builder_base}/wait",
                    json=body,
                ),
                "chat-cancel": await client.post(
                    f"{chat_base}/{chat_run_id}/cancel",
                ),
                "skill-builder-cancel": await client.post(
                    f"{builder_base}/{builder_run_id}/cancel",
                ),
                "skill-builder-delete": await client.delete(
                    f"{builder_base}/{builder_run_id}",
                ),
                "skill-builder-state": await client.get(
                    f"{builder_thread_base}/state",
                ),
                "skill-builder-upload": await client.post(
                    f"{builder_thread_base}/uploads",
                    files={"file": ("reference.txt", b"reference", "text/plain")},
                ),
                "skill-builder-upload-limits": await client.get(
                    f"{builder_thread_base}/uploads/limits",
                ),
                "skill-builder-upload-list": await client.get(
                    f"{builder_thread_base}/uploads",
                ),
                "skill-builder-upload-delete": await client.delete(
                    f"{builder_thread_base}/uploads?file_id={file_id}",
                ),
                "skill-builder-file-download": await client.get(
                    f"{builder_thread_base}/files/{file_id}",
                ),
                "skill-builder-artifact-download": await client.get(
                    f"/api/projects/{project_id}/private-work/artifacts/{artifact_id}?thread_id={builder_thread_id}",
                ),
                "chat-delete": await client.delete(
                    f"{chat_base}/{chat_run_id}",
                ),
            }

        assert {name: response.status_code for name, response in responses.items()} == {
            "chat-create": 200,
            "skill-builder-create": 404,
            "chat-stream": 200,
            "skill-builder-stream": 404,
            "chat-wait": 200,
            "skill-builder-wait": 404,
            # This terminal fixture has no Job, so ordinary chat cancellation
            # reaches the domain guard and returns conflict rather than 404.
            "chat-cancel": 409,
            "skill-builder-cancel": 404,
            "skill-builder-delete": 404,
            "skill-builder-state": 404,
            "skill-builder-upload": 404,
            "skill-builder-upload-limits": 404,
            "skill-builder-upload-list": 404,
            "skill-builder-upload-delete": 404,
            "skill-builder-file-download": 404,
            "skill-builder-artifact-download": 404,
            "chat-delete": 200,
        }
        assert launched_thread_ids == [
            chat_thread_id,
            chat_thread_id,
            chat_thread_id,
        ]
        builder_run = await app.state.private_run_service.get(
            seed.owner_a,
            builder_thread_id,
            builder_run_id,
        )
        assert builder_run.run_id == builder_run_id
    finally:
        await seed.engine.dispose()
