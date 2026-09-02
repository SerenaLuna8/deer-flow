from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from langchain_core.messages import AIMessage

from app.gateway.private_work_schemas import PrivateRunCreateRequest
from app.gateway.routers.private_work_routes import runs, threads
from deerflow.runtime import DisconnectMode


def _serialized_message(
    message_type: str,
    *,
    content: object,
    run_id: str | None = None,
    token_canary: int | None = None,
) -> dict[str, object]:
    additional_kwargs: dict[str, object] = {
        "usage": "business instructions",
    }
    if run_id is not None:
        additional_kwargs["run_id"] = run_id
    if token_canary is not None:
        additional_kwargs["token_usage_attribution"] = {
            "exact_tokens": token_canary,
        }
    return {
        "type": message_type,
        "content": content,
        "additional_kwargs": additional_kwargs,
        "response_metadata": (
            {
                "usage": {"input_tokens": token_canary},
                "provider": {
                    "usage_metadata": {"total_tokens": token_canary},
                },
            }
            if token_canary is not None
            else {}
        ),
        "usage_metadata": (
            {
                "input_tokens": token_canary,
                "output_tokens": token_canary,
                "total_tokens": token_canary * 2,
            }
            if token_canary is not None
            else None
        ),
        "tool_calls": [
            {
                "name": "lookup",
                "args": {"usage": "tool-call instructions"},
                "id": f"call-{token_canary}",
                "type": "tool_call",
            }
        ],
    }


