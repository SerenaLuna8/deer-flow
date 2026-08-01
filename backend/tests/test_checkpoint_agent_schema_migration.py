"""Agent-factory checkpoint schema and process-freeze migration contracts."""

from __future__ import annotations

from typing import Any, get_type_hints

import pytest
from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.channels import DeltaChannel
from langgraph.checkpoint.memory import InMemorySaver

from deerflow.agents import factory as agent_factory
from deerflow.agents.factory import create_deerflow_agent
from deerflow.agents.lead_agent import agent as lead_agent_module
from deerflow.agents.thread_state import ThreadState
from deerflow.config.app_config import AppConfig
from deerflow.runtime import checkpoint_mode
from deerflow.runtime.checkpoint_mode import (
    CHECKPOINT_MODE_METADATA_KEY,
    INTERNAL_CHECKPOINT_MODE_KEY,
    CheckpointModeReconfigurationError,
)


class _MockChatModel(FakeMessagesListChatModel):
    """Deterministic local model; no provider or network access."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> _MockChatModel:  # type: ignore[override]
        return self


class _MiddlewareState(AgentState):
    middleware_value: str


class _SchemaMiddleware(AgentMiddleware):
    state_schema = _MiddlewareState


class _StopAfterCheckpointFreeze(RuntimeError):
    """Test-only boundary used to avoid unrelated graph assembly."""


@pytest.fixture(autouse=True)
def _reset_process_checkpoint_freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkpoint_mode, "_frozen_checkpoint_channel_mode", None)
    monkeypatch.setattr(checkpoint_mode, "_frozen_checkpoint_snapshot_frequency", None)


def _mock_model() -> _MockChatModel:
    return _MockChatModel(responses=[AIMessage(content="mock response")])


def _app_config(
    *,
    mode: str = "full",
    snapshot_frequency: int = 10,
) -> AppConfig:
    return AppConfig.model_validate(
        {
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
            },
            "database": {
                "url": "postgresql://postgres@localhost/deerflow",
                "checkpoint_channel_mode": mode,
                "checkpoint_delta": {
                    "snapshot_frequency": snapshot_frequency,
                },
            },
        }
    )


def _delta_channel_from_schema(schema: type) -> DeltaChannel:
    messages = get_type_hints(schema, include_extras=True)["messages"]
    return next(metadata for metadata in messages.__metadata__ if isinstance(metadata, DeltaChannel))


def _stop_after_checkpoint_freeze(_agent_name: str | None) -> None:
    raise _StopAfterCheckpointFreeze


def _make_until_checkpoint_freeze(
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: dict[str, Any],
) -> None:
    monkeypatch.setattr(
        lead_agent_module,
        "load_agent_config",
        _stop_after_checkpoint_freeze,
    )

    def _forbid_ambient_config() -> AppConfig:
        raise AssertionError("runtime AppConfig must be supplied by the private runtime context")

    monkeypatch.setattr(
        lead_agent_module,
        "get_app_config",
        _forbid_ambient_config,
    )
    with pytest.raises(_StopAfterCheckpointFreeze):
        lead_agent_module.make_lead_agent(config)


def test_create_deerflow_agent_defaults_to_full_message_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_factory, "create_agent", lambda **kwargs: kwargs)

    assembled = create_deerflow_agent(_mock_model(), middleware=[])

    assert assembled["state_schema"] is ThreadState
    assert assembled["checkpointer"] is None


def test_create_deerflow_agent_delta_without_checkpointer_compiles_delta_schema() -> None:
    graph = create_deerflow_agent(
        _mock_model(),
        middleware=[],
        checkpoint_channel_mode="delta",
        checkpoint_snapshot_frequency=3,
    )

    messages = graph.channels["messages"]
    assert isinstance(messages, DeltaChannel)
    assert messages.snapshot_frequency == 3


def test_create_deerflow_agent_delta_synchronizes_middleware_state_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = _SchemaMiddleware()
    original_schema = middleware.state_schema
    monkeypatch.setattr(agent_factory, "create_agent", lambda **kwargs: kwargs)

    assembled = create_deerflow_agent(
        _mock_model(),
        middleware=[middleware],
        checkpoint_channel_mode="delta",
        checkpoint_snapshot_frequency=4,
    )

    normalized = assembled["middleware"][0]
    assert normalized is not middleware
    assert middleware.state_schema is original_schema
    assert _delta_channel_from_schema(assembled["state_schema"]).snapshot_frequency == 4
    assert _delta_channel_from_schema(normalized.state_schema).snapshot_frequency == 4


def test_create_deerflow_agent_delta_with_checkpointer_fails_closed() -> None:
    saver = InMemorySaver()

    with pytest.raises(ValueError, match="does not support delta persistence"):
        create_deerflow_agent(
            _mock_model(),
            middleware=[],
            checkpoint_channel_mode="delta",
            checkpointer=saver,
        )

    assert list(saver.list(None)) == []


def test_make_lead_agent_first_freezes_mode_and_frequency_from_app_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config: dict[str, Any] = {
        "context": {
            "app_config": _app_config(
                mode="delta",
                snapshot_frequency=7,
            ),
            "user_id": "owner",
        }
    }

    _make_until_checkpoint_freeze(monkeypatch, config=config)

    assert checkpoint_mode.frozen_checkpoint_channel_mode() == "delta"
    assert checkpoint_mode.frozen_checkpoint_snapshot_frequency() == 7
    assert config["configurable"][INTERNAL_CHECKPOINT_MODE_KEY] == "delta"
    assert config["metadata"][CHECKPOINT_MODE_METADATA_KEY] == "delta"


def test_make_lead_agent_forged_client_mode_cannot_control_first_freeze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config: dict[str, Any] = {
        "configurable": {
            INTERNAL_CHECKPOINT_MODE_KEY: "delta",
        },
        "context": {
            "app_config": _app_config(
                mode="full",
                snapshot_frequency=7,
            ),
            "user_id": "owner",
        },
    }

    _make_until_checkpoint_freeze(monkeypatch, config=config)

    assert checkpoint_mode.frozen_checkpoint_channel_mode() == "full"
    assert checkpoint_mode.frozen_checkpoint_snapshot_frequency() == 7
    assert config["configurable"][INTERNAL_CHECKPOINT_MODE_KEY] == "full"
    assert CHECKPOINT_MODE_METADATA_KEY not in config["metadata"]


def test_make_lead_agent_rejects_mode_mismatch_after_process_freeze() -> None:
    checkpoint_mode.freeze_checkpoint_channel_mode("full")
    checkpoint_mode.freeze_checkpoint_snapshot_frequency(7)
    config: dict[str, Any] = {
        "configurable": {
            INTERNAL_CHECKPOINT_MODE_KEY: "delta",
        },
        "context": {
            "app_config": _app_config(
                mode="delta",
                snapshot_frequency=7,
            ),
            "user_id": "owner",
        },
    }

    with pytest.raises(CheckpointModeReconfigurationError, match="restart"):
        lead_agent_module.make_lead_agent(config)


def test_make_lead_agent_rejects_frequency_mismatch_after_process_freeze() -> None:
    checkpoint_mode.freeze_checkpoint_channel_mode("delta")
    checkpoint_mode.freeze_checkpoint_snapshot_frequency(7)
    config: dict[str, Any] = {
        "configurable": {
            INTERNAL_CHECKPOINT_MODE_KEY: "delta",
        },
        "context": {
            "app_config": _app_config(
                mode="delta",
                snapshot_frequency=8,
            ),
            "user_id": "owner",
        },
    }

    with pytest.raises(CheckpointModeReconfigurationError, match="restart"):
        lead_agent_module.make_lead_agent(config)
