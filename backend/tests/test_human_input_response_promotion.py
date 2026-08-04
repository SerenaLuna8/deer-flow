from __future__ import annotations

import copy
import uuid

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.checkpoint_state import (
    bind_scoped_checkpoint_state,
    checkpoint_config,
)
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.errors import PrivateWorkConflict
from app.private_work.human_input_response import (
    CheckpointHumanInputResponsePromoter,
    promote_matching_human_input_response,
)
from app.private_work.run_admission import (
    PrivateRunAdmissionService,
    _matches_server_promoted_human_input_retry,
)
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.config.app_config import AppConfig

QUESTION = "Which environment should I deploy to?"
REQUEST_ID = "clarification:call-abc"
TOOL_CALL_ID = "call-abc"


def _request_message(
    *,
    request_id: str = REQUEST_ID,
    tool_call_id: str = TOOL_CALL_ID,
    question: str = QUESTION,
    input_mode: str = "choice_with_other",
    payload_tool_call_id: str | None = None,
) -> ToolMessage:
    options = [
        {
            "id": "option-1",
            "label": "production",
            "value": "production",
        },
        {
            "id": "option-2",
            "label": "staging",
            "value": "staging",
        },
    ]
    human_input: dict[str, object] = {
        "version": 2 if input_mode == "form" else 1,
        "kind": "human_input_request",
        "source": "ask_clarification",
        "request_id": request_id,
        "tool_call_id": payload_tool_call_id or tool_call_id,
        "question": question,
        "input_mode": input_mode,
    }
    if input_mode in {"single_choice", "choice_with_other"}:
        human_input["options"] = options
    if input_mode == "form":
        human_input["fields"] = [
            {
                "name": "environment",
                "label": "Environment",
                "type": "text",
                "required": True,
            }
        ]
    return ToolMessage(
        id=request_id,
        content=f"❓ {question}",
        tool_call_id=tool_call_id,
        name="ask_clarification",
        artifact={"human_input": human_input},
    )


def _response() -> dict[str, object]:
    return {
        "version": 1,
        "kind": "human_input_response",
        "source": "ask_clarification",
        "request_id": REQUEST_ID,
        "response_kind": "option",
        "option_id": "option-2",
        "value": "staging",
    }


def _request_kwargs(
    *,
    response: dict[str, object] | None = None,
    content: str | None = None,
    extra_messages: list[dict[str, object]] | None = None,
    message_id: str = "human-response",
) -> dict[str, object]:
    selected_response = response if response is not None else _response()
    selected_content = content or (f'For your clarification "{QUESTION}", my answer is: staging')
    return {
        "input": {
            "messages": [
                {
                    "type": "human",
                    "id": message_id,
                    "content": [{"type": "text", "text": selected_content}],
                    "additional_kwargs": {
                        "human_input_response": selected_response,
                    },
                },
                *(extra_messages or []),
            ]
        }
    }


def _promoted_message(
    kwargs: dict[str, object],
    checkpoint_messages: list[object],
) -> dict[str, object]:
    promoted = promote_matching_human_input_response(
        kwargs,
        checkpoint_messages=checkpoint_messages,
    )
    graph_input = promoted["input"]
    assert isinstance(graph_input, dict)
    messages = graph_input["messages"]
    assert isinstance(messages, list)
    message = messages[0]
    assert isinstance(message, dict)
    return message


