from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import httpx
import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.llm_error_handling_middleware import (
    RECOVERED_LLM_FAILURES_KEY,
    LLMErrorHandlingMiddleware,
)
from deerflow.agents.middlewares.tool_call_control import (
    TOOL_CALL_CONTROL_LOOP_REPLACEMENT_KEY,
    RepeatedCallObservation,
    ToolCallBudgetObservation,
    ToolCallControlObservation,
)
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.journal import RunJournal, RunJournalToolCallControlObserver
from deerflow.runtime.recovered_llm_failures import (
    RunRecoveredLLMFailureRecorder,
    build_recovered_llm_failures_receipt,
)
from deerflow.runtime.runs.execution_contracts import RunSemanticStopRecorder


class _RecordingEventStore:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def put_batch(self, events: list[dict], **_kwargs: object) -> None:
        self.events.extend(events)


def _budget_observation(
    *,
    observation_id: str = "a" * 64,
    role: str = "lead",
    scope_id: str = "run-budget",
) -> ToolCallControlObservation:
    return ToolCallBudgetObservation(
        reason_code="tool_budget_exhausted",
        role=role,
        scope_id=scope_id,
        workload_profile="research",
        tool_name="web_search",
        count_before=9,
        proposed=3,
        admitted=1,
        rejected=2,
        count_after=10,
        warn_threshold=6,
        hard_limit=10,
        disposition="truncate_tool_calls",
        observation_id=observation_id,
    )


def _repeated_observation(
    *,
    observation_id: str = "c" * 64,
) -> ToolCallControlObservation:
    return RepeatedCallObservation(
        reason_code="repeated_call_warning",
        role="lead",
        scope_id="run-budget",
        workload_profile="research",
        count_before=2,
        proposed=1,
        admitted=1,
        rejected=0,
        count_after=3,
        warn_threshold=3,
        hard_limit=5,
        disposition="advisory",
        observation_id=observation_id,
    )