class _FrozenRuntimePolicyMaterializer:
    def __init__(self, policies: dict[str, bool | Exception]) -> None:
        self._policies = policies
        self.calls: list[dict[str, object]] = []
        self.batch_calls: list[dict[str, object]] = []
        self.thread_batch_calls: list[dict[str, object]] = []

    async def materialize_run_snapshot_envelope(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        run_id = kwargs["run_id"]
        assert isinstance(run_id, str)
        result = self._policies[run_id]
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(
            value=SimpleNamespace(
                token_usage=SimpleNamespace(enabled=result),
            )
        )

    async def materialize_run_snapshot_envelopes(
        self,
        **kwargs: object,
    ) -> dict[str, object]:
        self.batch_calls.append(dict(kwargs))
        run_ids = kwargs["run_ids"]
        assert isinstance(run_ids, (list, tuple, set, frozenset))
        materialized: dict[str, object] = {}
        for run_id in run_ids:
            assert isinstance(run_id, str)
            result = self._policies.get(run_id)
            if isinstance(result, Exception) or result is None:
                continue
            materialized[run_id] = SimpleNamespace(
                value=SimpleNamespace(
                    token_usage=SimpleNamespace(enabled=result),
                )
            )
        return materialized

    async def materialize_thread_run_snapshot_envelopes(
        self,
        **kwargs: object,
    ) -> dict[str, object]:
        self.thread_batch_calls.append(dict(kwargs))
        materialized: dict[str, object] = {}
        for run_id, result in self._policies.items():
            if isinstance(result, Exception):
                continue
            materialized[run_id] = SimpleNamespace(
                value=SimpleNamespace(
                    token_usage=SimpleNamespace(enabled=result),
                )
            )
        return materialized


@pytest.mark.asyncio
async def test_run_messages_hide_usage_for_disabled_frozen_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    run_id = uuid.uuid4()
    materializer = _FrozenRuntimePolicyMaterializer({str(run_id): False})
    app = FastAPI()
    app.state.system_runtime_policy_materializer = materializer
    request = _request(app)
    record = {
        "run_id": str(run_id),
        "seq": 7,
        "content": _serialized_message(
            "ai",
            content={"usage": "business response instructions"},
            token_canary=41,
        ),
        "metadata": {
            "usage": {"total_tokens": 82},
            "usage_completeness": "final_observed",
            "business": {"usage": "business metadata instructions"},
        },
        "created_at": "2026-08-23T00:00:00+00:00",
    }

    class _RunService:
        async def get(self, *_args: object) -> object:
            return SimpleNamespace(status="success", kwargs={})

    class _EventStore:
        async def list_messages_by_run(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return [copy.deepcopy(record)]

    async def browser_service(*_args: object) -> object:
        return _RunService()

    async def durations(
        _request: object,
        _context: object,
        _thread_id: str,
        records: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        return records

    monkeypatch.setattr(
        runs,
        "_browser_chat_run_service",
        browser_service,
    )
    monkeypatch.setattr(
        runs,
        "_run_event_store",
        lambda *_args: _EventStore(),
    )
    monkeypatch.setattr(
        runs,
        "_project_scoped_event_durations",
        durations,
    )

    response = await runs.list_private_run_messages(
        thread_id,
        run_id,
        request,
        limit=50,
        before_seq=None,
        after_seq=None,
        context=SimpleNamespace(
            request_id="run-message-token-projection",
            project_id=project_id,
            user_id=user_id,
            resource_scope=object(),
        ),  # type: ignore[arg-type]
    )

    message = response.data[0]
    assert message.content["content"]["usage"] == "business response instructions"
    assert "usage_metadata" not in message.content
    assert "usage" not in message.content["response_metadata"]
    assert "token_usage_attribution" not in message.content["additional_kwargs"]
    assert "usage" not in message.metadata
    assert "usage_completeness" not in message.metadata
    assert message.metadata["business"]["usage"] == "business metadata instructions"


@pytest.mark.asyncio
async def test_thread_messages_batch_project_each_runs_frozen_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    disabled_run = "run-disabled"
    enabled_run = "run-enabled"
    stale_run = "run-stale"
    materializer = _FrozenRuntimePolicyMaterializer(
        {
            disabled_run: False,
            enabled_run: True,
            stale_run: RuntimeError("legacy snapshot unavailable"),
        }
    )
    app = FastAPI()
    app.state.system_runtime_policy_materializer = materializer
    request = _request(app)

    def event(run_id: str | None, canary: int) -> dict[str, object]:
        record: dict[str, object] = {
            "seq": canary,
            "content": _serialized_message(
                "ai",
                content={"usage": f"business-{canary}"},
                token_canary=canary,
            ),
            "metadata": {"usage": {"total_tokens": canary}},
            "created_at": "2026-08-23T00:00:00+00:00",
        }
        if run_id is not None:
            record["run_id"] = run_id
        return record

    records = [
        event(disabled_run, 11),
        event(enabled_run, 22),
        event(stale_run, 33),
        event(None, 44),
    ]

    class _RunService:
        async def list(self, *_args: object, **_kwargs: object) -> tuple[()]:
            return ()

    class _EventStore:
        async def list_messages(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return copy.deepcopy(records)

    async def browser_service(*_args: object) -> object:
        return _RunService()

    async def durations(
        _request: object,
        _context: object,
        _thread_id: str,
        values: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        return values

    monkeypatch.setattr(
        runs,
        "_browser_chat_run_service",
        browser_service,
    )
    monkeypatch.setattr(
        runs,
        "_run_event_store",
        lambda *_args: _EventStore(),
    )
    monkeypatch.setattr(
        runs,
        "_project_scoped_event_durations",
        durations,
    )

    response = await runs.list_private_thread_messages(
        thread_id,
        request,
        limit=50,
        before_seq=None,
        after_seq=None,
        context=SimpleNamespace(
            request_id="thread-message-token-projection",
            project_id=project_id,
            user_id=user_id,
            resource_scope=object(),
        ),  # type: ignore[arg-type]
    )

    for index in (0, 2, 3):
        assert "usage_metadata" not in response[index]["content"]
        assert "usage" not in response[index]["metadata"]
        assert response[index]["content"]["content"]["usage"] == (f"business-{(index + 1) * 11}")
    assert response[1]["content"]["usage_metadata"]["total_tokens"] == 44
    assert response[1]["metadata"]["usage"] == {"total_tokens": 22}
    assert materializer.calls == []
    assert len(materializer.batch_calls) == 1
    assert set(materializer.batch_calls[0]["run_ids"]) == {
        disabled_run,
        enabled_run,
        stale_run,
    }


@pytest.mark.asyncio
async def test_run_events_hide_only_recognized_usage_for_disabled_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    run_id = uuid.uuid4()
    materializer = _FrozenRuntimePolicyMaterializer({str(run_id): False})
    app = FastAPI()
    app.state.system_runtime_policy_materializer = materializer
    request = _request(app)
    records = [
        {
            "run_id": str(run_id),
            "seq": 1,
            "event_type": "subagent.end",
            "category": "subagent",
            "content": {
                "task_id": "task-1",
                "status": "completed",
                "usage": {"total_tokens": 73},
                "usage_completeness": "final_observed",
                "result": {"usage": "business result instructions"},
            },
            "metadata": {
                "task_id": "task-1",
                "usage": {"total_tokens": 73},
            },
            "created_at": "2026-08-23T00:00:00+00:00",
        },
        {
            "run_id": str(run_id),
            "seq": 2,
            "event_type": "business.metric",
            "category": "trace",
            "content": {"usage": "business event instructions"},
            "metadata": {"usage": {"total_tokens": 74}},
            "created_at": "2026-08-23T00:00:01+00:00",
        },
        {
            "run_id": str(run_id),
            "seq": 3,
            "event_type": "llm.ai.response",
            "category": "message",
            "content": _serialized_message(
                "ai",
                content="provider response",
                token_canary=75,
            ),
            "metadata": {"usage_completeness": "final_observed"},
            "created_at": "2026-08-23T00:00:02+00:00",
        },
    ]

    class _RunService:
        async def get(self, *_args: object) -> object:
            return object()

    class _EventStore:
        async def list_events(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return copy.deepcopy(records)

    async def browser_service(*_args: object) -> object:
        return _RunService()

    monkeypatch.setattr(
        runs,
        "_browser_chat_run_service",
        browser_service,
    )
    monkeypatch.setattr(
        runs,
        "_run_event_store",
        lambda *_args: _EventStore(),
    )

    response = await runs.list_private_run_events(
        thread_id,
        run_id,
        request,
        event_types=None,
        task_id=None,
        limit=500,
        after_seq=None,
        context=SimpleNamespace(
            request_id="run-event-token-projection",
            project_id=project_id,
            user_id=user_id,
            resource_scope=object(),
        ),  # type: ignore[arg-type]
    )

    assert "usage" not in response[0]["content"]
    assert "usage_completeness" not in response[0]["content"]
    assert response[0]["content"]["result"]["usage"] == ("business result instructions")
    assert "usage" not in response[0]["metadata"]
    assert response[1]["content"]["usage"] == "business event instructions"
    assert "usage" not in response[1]["metadata"]
    assert "usage_metadata" not in response[2]["content"]
    assert "usage_completeness" not in response[2]["metadata"]


@pytest.mark.asyncio
async def test_thread_token_usage_counts_only_verified_tracking_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    materializer = _FrozenRuntimePolicyMaterializer(
        {
            "run-enabled": True,
            "run-disabled": False,
            "run-stale": RuntimeError("legacy policy is unverifiable"),
        }
    )
    app = FastAPI()
    app.state.system_runtime_policy_materializer = materializer
    request = _request(app)
    aggregate_calls: list[dict[str, object]] = []

    class _RunService:
        async def list(self, *_args: object, **_kwargs: object) -> tuple[()]:
            return ()

    class _RunStore:
        async def aggregate_tokens_by_thread(
            self,
            _thread_id: str,
            **kwargs: object,
        ) -> dict[str, object]:
            aggregate_calls.append(dict(kwargs))
            if kwargs.get("included_run_ids") == frozenset({"run-enabled"}):
                return {
                    "total_tokens": 12,
                    "total_input_tokens": 5,
                    "total_output_tokens": 7,
                    "total_runs": 1,
                    "by_model": {"model-a": {"tokens": 12, "runs": 1}},
                    "by_caller": {
                        "lead_agent": 12,
                        "subagent": 0,
                        "middleware": 0,
                    },
                }
            return {
                "total_tokens": 999,
                "total_input_tokens": 500,
                "total_output_tokens": 499,
                "total_runs": 4,
                "by_model": {"legacy": {"tokens": 999, "runs": 4}},
                "by_caller": {
                    "lead_agent": 999,
                    "subagent": 0,
                    "middleware": 0,
                },
            }

    async def browser_service(*_args: object) -> object:
        return _RunService()

    monkeypatch.setattr(
        runs,
        "_browser_chat_run_service",
        browser_service,
    )
    monkeypatch.setattr(
        runs,
        "_run_store",
        lambda *_args: _RunStore(),
    )

    response = await runs.private_thread_token_usage(
        thread_id,
        request,
        include_active=False,
        context=SimpleNamespace(
            request_id="thread-token-usage-policy",
            project_id=project_id,
            user_id=user_id,
            resource_scope=object(),
        ),  # type: ignore[arg-type]
    )

    assert response.total_tokens == 12
    assert response.total_runs == 1
    assert response.by_model["model-a"].tokens == 12
    assert len(materializer.thread_batch_calls) == 1
    assert materializer.thread_batch_calls[0]["thread_id"] == str(thread_id)
    assert aggregate_calls[0]["included_run_ids"] == frozenset({"run-enabled"})


@pytest.mark.asyncio
async def test_checkpoint_projection_uses_each_turns_frozen_tracking_policy_and_fails_closed() -> None:
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    disabled_run = "run-disabled"
    enabled_run = "run-enabled"
    stale_run = "run-stale"
    materializer = _FrozenRuntimePolicyMaterializer(
        {
            disabled_run: False,
            enabled_run: True,
            stale_run: RuntimeError("snapshot unavailable"),
        }
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                system_runtime_policy_materializer=materializer,
            )
        )
    )
    values = {
        "messages": [
            _serialized_message("ai", content="unknown prefix", token_canary=101),
            _serialized_message("human", content="disabled", run_id=disabled_run),
            _serialized_message(
                "ai",
                content=[{"type": "text", "text": "disabled", "usage": "content instructions"}],
                token_canary=202,
            ),
            _serialized_message(
                "tool",
                content={"usage": "tool result instructions"},
                token_canary=203,
            ),
            _serialized_message("human", content="enabled", run_id=enabled_run),
            _serialized_message("ai", content="enabled", token_canary=303),
            _serialized_message("human", content="stale", run_id=stale_run),
            _serialized_message("ai", content="stale", token_canary=404),
            _serialized_message("human", content="missing provenance"),
            _serialized_message("ai", content="unknown", token_canary=505),
        ],
        "business_state": {"usage": "business-state instructions"},
    }
    original = copy.deepcopy(values)

    projected = await runs._project_scoped_checkpoint_token_usage(
        request,  # type: ignore[arg-type]
        SimpleNamespace(project_id=project_id, user_id=user_id),  # type: ignore[arg-type]
        values,
    )

    messages = projected["messages"]
    assert isinstance(messages, list)
    for index in (0, 2, 3, 7, 9):
        message = messages[index]
        assert "usage_metadata" not in message
        assert "usage" not in message["response_metadata"]
        assert "usage_metadata" not in message["response_metadata"].get(
            "provider",
            {},
        )
        assert "token_usage_attribution" not in message["additional_kwargs"]

    enabled_message = messages[5]
    assert enabled_message["usage_metadata"]["total_tokens"] == 606
    assert enabled_message["response_metadata"]["usage"] == {
        "input_tokens": 303,
    }
    assert enabled_message["additional_kwargs"]["token_usage_attribution"] == {
        "exact_tokens": 303,
    }

    assert messages[2]["content"][0]["usage"] == "content instructions"
    assert messages[2]["tool_calls"][0]["args"]["usage"] == ("tool-call instructions")
    assert messages[3]["content"]["usage"] == "tool result instructions"
    assert messages[3]["additional_kwargs"]["usage"] == "business instructions"
    assert projected["business_state"] == {
        "usage": "business-state instructions",
    }
    assert values == original
    assert materializer.calls == []
    assert len(materializer.batch_calls) == 1
    assert list(materializer.batch_calls[0]["run_ids"]) == [
        disabled_run,
        enabled_run,
        stale_run,
    ]
    assert materializer.batch_calls[0]["project_id"] == project_id
    assert materializer.batch_calls[0]["owner_user_id"] == str(user_id)


def _request(app: FastAPI) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
            "app": app,
        }
    )


@pytest.mark.asyncio
async def test_wait_and_state_routes_project_checkpoint_tokens_after_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    request = _request(app)
    context = SimpleNamespace(request_id="checkpoint-token-projection")
    thread_id = uuid.uuid4()
    snapshot = SimpleNamespace(
        values={
            "messages": [
                AIMessage(
                    content="provider response",
                    usage_metadata={
                        "input_tokens": 11,
                        "output_tokens": 12,
                        "total_tokens": 23,
                    },
                )
            ],
            "token_budget_usage": {"total_tokens": 98765},
        },
        metadata={"created_at": "2026-08-23T00:00:00+00:00"},
        config={"configurable": {"checkpoint_id": "checkpoint-1"}},
        parent_config=None,
        tasks=(),
    )
    accessor = SimpleNamespace(aget=AsyncMock(return_value=snapshot))
    projection_calls: list[dict[str, object]] = []

    async def browser_service(*_args: object) -> object:
        return object()

    async def normalize(body: object, **_kwargs: object) -> object:
        return body

    async def launch(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(
            run_id="run-wait",
            on_disconnect=DisconnectMode.cancel,
        )

    async def wait_for_run(**_kwargs: object) -> tuple[bool, object]:
        return True, SimpleNamespace(status="success", error=None)

    async def project(
        _request: object,
        _context: object,
        values: dict[str, object],
    ) -> dict[str, object]:
        assert isinstance(values["messages"], list)
        assert isinstance(values["messages"][0], dict)
        assert "token_budget_usage" not in values
        projection_calls.append(values)
        return {**values, "projection_applied": True}

    async def durations(
        _request: object,
        _context: object,
        _thread_id: str,
        values: dict[str, object],
    ) -> dict[str, object]:
        return values

    monkeypatch.setattr(
        runs,
        "_browser_chat_run_service",
        browser_service,
    )
    monkeypatch.setattr(runs, "_require_run_runtime", lambda *_args: None)
    monkeypatch.setattr(
        runs,
        "_normalize_prepared_edit_replay",
        normalize,
    )
    monkeypatch.setattr(runs, "start_private_run", launch)
    monkeypatch.setattr(
        runs,
        "_wait_for_durable_private_run",
        wait_for_run,
    )
    monkeypatch.setattr(
        runs,
        "_scoped_checkpointer",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        runs,
        "bind_scoped_checkpoint_state",
        lambda *_args, **_kwargs: accessor,
    )
    monkeypatch.setattr(
        runs,
        "_project_scoped_checkpoint_token_usage",
        project,
        raising=False,
    )
    monkeypatch.setattr(
        runs,
        "_project_scoped_checkpoint_durations",
        durations,
    )

    monkeypatch.setattr(
        threads,
        "_browser_chat_run_service",
        browser_service,
    )
    monkeypatch.setattr(
        threads,
        "_scoped_checkpointer",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        threads,
        "bind_scoped_checkpoint_state",
        lambda *_args, **_kwargs: accessor,
    )
    monkeypatch.setattr(
        threads,
        "_project_scoped_checkpoint_token_usage",
        project,
    )
    monkeypatch.setattr(
        threads,
        "_project_scoped_checkpoint_durations",
        durations,
    )

    wait_values = await runs.wait_private_run(
        thread_id,
        PrivateRunCreateRequest(input={}),
        request,
        context,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    state = await threads.get_thread_state(
        thread_id,
        request,
        context,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    assert wait_values["projection_applied"] is True
    assert state.values["projection_applied"] is True
    assert len(projection_calls) == 2
