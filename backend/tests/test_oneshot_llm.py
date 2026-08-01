from __future__ import annotations

from types import SimpleNamespace

import pytest

from deerflow.utils import oneshot_llm


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    async def ainvoke(self, messages, config):
        self.calls.append((messages, config))
        return SimpleNamespace(content="generated")


@pytest.mark.anyio
@pytest.mark.parametrize("attach_tracing", (True, False))
async def test_run_oneshot_llm_forwards_explicit_tracing_policy(
    monkeypatch,
    attach_tracing: bool,
) -> None:
    model = FakeModel()
    captured: dict[str, object] = {}
    metadata_calls: list[tuple[object, object]] = []

    def create_model(**kwargs):
        captured.update(kwargs)
        return model

    monkeypatch.setattr(oneshot_llm, "create_chat_model", create_model)
    monkeypatch.setattr(
        oneshot_llm,
        "inject_langfuse_metadata",
        lambda *args, **kwargs: metadata_calls.append((args, kwargs)),
    )

    result = await oneshot_llm.run_oneshot_llm(
        system_instruction="private system",
        user_content="private user",
        run_name="privacy-test",
        app_config=SimpleNamespace(),
        attach_tracing=attach_tracing,
    )

    assert result == "generated"
    assert captured["attach_tracing"] is attach_tracing
    assert bool(metadata_calls) is attach_tracing
    if attach_tracing:
        assert metadata_calls[0][1]["include_deerflow_trace_id"] is False
    assert len(model.calls) == 1
