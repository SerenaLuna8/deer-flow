"""Focused profile tests for production ModelRuntime call sites."""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr

import app.worker.memory_dream as memory_dream_module
import deerflow.runtime.goal as goal_module
import deerflow.skills.security_scanner as security_scanner_module
from app.gateway.routers.project_input_polish import ProjectInputPolishService
from app.private_work.chat_controls import ProjectChatControlService
from app.worker.memory_dream import MemoryDreamJobHandler
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.models import ModelRuntimeProfile


def _oneshot_profile_in(method: object) -> str | None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run_oneshot_llm"]
    assert len(calls) == 1
    profile = next(
        (keyword.value for keyword in calls[0].keywords if keyword.arg == "profile"),
        None,
    )
    if not isinstance(profile, ast.Attribute) or not isinstance(profile.value, ast.Name):
        return None
    return f"{profile.value.id}.{profile.attr}"


def test_private_gateway_auxiliary_calls_use_explicit_private_profile() -> None:
    assert _oneshot_profile_in(ProjectInputPolishService.polish) == "ModelRuntimeProfile.PRIVATE_ONESHOT"
    assert _oneshot_profile_in(ProjectChatControlService.suggest) == "ModelRuntimeProfile.PRIVATE_ONESHOT"


def test_goal_evaluator_builds_through_private_oneshot_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    expected = object()

    def build_model(_self: object, **kwargs: Any) -> object:
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(
        goal_module.ModelRuntime,
        "build_chat_model",
        build_model,
    )

    result = goal_module.create_goal_evaluator_model(
        model_name="goal-model",
        app_config=object(),
    )

    assert result is expected
    assert calls == [
        {
            "profile": ModelRuntimeProfile.PRIVATE_ONESHOT,
            "model_name": "goal-model",
            "thinking_enabled": False,
        }
    ]


@pytest.mark.asyncio
async def test_goal_evaluator_invokes_through_runtime_with_abort_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    abort_event = asyncio.Event()

    async def invoke(_self: object, input_: object, **kwargs: Any) -> object:
        calls.append({"input": input_, **kwargs})
        return AIMessage(content=('{"satisfied":true,"blocker":"none","reason":"done","evidence_summary":"visible result"}'))

    monkeypatch.setattr(goal_module.ModelRuntime, "ainvoke", invoke)

    result = await goal_module.evaluate_goal_completion(
        {"objective": "finish"},  # type: ignore[arg-type]
        [
            SimpleNamespace(type="human", content="please finish"),
            SimpleNamespace(type="ai", content="finished"),
        ],
        model=object(),
        app_config=object(),
        abort_event=abort_event,
    )

    assert result["satisfied"] is True
    assert calls[0]["profile"] is ModelRuntimeProfile.PRIVATE_ONESHOT
    assert calls[0]["abort_event"] is abort_event
    assert calls[0]["config"] == {"run_name": "goal_evaluator"}


@pytest.mark.asyncio
async def test_security_scanner_invokes_through_private_oneshot_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def invoke(_self: object, input_: object, **kwargs: Any) -> object:
        calls.append({"input": input_, **kwargs})
        return SimpleNamespace(
            content='{"decision":"allow","reason":"safe"}',
        )

    monkeypatch.setattr(
        security_scanner_module.ModelRuntime,
        "ainvoke",
        invoke,
    )

    result = await security_scanner_module.scan_skill_content(
        "# Harmless skill",
        app_config=object(),  # type: ignore[arg-type]
    )

    assert result.decision == "allow"
    assert calls[0]["profile"] is ModelRuntimeProfile.PRIVATE_ONESHOT
    assert calls[0]["thinking_enabled"] is False
    assert calls[0]["config"] == {"run_name": "security_agent"}


def test_memory_dream_builds_through_private_oneshot_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ModelConfig(
        name="dream-model",
        display_name="Dream model",
        use="langchain_openai:ChatOpenAI",
        model="provider-model",
        max_input_tokens=64_000,
        api_key=SecretStr("unit-test-key"),
    )
    handler = object.__new__(MemoryDreamJobHandler)
    handler._app_config = AppConfig(
        models=[model],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )
    calls: list[dict[str, Any]] = []
    runtimes: list[object] = []
    expected = SimpleNamespace(bind_tools=lambda _tools: None)

    def build_model(self: object, **kwargs: Any) -> object:
        runtimes.append(self)
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(
        memory_dream_module.ModelRuntime,
        "build_chat_model",
        build_model,
    )
    monkeypatch.setattr(
        memory_dream_module,
        "model_supports_temperature",
        lambda *_args, **_kwargs: False,
    )

    runner = handler._make_runner(model)

    assert runner._model is expected
    assert runner._model_runtime is runtimes[0]
    assert calls == [
        {
            "profile": ModelRuntimeProfile.PRIVATE_ONESHOT,
            "model_name": "dream-model",
            "model_overrides": None,
        }
    ]