@pytest.mark.anyio
async def test_tool_call_control_observation_is_safe_deduplicated_and_precedes_terminal() -> None:
    store = _RecordingEventStore()
    journal = RunJournal(
        "run-budget",
        "thread-budget",
        store,
        flush_threshold=100,
    )
    observation = _budget_observation()

    journal.record_tool_call_control_observation(observation)
    journal.record_tool_call_control_observation(observation)
    journal.on_chain_end(
        {"messages": [AIMessage(content="Finished from existing evidence")]},
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    await journal.flush()

    assert [event["event_type"] for event in store.events] == [
        "middleware:tool_call_budget",
        "run.end",
    ]
    payload = store.events[0]["content"]
    assert payload == {
        "schema_version": 1,
        "reason_code": "tool_budget_exhausted",
        "workload_profile": "research",
        "role": "lead",
        "run_id": "run-budget",
        "execution_id": None,
        "tool_name": "web_search",
        "count_before": 9,
        "proposed": 3,
        "admitted": 1,
        "rejected": 2,
        "count_after": 10,
        "warn_threshold": 6,
        "hard_limit": 10,
        "disposition": "truncate_tool_calls",
        "observation_id": "a" * 64,
    }
    serialized = str(payload)
    assert "scope_id" not in payload
    assert "query" not in serialized
    assert "url" not in serialized
    assert "args" not in serialized


@pytest.mark.anyio
async def test_repeated_call_and_tool_budget_use_distinct_observation_types_and_events() -> None:
    store = _RecordingEventStore()
    journal = RunJournal(
        "run-budget",
        "thread-budget",
        store,
        flush_threshold=100,
    )

    journal.record_tool_call_control_observation(_repeated_observation())
    journal.record_tool_call_control_observation(_budget_observation())
    await journal.flush()

    assert [event["event_type"] for event in store.events] == [
        "middleware:repeated_call",
        "middleware:tool_call_budget",
    ]
    assert "tool_name" not in store.events[0]["content"]
    assert store.events[1]["content"]["tool_name"] == "web_search"


@pytest.mark.anyio
async def test_tool_call_control_observer_marshals_subagent_observation_to_owner_loop() -> None:
    store = _RecordingEventStore()
    journal = RunJournal(
        "run-parent",
        "thread-parent",
        store,
        flush_threshold=100,
    )
    observer = RunJournalToolCallControlObserver(
        journal,
        owner_loop=asyncio.get_running_loop(),
    )

    await asyncio.to_thread(
        observer.observe,
        _budget_observation(
            observation_id="b" * 64,
            role="subagent",
            scope_id="private-internal-execution-id",
        ),
    )
    await asyncio.sleep(0)
    await journal.flush()

    assert len(store.events) == 1
    payload = store.events[0]["content"]
    assert payload["role"] == "subagent"
    assert payload["run_id"] == "run-parent"
    assert payload["execution_id"] != "private-internal-execution-id"
    assert len(payload["execution_id"]) == 32


def _observe_lead_ai_message(
    journal: RunJournal,
    message: AIMessage,
) -> None:
    journal.on_llm_end(
        SimpleNamespace(
            generations=[[SimpleNamespace(message=message)]],
        ),
        run_id=uuid.uuid4(),
    )


def _llm_error_middleware() -> LLMErrorHandlingMiddleware:
    middleware = LLMErrorHandlingMiddleware(
        app_config=SimpleNamespace(
            circuit_breaker=SimpleNamespace(
                failure_threshold=5,
                recovery_timeout_sec=60,
            )
        )
    )
    middleware.retry_base_delay_ms = 0
    return middleware


@pytest.mark.anyio
async def test_fatal_llm_fallback_cannot_journal_a_successful_run_end() -> None:
    store = _RecordingEventStore()
    journal = RunJournal(
        "run-fallback",
        "thread-fallback",
        store,
        flush_threshold=100,
    )
    journal.on_chain_end(
        {
            "messages": [
                AIMessage(
                    content="The image could not be read.",
                    additional_kwargs={
                        "deerflow_error_fallback": True,
                        "error_detail": "CURRENT_UPLOAD_UNAVAILABLE",
                    },
                )
            ]
        },
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    await journal.flush()

    terminal_events = [event for event in store.events if event["event_type"] == "run.end"]
    assert len(terminal_events) == 1
    assert terminal_events[0]["metadata"] == {"status": "error"}
    assert journal.had_llm_error_fallback is True
    assert journal.llm_error_fallback_message == "CURRENT_UPLOAD_UNAVAILABLE"
    assert journal.llm_error_fallback_code == "CURRENT_UPLOAD_UNAVAILABLE"


@pytest.mark.anyio
async def test_stale_fallback_cannot_poison_a_later_successful_run_end() -> None:
    store = _RecordingEventStore()
    journal = RunJournal(
        "run-success",
        "thread-success",
        store,
        flush_threshold=100,
    )
    journal.on_chain_end(
        {
            "messages": [
                AIMessage(
                    id="old-fallback",
                    content="The image could not be read.",
                    additional_kwargs={
                        "deerflow_error_fallback": True,
                        "error_detail": "CURRENT_UPLOAD_UNAVAILABLE",
                    },
                ),
                AIMessage(id="current-success", content="Current answer"),
            ]
        },
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    await journal.flush()

    terminal_events = [event for event in store.events if event["event_type"] == "run.end"]
    assert len(terminal_events) == 1
    assert terminal_events[0]["metadata"] == {"status": "success"}
    assert journal.had_llm_error_fallback is False
    assert journal.llm_error_fallback_message is None
    assert journal.llm_error_fallback_code is None


@pytest.mark.anyio
async def test_recovered_llm_failure_is_durable_and_contains_no_raw_error() -> None:
    store = _RecordingEventStore()
    recorder = RunRecoveredLLMFailureRecorder()
    journal = RunJournal(
        "run-recovered",
        "thread-recovered",
        store,
        flush_threshold=100,
        recovered_llm_failure_recorder=recorder,
    )
    message = AIMessage(
        id="answer-1",
        content="Recovered answer",
        additional_kwargs={
            RECOVERED_LLM_FAILURES_KEY: {
                "schema_version": 1,
                "failures": [
                    {
                        "attempt": 1,
                        "max_attempts": 3,
                        "error_code": "LLM_PROVIDER_UNAVAILABLE",
                        "reason": "transient",
                        "disposition": "recovered",
                    }
                ],
            }
        },
    )

    journal.on_llm_error(
        RuntimeError("secret provider URL and response body"),
        run_id=uuid.uuid4(),
    )
    _observe_lead_ai_message(
        journal,
        message.model_copy(update={"additional_kwargs": {}}),
    )
    recorder.record(
        message.additional_kwargs[RECOVERED_LLM_FAILURES_KEY],
    )
    journal.on_chain_end(
        {"messages": [message]},
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    await journal.flush()

    serialized = repr(store.events)
    assert "secret provider URL" not in serialized
    recovered = [event for event in store.events if event["event_type"] == "run.recovered_issue"]
    assert len(recovered) == 1
    assert recovered[0]["content"] == {
        "kind": "llm_retry_recovered",
        "schema_version": 1,
        "failures": [
            {
                "attempt": 1,
                "max_attempts": 3,
                "error_code": "LLM_PROVIDER_UNAVAILABLE",
                "reason": "transient",
                "disposition": "recovered",
            }
        ],
    }
    reconciled = [event for event in store.events if event["event_type"] == "llm.ai.response" and event["metadata"].get("source") == "recovered_llm_failures"]
    assert len(reconciled) == 1
    assert reconciled[0]["content"]["id"] == "answer-1"
    assert reconciled[0]["content"]["additional_kwargs"][RECOVERED_LLM_FAILURES_KEY]["failures"][0]["error_code"] == "LLM_PROVIDER_UNAVAILABLE"
    llm_errors = [event for event in store.events if event["event_type"] == "llm.error"]
    assert llm_errors[0]["content"] == "LLM request failed"
    assert llm_errors[0]["metadata"] == {"exception_class": "RuntimeError"}


@pytest.mark.anyio
async def test_idless_recovered_responses_aggregate_without_duplicate_ai_events() -> None:
    store = _RecordingEventStore()
    recorder = RunRecoveredLLMFailureRecorder()
    journal = RunJournal(
        "run-multiple-recovered",
        "thread-multiple-recovered",
        store,
        flush_threshold=100,
        recovered_llm_failure_recorder=recorder,
    )
    messages = [
        AIMessage(
            content="First recovered answer",
            additional_kwargs={
                RECOVERED_LLM_FAILURES_KEY: {
                    "schema_version": 1,
                    "failures": [
                        {
                            "attempt": 1,
                            "max_attempts": 3,
                            "error_code": "LLM_PROVIDER_UNAVAILABLE",
                            "reason": "transient",
                            "disposition": "recovered",
                        }
                    ],
                }
            },
        ),
        AIMessage(
            content="Second recovered answer",
            additional_kwargs={
                RECOVERED_LLM_FAILURES_KEY: {
                    "schema_version": 1,
                    "failures": [
                        {
                            "attempt": 1,
                            "max_attempts": 3,
                            "error_code": "LLM_PROVIDER_BUSY",
                            "reason": "busy",
                            "disposition": "recovered",
                        }
                    ],
                }
            },
        ),
    ]
    for message in messages:
        recorder.record(
            message.additional_kwargs[RECOVERED_LLM_FAILURES_KEY],
        )

    journal.on_chain_end(
        {"messages": messages},
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    journal.on_chain_end(
        {"messages": messages},
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    await journal.flush()

    recovered = [event for event in store.events if event["event_type"] == "run.recovered_issue"]
    assert len(recovered) == 1
    assert recovered[0]["content"]["failures"] == [
        {
            "attempt": 1,
            "max_attempts": 3,
            "error_code": "LLM_PROVIDER_UNAVAILABLE",
            "reason": "transient",
            "disposition": "recovered",
        },
        {
            "attempt": 1,
            "max_attempts": 3,
            "error_code": "LLM_PROVIDER_BUSY",
            "reason": "busy",
            "disposition": "recovered",
        },
    ]
    assert not [event for event in store.events if event["event_type"] == "llm.ai.response" and event["metadata"].get("source") == "recovered_llm_failures"]


@pytest.mark.anyio
async def test_final_response_carries_run_aggregate_recovered_receipt() -> None:
    store = _RecordingEventStore()
    recorder = RunRecoveredLLMFailureRecorder()
    journal = RunJournal(
        "run-aggregate-recovered",
        "thread-aggregate-recovered",
        store,
        flush_threshold=100,
        recovered_llm_failure_recorder=recorder,
    )
    middleware = _llm_error_middleware()
    request = SimpleNamespace(
        runtime=Runtime(
            context={
                RuntimeContextKeys.RECOVERED_LLM_FAILURE_RECORDER: recorder,
            },
        )
    )
    provider_request = httpx.Request(
        "POST",
        "https://provider.invalid/v1/responses",
    )

    def recovered_call(content: str) -> AIMessage:
        attempts = 0

        def handler(_request) -> ModelResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError(
                    "connection secret detail",
                    request=provider_request,
                )
            return ModelResponse(result=[AIMessage(content=content)])

        response = middleware.wrap_model_call(request, handler)  # type: ignore[arg-type]
        assert isinstance(response, ModelResponse)
        message = response.result[0]
        assert isinstance(message, AIMessage)
        return message

    first = recovered_call("First recovered step")
    second = recovered_call("Second recovered step")
    final_response = middleware.wrap_model_call(
        request,  # type: ignore[arg-type]
        lambda _request: ModelResponse(
            result=[AIMessage(content="Final answer")],
        ),
    )
    assert isinstance(final_response, ModelResponse)
    final = final_response.result[0]
    assert isinstance(final, AIMessage)

    aggregate = final.additional_kwargs[RECOVERED_LLM_FAILURES_KEY]
    assert aggregate["schema_version"] == 1
    assert len(aggregate["failures"]) == 2

    journal.on_chain_end(
        {"messages": [first, second, final]},
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    await journal.flush()

    recovered = [event for event in store.events if event["event_type"] == "run.recovered_issue"]
    assert len(recovered) == 1
    assert recovered[0]["content"]["failures"] == aggregate["failures"]
    assert not [event for event in store.events if event["event_type"] == "llm.ai.response" and event["metadata"].get("source") == "recovered_llm_failures"]


@pytest.mark.anyio
async def test_continuation_persists_only_new_failures_and_each_final_aggregate() -> None:
    store = _RecordingEventStore()
    recorder = RunRecoveredLLMFailureRecorder()
    journal = RunJournal(
        "run-continuation-recovered",
        "thread-continuation-recovered",
        store,
        flush_threshold=100,
        recovered_llm_failure_recorder=recorder,
    )
    first_failure = {
        "attempt": 1,
        "max_attempts": 3,
        "error_code": "LLM_PROVIDER_UNAVAILABLE",
        "reason": "transient",
        "disposition": "recovered",
    }
    second_failure = {
        "attempt": 1,
        "max_attempts": 3,
        "error_code": "LLM_PROVIDER_BUSY",
        "reason": "busy",
        "disposition": "recovered",
    }

    shared_message_id = "provider-reused-final-id"
    _observe_lead_ai_message(
        journal,
        AIMessage(
            id=shared_message_id,
            content="First turn answer",
        ),
    )
    first_aggregate = recorder.record(
        build_recovered_llm_failures_receipt((first_failure,)),
    )
    first_final = AIMessage(
        id=shared_message_id,
        content="First turn answer",
        additional_kwargs={RECOVERED_LLM_FAILURES_KEY: (build_recovered_llm_failures_receipt(first_aggregate))},
    )
    journal.on_chain_end(
        {"messages": [first_final]},
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )

    _observe_lead_ai_message(
        journal,
        AIMessage(
            id=shared_message_id,
            content="Second turn answer",
        ),
    )
    second_aggregate = recorder.record(
        build_recovered_llm_failures_receipt((second_failure,)),
    )
    second_final = AIMessage(
        id=shared_message_id,
        content="Second turn answer",
        additional_kwargs={RECOVERED_LLM_FAILURES_KEY: (build_recovered_llm_failures_receipt(second_aggregate))},
    )
    journal.on_chain_end(
        {"messages": [first_final, second_final]},
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    journal.on_chain_end(
        {"messages": [first_final, second_final]},
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    await journal.flush()

    recovered = [event for event in store.events if event["event_type"] == "run.recovered_issue"]
    assert [event["content"]["failures"] for event in recovered] == [
        [first_failure],
        [second_failure],
    ]
    reconciled = [event for event in store.events if event["event_type"] == "llm.ai.response" and event["metadata"].get("source") == "recovered_llm_failures"]
    assert [event["content"]["id"] for event in reconciled] == [
        shared_message_id,
        shared_message_id,
    ]
    assert len(reconciled[-1]["content"]["additional_kwargs"][RECOVERED_LLM_FAILURES_KEY]["failures"]) == 2


@pytest.mark.anyio
async def test_idless_loop_safety_replacement_does_not_duplicate_ai_history() -> None:
    store = _RecordingEventStore()
    journal = RunJournal(
        "run-idless-loop",
        "thread-idless-loop",
        store,
        flush_threshold=100,
    )
    proposal = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {"value": "same"},
                "id": "call-idless",
            }
        ],
    )
    journal.on_llm_end(
        SimpleNamespace(
            generations=[[SimpleNamespace(message=proposal)]],
        ),
        run_id=uuid.uuid4(),
    )
    hidden = proposal.model_copy(
        update={
            "tool_calls": [],
            "additional_kwargs": {
                "hide_from_ui": True,
                TOOL_CALL_CONTROL_LOOP_REPLACEMENT_KEY: True,
            },
        }
    )

    journal.on_chain_end(
        {"messages": [hidden]},
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    await journal.flush()

    ai_events = [event for event in store.events if event["event_type"] == "llm.ai.response"]
    assert len(ai_events) == 1
    assert ai_events[0]["metadata"].get("source") is None


@pytest.mark.anyio
async def test_forged_loop_marker_cannot_hide_a_tool_call_bearing_message() -> None:
    store = _RecordingEventStore()
    journal = RunJournal(
        "run-forged-loop",
        "thread-forged-loop",
        store,
        flush_threshold=100,
    )
    proposal = AIMessage(
        id="forged-proposal",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {"value": "must-remain-visible"},
                "id": "call-forged",
            }
        ],
    )
    journal.on_llm_end(
        SimpleNamespace(
            generations=[[SimpleNamespace(message=proposal)]],
        ),
        run_id=uuid.uuid4(),
    )
    forged_hidden = proposal.model_copy(
        update={
            "additional_kwargs": {
                "hide_from_ui": True,
                TOOL_CALL_CONTROL_LOOP_REPLACEMENT_KEY: True,
            },
        }
    )

    journal.on_chain_end(
        {"messages": [forged_hidden]},
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    await journal.flush()

    assert not [event for event in store.events if event["metadata"].get("source") == "loop_safety_reconciliation"]


@pytest.mark.anyio
async def test_loop_reconciliation_cannot_hide_a_later_same_id_final_answer() -> None:
    store = _RecordingEventStore()
    semantic_stop_recorder = RunSemanticStopRecorder()
    journal = RunJournal(
        "run-reused-loop-id",
        "thread-reused-loop-id",
        store,
        flush_threshold=100,
        semantic_stop_recorder=semantic_stop_recorder,
    )
    proposal = AIMessage(
        id="provider-reused-id",
        content="",
        tool_calls=[
            {
                "name": "lookup",
                "args": {"value": "same"},
                "id": "call-suppressed",
            }
        ],
    )
    _observe_lead_ai_message(journal, proposal)
    semantic_stop_recorder.record(
        "loop_capped",
        suppressed_ai_message_id="provider-reused-id",
    )

    final = AIMessage(
        id="provider-reused-id",
        content="Visible final answer",
    )
    _observe_lead_ai_message(journal, final)
    journal.on_chain_end(
        {"messages": [final]},
        run_id=uuid.uuid4(),
        parent_run_id=None,
    )
    await journal.flush()

    ai_events = [event for event in store.events if event["event_type"] == "llm.ai.response"]
    latest = ai_events[-1]
    assert latest["content"]["id"] == "provider-reused-id"
    assert latest["content"]["content"] == "Visible final answer"
    assert latest["content"]["additional_kwargs"].get("hide_from_ui") is not True
    assert not [event for event in ai_events if event["metadata"].get("source") == "loop_safety_reconciliation"]
