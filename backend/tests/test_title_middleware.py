"""Title generation uses the system default model when unset."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.system_runtime_settings.models import auxiliary_model_snapshot_ref
from deerflow.agents.middlewares.title_middleware import TitleMiddleware
from deerflow.config.title_config import TitleConfig


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

    class _Model:
        async def ainvoke(self, prompt, config=None):
            created["prompt"] = prompt
            return SimpleNamespace(content="Quarterly Report")

    def fake_create_chat_model(name=None, **_kwargs):
        created["name"] = name
        return _Model()

    monkeypatch.setattr(
        "deerflow.agents.middlewares.title_middleware.create_chat_model",
        fake_create_chat_model,
    )
    monkeypatch.setattr(
        "deerflow.agents.middlewares.title_middleware.check_authorization_boundary",
        AsyncMock(),
    )

    middleware = TitleMiddleware(title_config=TitleConfig(enabled=True, model_name=None))
    result = await middleware.aafter_model(
        _title_state(),
        SimpleNamespace(context={}),
    )

    assert created["name"] is None
    assert result == {"title": "Quarterly Report"}


@pytest.mark.asyncio
async def test_async_title_falls_back_locally_when_model_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_create_chat_model(**_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(
        "deerflow.agents.middlewares.title_middleware.create_chat_model",
        fake_create_chat_model,
    )
    monkeypatch.setattr(
        "deerflow.agents.middlewares.title_middleware.check_authorization_boundary",
        AsyncMock(),
    )

    middleware = TitleMiddleware(title_config=TitleConfig(enabled=True, model_name=None))
    result = await middleware.aafter_model(
        _title_state(),
        SimpleNamespace(context={}),
    )

    assert result == {"title": "Please summarize the quarterly report"}
