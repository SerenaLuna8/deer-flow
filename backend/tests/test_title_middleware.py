"""Title generation uses the system default model when unset."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.system_runtime_settings.models import auxiliary_model_snapshot_ref
from deerflow.agents.middlewares.title_middleware import TitleMiddleware
from deerflow.config.title_config import TitleConfig
from deerflow.models import ModelRuntimeProfile
from deerflow.runtime.context_keys import RuntimeContextKeys


def test_unset_title_model_admits_catalog_default() -> None:
    assert (
        auxiliary_model_snapshot_ref(
            "title",
            None,
            title_enabled=True,
        )
        == "default"
    )
    assert (
        auxiliary_model_snapshot_ref(
            "title",
            None,
            title_enabled=False,
        )
        is None
    )
    assert (
        auxiliary_model_snapshot_ref(
            "title",
            "pinned-model",
            title_enabled=True,
        )
        == "pinned-model"
    )
    assert (
        auxiliary_model_snapshot_ref(
            "summarization",
            None,
            title_enabled=True,
        )
        is None
    )


def _title_state() -> dict[str, object]:
    return {
        "messages": [
            HumanMessage(content="Please summarize the quarterly report"),
            AIMessage(content="Here is the summary."),
        ],
    }


@pytest.mark.asyncio
async def test_async_title_calls_default_model_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[str, object] = {}

    abort_event = asyncio.Event()

    class _Model:
        async def ainvoke(self, prompt, config=None):
            created["prompt"] = prompt
            created["invoke_config"] = config
            return SimpleNamespace(content="Quarterly Report")

    def fake_build_chat_model(_self, *, model_name=None, profile=None, **_kwargs):
        created["name"] = model_name
        created["profile"] = profile
        return _Model()

    monkeypatch.setattr(
        "deerflow.agents.middlewares.title_middleware.ModelRuntime.build_chat_model",
        fake_build_chat_model,
    )
    monkeypatch.setattr(
        "deerflow.agents.middlewares.title_middleware.check_authorization_boundary",
        AsyncMock(),
    )

    middleware = TitleMiddleware(
        app_config=SimpleNamespace(),
        title_config=TitleConfig(enabled=True, model_name=None),
    )
    result = await middleware.aafter_model(
        _title_state(),
        SimpleNamespace(
            context={RuntimeContextKeys.SERVER_ABORT_EVENT: abort_event},
        ),
    )

    assert created["name"] is None
    assert created["profile"] is ModelRuntimeProfile.AGENT_GRAPH
    assert created["invoke_config"]["run_name"] == "title_agent"
    assert result == {"title": "Quarterly Report"}


@pytest.mark.asyncio
async def test_async_title_propagates_preexisting_run_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    abort_event = asyncio.Event()
    abort_event.set()

    class _Model:
        async def ainvoke(self, prompt, config=None):
            raise AssertionError((prompt, config))

    monkeypatch.setattr(
        "deerflow.agents.middlewares.title_middleware.ModelRuntime.build_chat_model",
        lambda _self, **_kwargs: _Model(),
    )
    monkeypatch.setattr(
        "deerflow.agents.middlewares.title_middleware.check_authorization_boundary",
        AsyncMock(),
    )
    middleware = TitleMiddleware(
        app_config=SimpleNamespace(),
        title_config=TitleConfig(enabled=True, model_name=None),
    )

    with pytest.raises(asyncio.CancelledError):
        await middleware.aafter_model(
            _title_state(),
            SimpleNamespace(
                context={RuntimeContextKeys.SERVER_ABORT_EVENT: abort_event},
            ),
        )


@pytest.mark.asyncio
async def test_async_title_falls_back_locally_when_model_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_build_chat_model(_self, **_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(
        "deerflow.agents.middlewares.title_middleware.ModelRuntime.build_chat_model",
        fake_build_chat_model,
    )
    monkeypatch.setattr(
        "deerflow.agents.middlewares.title_middleware.check_authorization_boundary",
        AsyncMock(),
    )

    middleware = TitleMiddleware(
        app_config=SimpleNamespace(),
        title_config=TitleConfig(enabled=True, model_name=None),
    )
    result = await middleware.aafter_model(
        _title_state(),
        SimpleNamespace(context={}),
    )

    assert result == {"title": "Please summarize the quarterly report"}