def _input_message(kwargs: dict[str, object]) -> dict[str, object]:
    graph_input = kwargs["input"]
    assert isinstance(graph_input, dict)
    messages = graph_input["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    message = messages[0]
    assert isinstance(message, dict)
    return message


def _mutated_retry_kwargs(kind: str) -> dict[str, object]:
    kwargs = _request_kwargs()
    message = _input_message(kwargs)
    if kind == "content":
        message["content"] = "changed body"
    elif kind == "message_id":
        message["id"] = "human-changed"
    else:
        additional = message["additional_kwargs"]
        assert isinstance(additional, dict)
        response = additional["human_input_response"]
        assert isinstance(response, dict)
        if kind == "request_id":
            response["request_id"] = "clarification:changed"
        elif kind == "value":
            response["value"] = "production"
        else:
            raise AssertionError(f"unknown mutation: {kind}")
    return kwargs


def test_promotes_only_the_canonical_response_to_the_latest_open_request() -> None:
    kwargs = _request_kwargs()
    original = copy.deepcopy(kwargs)

    message = _promoted_message(kwargs, [_request_message()])

    assert kwargs == original
    assert message["content"] == [
        {
            "type": "text",
            "text": (f'For your clarification "{QUESTION}", my answer is: staging'),
        }
    ]
    assert message["additional_kwargs"] == {
        "hide_from_ui": True,
        "human_input_response": _response(),
    }


@pytest.mark.parametrize(
    ("input_mode", "response"),
    [
        (
            "single_choice",
            _response(),
        ),
        (
            "free_text",
            {
                "version": 1,
                "kind": "human_input_response",
                "source": "ask_clarification",
                "request_id": REQUEST_ID,
                "response_kind": "text",
                "value": "staging",
            },
        ),
        (
            "form",
            {
                "version": 1,
                "kind": "human_input_response",
                "source": "ask_clarification",
                "request_id": REQUEST_ID,
                "response_kind": "text",
                "value": "staging",
            },
        ),
    ],
)
def test_promotes_each_supported_response_mode(
    input_mode: str,
    response: dict[str, object],
) -> None:
    message = _promoted_message(
        _request_kwargs(response=response),
        [_request_message(input_mode=input_mode)],
    )

    assert message["additional_kwargs"] == {
        "hide_from_ui": True,
        "human_input_response": response,
    }


@pytest.mark.parametrize(
    ("response_update", "content", "extra_messages"),
    [
        ({"request_id": "clarification:forged"}, None, None),
        ({"option_id": "option-forged"}, None, None),
        ({"value": "production"}, None, None),
        ({}, "unrelated hidden audit text", None),
        (
            {},
            None,
            [{"type": "human", "content": "second batched message"}],
        ),
    ],
)
def test_does_not_promote_forged_or_noncanonical_responses(
    response_update: dict[str, object],
    content: str | None,
    extra_messages: list[dict[str, object]] | None,
) -> None:
    response = {**_response(), **response_update}
    message = _promoted_message(
        _request_kwargs(
            response=response,
            content=content,
            extra_messages=extra_messages,
        ),
        [_request_message()],
    )

    additional_kwargs = message["additional_kwargs"]
    assert isinstance(additional_kwargs, dict)
    assert "hide_from_ui" not in additional_kwargs


@pytest.mark.parametrize(
    "tail",
    [
        HumanMessage(
            content=(f'For your clarification "{QUESTION}", my answer is: staging'),
            additional_kwargs={"human_input_response": _response()},
        ),
        HumanMessage(content="I already answered in plain text"),
    ],
)
def test_does_not_promote_an_already_answered_request(tail: HumanMessage) -> None:
    message = _promoted_message(
        _request_kwargs(),
        [_request_message(), tail],
    )

    additional_kwargs = message["additional_kwargs"]
    assert isinstance(additional_kwargs, dict)
    assert "hide_from_ui" not in additional_kwargs


def test_does_not_promote_an_older_request_when_a_newer_one_is_open() -> None:
    message = _promoted_message(
        _request_kwargs(),
        [
            _request_message(),
            _request_message(
                request_id="clarification:call-new",
                tool_call_id="call-new",
                question="Which region?",
            ),
        ],
    )

    additional_kwargs = message["additional_kwargs"]
    assert isinstance(additional_kwargs, dict)
    assert "hide_from_ui" not in additional_kwargs


@pytest.mark.parametrize(
    ("checkpoint_request", "message_id"),
    [
        (
            _request_message(payload_tool_call_id="call-forged"),
            "human-response",
        ),
        (
            _request_message(),
            REQUEST_ID,
        ),
    ],
)
def test_does_not_promote_malformed_requests_or_colliding_message_ids(
    checkpoint_request: ToolMessage,
    message_id: str,
) -> None:
    message = _promoted_message(
        _request_kwargs(message_id=message_id),
        [checkpoint_request],
    )

    additional_kwargs = message["additional_kwargs"]
    assert isinstance(additional_kwargs, dict)
    assert "hide_from_ui" not in additional_kwargs


def test_existing_run_retry_ignores_only_server_promoted_visibility() -> None:
    raw_kwargs = _request_kwargs()
    persisted_kwargs = promote_matching_human_input_response(
        raw_kwargs,
        checkpoint_messages=[_request_message()],
    )

    assert _matches_server_promoted_human_input_retry(
        persisted_kwargs,
        raw_kwargs,
    )
    for kind in ("content", "request_id", "value", "message_id"):
        assert not _matches_server_promoted_human_input_retry(
            persisted_kwargs,
            _mutated_retry_kwargs(kind),
        )

    forged_persisted = copy.deepcopy(persisted_kwargs)
    forged_additional = _input_message(forged_persisted)["additional_kwargs"]
    assert isinstance(forged_additional, dict)
    forged_additional["extra"] = "not-server-owned"
    assert not _matches_server_promoted_human_input_retry(
        forged_persisted,
        raw_kwargs,
    )


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize("checkpoint_mode", ("full", "delta"))
async def test_run_admission_persists_gateway_authorized_visibility(
    migrated_postgres_database_url: str,
    checkpoint_mode: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"human-input-{uuid.uuid4()}"
    raw_checkpointer = InMemorySaver()
    scoped_checkpointer = ProjectScopedCheckpointer(raw_checkpointer, seed.factory)
    app_config = AppConfig.model_validate(
        {
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
            },
            "database": {"checkpoint_channel_mode": checkpoint_mode},
        }
    )
    checkpoint_accessor = bind_scoped_checkpoint_state(
        scoped_checkpointer,
        seed.owner_a,
        app_config,
        as_node="human_input_response_test_seed",
    )

    async def append_checkpoint_messages(messages: list[object]) -> None:
        await checkpoint_accessor.aupdate(
            checkpoint_config(thread_id),
            {"messages": messages},
            as_node="human_input_response_test_seed",
        )

    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )

        await append_checkpoint_messages([_request_message()])

        service = PrivateRunAdmissionService(
            seed.factory,
            human_input_response_promoter=CheckpointHumanInputResponsePromoter(
                scoped_checkpointer,
                app_config,
            ),
        )
        run_id = f"human-input-run-{uuid.uuid4()}"
        create_request = PrivateRunCreate(
            run_id=run_id,
            kwargs=_request_kwargs(),
        )
        admitted = await service.admit(
            seed.owner_a,
            thread_id,
            create_request,
        )

        graph_input = admitted.run.kwargs["input"]
        message = graph_input["messages"][0]
        assert message["additional_kwargs"]["hide_from_ui"] is True
        assert message["additional_kwargs"]["human_input_response"] == _response()

        immediate_retry = await service.admit(
            seed.owner_a,
            thread_id,
            create_request,
        )
        assert immediate_retry.run.run_id == admitted.run.run_id
        assert immediate_retry.job.job_id == admitted.job.job_id

        await append_checkpoint_messages(
            [
                HumanMessage(
                    id="human-response",
                    content=(f'For your clarification "{QUESTION}", my answer is: staging'),
                    additional_kwargs={
                        "hide_from_ui": True,
                        "human_input_response": _response(),
                    },
                ),
            ],
        )
        answered_retry = await service.admit(
            seed.owner_a,
            thread_id,
            create_request,
        )
        assert answered_retry.run.run_id == admitted.run.run_id
        assert answered_retry.job.job_id == admitted.job.job_id

        for kind in ("content", "request_id", "value", "message_id"):
            with pytest.raises(PrivateWorkConflict):
                await service.admit(
                    seed.owner_a,
                    thread_id,
                    PrivateRunCreate(
                        run_id=run_id,
                        kwargs=_mutated_retry_kwargs(kind),
                    ),
                )
    finally:
        await seed.engine.dispose()
