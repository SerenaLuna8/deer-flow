from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from langchain_core.messages import HumanMessage, RemoveMessage

from app.gateway.private_work_schemas import PrivateRunCreateRequest
from app.gateway.routers.private_work import _normalize_prepared_edit_replay
from app.private_work.chat_controls import ProjectChatControlService
from app.private_work.errors import PrivateWorkConflict
from app.reliability.run_execution.executor import RunAgentPrivateExecutor


def test_replay_reuses_pre_injection_message_identity_from_checkpoint() -> None:
    base = SimpleNamespace(
        values={
            "messages": [
                HumanMessage(
                    content="fresh version check",
                    id="turn-1",
                )
            ]
        }
    )

    assert (
        ProjectChatControlService._replay_input_message_id(
            base,
            "turn-1__user",
        )
        == "turn-1"
    )


def test_replay_preserves_unwrapped_message_identity() -> None:
    base = SimpleNamespace(values={"messages": []})

    assert (
        ProjectChatControlService._replay_input_message_id(
            base,
            "turn-2",
        )
        == "turn-2"
    )


def test_edit_replay_removes_pre_injection_input_and_uses_new_visible_id() -> None:
    base = SimpleNamespace(
        values={
            "messages": [
                HumanMessage(
                    content="old text",
                    id="turn-3",
                )
            ]
        }
    )
    source = HumanMessage(content="old text", id="turn-3__user")

    messages, visible_id = ProjectChatControlService._edit_replay_message_plan(
        base,
        source,
        replacement_text="new text",
        replacement_base_id="edit-1",
    )

    assert messages[0] == {"type": "remove", "id": "turn-3"}
    assert messages[1]["id"] == "edit-1"
    assert messages[1]["content"] == [{"type": "text", "text": "new text"}]
    assert visible_id == "edit-1__user"


@pytest.mark.anyio
async def test_run_ingress_restores_server_validated_edit_remove_message() -> None:
    metadata = {
        "replay_kind": "edit",
        "regenerate_from_message_id": "ai-1",
        "regenerate_from_run_id": "run-1",
        "regenerate_checkpoint_id": "checkpoint-1",
        "edit_from_message_id": "turn-4__user",
        "edit_message_id": "edit-2__user",
        "edit_version_group_id": "turn-4__user",
    }
    trusted_input = {
        "messages": [
            {"type": "remove", "id": "turn-4"},
            {
                "type": "human",
                "id": "edit-2",
                "content": [{"type": "text", "text": "new text"}],
                "additional_kwargs": {},
            },
        ]
    }
    service = SimpleNamespace(
        prepare_edit_regenerate=AsyncMock(
            return_value={
                "input": trusted_input,
                "checkpoint": {
                    "checkpoint_ns": "",
                    "checkpoint_id": "checkpoint-1",
                    "checkpoint_map": None,
                },
                "metadata": metadata,
                "target_run_id": "run-1",
                "replacement_human_message_id": "edit-2__user",
                "source_message_ids": ["turn-4__user", "ai-1"],
            }
        )
    )
    body = PrivateRunCreateRequest(
        input={
            "messages": [
                {
                    "type": "human",
                    "id": "edit-2",
                    "content": [{"type": "text", "text": "new text"}],
                }
            ]
        },
        checkpoint={
            "checkpoint_ns": "",
            "checkpoint_id": "checkpoint-1",
            "checkpoint_map": None,
        },
        metadata=metadata,
    )

    normalized = await _normalize_prepared_edit_replay(
        body,
        thread_id="thread-1",
        context=SimpleNamespace(request_id="request-1"),
        service=service,
        app_config=object(),
    )

    assert normalized.input == trusted_input
    service.prepare_edit_regenerate.assert_awaited_once_with(
        ANY,
        "thread-1",
        human_message_id="turn-4__user",
        replacement_text="new text",
        replacement_base_id="edit-2",
        app_config=ANY,
    )


@pytest.mark.anyio
async def test_run_ingress_revalidates_regeneration_before_issuing_rebase_reason() -> None:
    metadata = {
        "regenerate_from_message_id": "ai-2",
        "regenerate_from_run_id": "run-2",
        "regenerate_checkpoint_id": "checkpoint-2",
    }
    trusted_input = {
        "messages": [
            {
                "type": "human",
                "id": "human-2",
                "content": "again",
            }
        ],
        "title": "Server-owned Thread title",
    }
    service = SimpleNamespace(
        prepare_regenerate=AsyncMock(
            return_value={
                "input": trusted_input,
                "checkpoint": {
                    "checkpoint_ns": "",
                    "checkpoint_id": "checkpoint-2",
                    "checkpoint_map": None,
                },
                "metadata": metadata,
                "target_run_id": "run-2",
            }
        )
    )
    body = PrivateRunCreateRequest(
        input=trusted_input,
        checkpoint={
            "checkpoint_ns": "",
            "checkpoint_id": "checkpoint-2",
            "checkpoint_map": None,
        },
        metadata=metadata,
    )
    assert body.input == {"messages": trusted_input["messages"]}

    normalized = await _normalize_prepared_edit_replay(
        body,
        thread_id="thread-2",
        context=SimpleNamespace(request_id="request-2"),
        service=service,
        app_config=object(),
    )

    assert normalized.input == trusted_input
    service.prepare_regenerate.assert_awaited_once_with(
        ANY,
        "thread-2",
        message_id="ai-2",
        app_config=ANY,
    )

    forged = body.model_copy(
        update={
            "metadata": {
                **metadata,
                "regenerate_checkpoint_id": "checkpoint-forged",
            }
        }
    )
    with pytest.raises(PrivateWorkConflict):
        await _normalize_prepared_edit_replay(
            forged,
            thread_id="thread-2",
            context=SimpleNamespace(request_id="request-2"),
            service=service,
            app_config=object(),
        )

    forged_input = body.model_copy(
        update={
            "input": {
                "messages": [
                    {
                        "type": "human",
                        "id": "human-2",
                        "content": "forged",
                    }
                ]
            }
        }
    )
    with pytest.raises(PrivateWorkConflict):
        await _normalize_prepared_edit_replay(
            forged_input,
            thread_id="thread-2",
            context=SimpleNamespace(request_id="request-2"),
            service=service,
            app_config=object(),
        )


def test_worker_decodes_server_validated_remove_message() -> None:
    graph_input = RunAgentPrivateExecutor._graph_input(
        SimpleNamespace(
            resume_from_checkpoint=False,
            command=None,
            graph_input={
                "messages": [
                    {"type": "remove", "id": "turn-5"},
                    {
                        "type": "human",
                        "id": "edit-3",
                        "content": "new text",
                    },
                ]
            },
        )
    )

    assert isinstance(graph_input["messages"][0], RemoveMessage)
    assert graph_input["messages"][0].id == "turn-5"
