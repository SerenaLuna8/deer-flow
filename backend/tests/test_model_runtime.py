from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langsmith.run_helpers import get_tracing_context, tracing_context
from pydantic import SecretStr

import deerflow.models.runtime as model_runtime_module
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.models.factory import (
    RuntimeModelSettingsUnsupported,
    create_chat_model,
)
from deerflow.models.runtime import ModelRuntime, ModelRuntimeProfile
from deerflow.utils.oneshot_llm import run_oneshot_llm


def test_model_runtime_profiles_are_closed_private_and_bounded() -> None:
    assert set(ModelRuntimeProfile) == {
        ModelRuntimeProfile.AGENT_GRAPH,
        ModelRuntimeProfile.PRIVATE_ONESHOT,
        ModelRuntimeProfile.SENSITIVE_MULTIMODAL,
        ModelRuntimeProfile.ADMIN_PROBE,
    }
    assert model_runtime_module._PROFILE_POLICIES[ModelRuntimeProfile.AGENT_GRAPH].default_timeout_seconds is None
    for profile in (
        ModelRuntimeProfile.PRIVATE_ONESHOT,
        ModelRuntimeProfile.SENSITIVE_MULTIMODAL,
        ModelRuntimeProfile.ADMIN_PROBE,
    ):
        policy = model_runtime_module._PROFILE_POLICIES[profile]
        assert policy.attach_model_tracing is False
        assert policy.default_timeout_seconds == 600.0


@pytest.mark.anyio
async def test_model_runtime_builds_through_shared_factory_and_invokes_model() -> None:
    observed: dict[str, object] = {}
    response = AIMessage(content="runtime response")

    class FakeChatModel:
        async def ainvoke(
            self,
            messages: object,
            *,
            config: object,
        ) -> AIMessage:
            observed["messages"] = messages
            observed["invoke_config"] = config
            return response

    def model_factory(**kwargs: object) -> FakeChatModel:
        observed["factory_kwargs"] = kwargs
        return FakeChatModel()

    app_config = SimpleNamespace()
    runtime = ModelRuntime(
        app_config=app_config,  # type: ignore[arg-type]
        model_factory=model_factory,
    )
    messages = [HumanMessage(content="hello")]
    invoke_config = {"run_name": "model-runtime-test"}

    result = await runtime.ainvoke(
        messages,
        profile=ModelRuntimeProfile.AGENT_GRAPH,
        model_name="model-id",
        thinking_enabled=True,
        reasoning_effort="low",
        model_overrides={"max_tokens": 128},
        config=invoke_config,
    )

    assert result is response
    assert observed == {
        "factory_kwargs": {
            "name": "model-id",
            "thinking_enabled": True,
            "app_config": app_config,
            "attach_tracing": False,
            "model_overrides": {"max_tokens": 128},
            "reasoning_effort": "low",
            "runtime_overrides": {"max_retries": 0},
        },
        "messages": messages,
        "invoke_config": invoke_config,
    }


@pytest.mark.anyio
async def test_model_runtime_does_not_forward_absent_reasoning_effort() -> None:
    observed: dict[str, object] = {}

    class FakeChatModel:
        async def ainvoke(
            self,
            messages: object,
            *,
            config: object,
        ) -> AIMessage:
            return AIMessage(content="ok")

    def model_factory(**kwargs: object) -> FakeChatModel:
        observed.update(kwargs)
        return FakeChatModel()

    runtime = ModelRuntime(
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        model_factory=model_factory,
    )

    await runtime.ainvoke(
        [HumanMessage(content="hello")],
        profile=ModelRuntimeProfile.PRIVATE_ONESHOT,
        config={"run_name": "model-runtime-test"},
    )

    assert "reasoning_effort" not in observed
    assert observed["attach_tracing"] is False
    assert observed["runtime_overrides"] == {"max_retries": 2}


@pytest.mark.parametrize(
    "profile",
    [
        ModelRuntimeProfile.PRIVATE_ONESHOT,
        ModelRuntimeProfile.SENSITIVE_MULTIMODAL,
        ModelRuntimeProfile.ADMIN_PROBE,
    ],
)
def test_private_profiles_disable_model_tracing(
    profile: ModelRuntimeProfile,
) -> None:
    observed: dict[str, object] = {}

    def model_factory(**kwargs: object) -> object:
        observed.update(kwargs)
        return object()

    runtime = ModelRuntime(
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        model_factory=model_factory,
    )

    runtime.build_chat_model(profile=profile)

    assert observed["attach_tracing"] is False
    expected_retries = 2 if profile is ModelRuntimeProfile.PRIVATE_ONESHOT else 0
    assert observed["runtime_overrides"] == {"max_retries": expected_retries}


@pytest.mark.anyio
async def test_sensitive_runtime_clears_inherited_callbacks() -> None:
    observed: dict[str, object] = {}

    class FakeChatModel:
        async def ainvoke(self, messages: object, *, config: object) -> AIMessage:
            del messages
            observed["config"] = config
            return AIMessage(content="ok")

    runtime = ModelRuntime(
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        model_factory=lambda **_kwargs: FakeChatModel(),
    )

    await runtime.ainvoke(
        [HumanMessage(content="private")],
        profile=ModelRuntimeProfile.SENSITIVE_MULTIMODAL,
        config={"callbacks": [object()], "run_name": "private-image"},
    )

    assert observed["config"] == {
        "callbacks": [],
        "run_name": "private-image",
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "profile",
    [
        ModelRuntimeProfile.PRIVATE_ONESHOT,
        ModelRuntimeProfile.SENSITIVE_MULTIMODAL,
        ModelRuntimeProfile.ADMIN_PROBE,
    ],
)
async def test_private_profiles_disable_ambient_langsmith_tracing(
    profile: ModelRuntimeProfile,
) -> None:
    observed: dict[str, object] = {}

    class FakeChatModel:
        async def ainvoke(self, messages: object, *, config: object) -> AIMessage:
            del messages, config
            observed["tracing_enabled"] = get_tracing_context()["enabled"]
            return AIMessage(content="ok")

    runtime = ModelRuntime(
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        model_factory=lambda **_kwargs: FakeChatModel(),
    )

    with tracing_context(enabled=True):
        await runtime.ainvoke(
            [HumanMessage(content="private")],
            profile=profile,
        )

    assert observed["tracing_enabled"] is False


@pytest.mark.anyio
async def test_model_runtime_invokes_bound_runnable_under_private_profile() -> None:
    observed: dict[str, object] = {}
    response = AIMessage(content="private bound response")

    class BoundRunnable:
        async def ainvoke(
            self,
            value: object,
            *,
            config: object,
        ) -> AIMessage:
            observed["value"] = value
            observed["config"] = config
            observed["tracing_enabled"] = get_tracing_context()["enabled"]
            return response

    runtime = ModelRuntime(
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        model_factory=lambda **_kwargs: object(),
    )
    input_value = [HumanMessage(content="private")]

    with tracing_context(enabled=True):
        result = await runtime.ainvoke_runnable(
            BoundRunnable(),
            input_value,
            profile=ModelRuntimeProfile.PRIVATE_ONESHOT,
            config={"callbacks": [object()], "run_name": "private-bound"},
        )

    assert result is response
    assert observed == {
        "value": input_value,
        "config": {"callbacks": [], "run_name": "private-bound"},
        "tracing_enabled": False,
    }


def test_model_runtime_invokes_sync_graph_runnable_with_exact_config() -> None:
    observed: dict[str, object] = {}
    response = AIMessage(content="sync graph response")

    class GraphRunnable:
        def invoke(self, value: object, *, config: object) -> AIMessage:
            observed["value"] = value
            observed["config"] = config
            return response

    invoke_config = {
        "run_name": "sync-summary",
        "metadata": {"lc_source": "summarization"},
    }
    result = ModelRuntime.invoke_runnable(
        GraphRunnable(),
        "summary prompt",
        profile=ModelRuntimeProfile.AGENT_GRAPH,
        config=invoke_config,
    )

    assert result is response
    assert observed == {
        "value": "summary prompt",
        "config": invoke_config,
    }


def test_model_runtime_sync_runnable_rejects_profiles_with_runtime_owned_policy() -> None:
    class NeverInvoked:
        def invoke(self, value: object, *, config: object) -> object:
            raise AssertionError((value, config))

    with pytest.raises(ValueError, match="only supports AGENT_GRAPH"):
        ModelRuntime.invoke_runnable(
            NeverInvoked(),
            "private",
            profile=ModelRuntimeProfile.PRIVATE_ONESHOT,
        )


@pytest.mark.anyio
async def test_model_runtime_bound_runnable_deadline_cancels_and_joins() -> None:
    started = asyncio.Event()
    cancelled_and_joined = asyncio.Event()

    class BlockingRunnable:
        async def ainvoke(self, value: object, *, config: object) -> AIMessage:
            del value, config
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                raise
            finally:
                cancelled_and_joined.set()

    runtime = ModelRuntime(
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        model_factory=lambda **_kwargs: object(),
    )

    with pytest.raises(TimeoutError):
        await runtime.ainvoke_runnable(
            BlockingRunnable(),
            "private",
            profile=ModelRuntimeProfile.PRIVATE_ONESHOT,
            deadline_monotonic=time.monotonic() + 0.01,
        )

    assert started.is_set()
    assert cancelled_and_joined.is_set()


@pytest.mark.anyio
async def test_model_runtime_bound_runnable_caller_cancel_joins_provider() -> None:
    started = asyncio.Event()
    cancelled_and_joined = asyncio.Event()

    class BlockingRunnable:
        async def ainvoke(self, value: object, *, config: object) -> AIMessage:
            del value, config
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                raise
            finally:
                cancelled_and_joined.set()

    runtime = ModelRuntime(
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        model_factory=lambda **_kwargs: object(),
    )
    invocation = asyncio.create_task(
        runtime.ainvoke_runnable(
            BlockingRunnable(),
            "private",
            profile=ModelRuntimeProfile.PRIVATE_ONESHOT,
        )
    )
    await started.wait()
    invocation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await invocation

    assert cancelled_and_joined.is_set()


@pytest.mark.anyio
async def test_model_runtime_abort_wins_when_provider_completes_simultaneously() -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    class SimultaneousRunnable:
        async def ainvoke(self, value: object, *, config: object) -> AIMessage:
            del value, config
            started.set()
            await release.wait()
            return AIMessage(content="must not escape abort")

    class SimultaneousAbort:
        def is_set(self) -> bool:
            return False

        async def wait(self) -> object:
            await release.wait()
            return None

    runtime = ModelRuntime(
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        model_factory=lambda **_kwargs: object(),
    )
    invocation = asyncio.create_task(
        runtime.ainvoke_runnable(
            SimultaneousRunnable(),
            "private",
            profile=ModelRuntimeProfile.PRIVATE_ONESHOT,
            abort_event=SimultaneousAbort(),
        )
    )
    await started.wait()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await invocation


def test_model_runtime_rejects_unregistered_profile_values() -> None:
    runtime = ModelRuntime(
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        model_factory=lambda **_kwargs: object(),
    )

    with pytest.raises(TypeError, match="ModelRuntimeProfile"):
        runtime.build_chat_model(profile="oneshot")  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_model_runtime_deadline_cancels_provider_task() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingChatModel:
        async def ainvoke(
            self,
            messages: object,
            *,
            config: object,
        ) -> AIMessage:
            del messages, config
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    runtime = ModelRuntime(
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        model_factory=lambda **_kwargs: BlockingChatModel(),
    )

    with pytest.raises(TimeoutError):
        await runtime.ainvoke(
            [HumanMessage(content="hello")],
            profile=ModelRuntimeProfile.ADMIN_PROBE,
            config={"run_name": "deadline-test"},
            deadline_monotonic=time.monotonic() + 0.01,
        )

    assert started.is_set()
    assert cancelled.is_set()


@pytest.mark.anyio
async def test_model_runtime_profile_default_deadline_cancels_and_joins_provider_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    joined = asyncio.Event()

    class BlockingChatModel:
        async def ainvoke(
            self,
            messages: object,
            *,
            config: object,
        ) -> AIMessage:
            del messages, config
            started.set()
            try:
                await asyncio.Future()
            finally:
                # Prove ModelRuntime awaits Provider cleanup instead of merely
                # requesting cancellation and returning the timeout early.
                await asyncio.sleep(0)
                joined.set()

    policy = model_runtime_module._PROFILE_POLICIES[ModelRuntimeProfile.PRIVATE_ONESHOT]
    monkeypatch.setitem(
        model_runtime_module._PROFILE_POLICIES,
        ModelRuntimeProfile.PRIVATE_ONESHOT,
        replace(policy, default_timeout_seconds=0.01),
    )
    runtime = ModelRuntime(
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        model_factory=lambda **_kwargs: BlockingChatModel(),
    )

    with pytest.raises(TimeoutError):
        await runtime.ainvoke(
            [HumanMessage(content="hello")],
            profile=ModelRuntimeProfile.PRIVATE_ONESHOT,
            config={"run_name": "profile-default-deadline-test"},
        )

    assert started.is_set()
    assert joined.is_set()


@pytest.mark.anyio
async def test_model_runtime_abort_signal_cancels_provider_task() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    abort_event = asyncio.Event()

    class BlockingChatModel:
        async def ainvoke(
            self,
            messages: object,
            *,
            config: object,
        ) -> AIMessage:
            del messages, config
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    runtime = ModelRuntime(
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        model_factory=lambda **_kwargs: BlockingChatModel(),
    )
    invocation = asyncio.create_task(
        runtime.ainvoke(
            [HumanMessage(content="hello")],
            profile=ModelRuntimeProfile.SENSITIVE_MULTIMODAL,
            config={"run_name": "abort-test"},
            abort_event=abort_event,
        )
    )
    await started.wait()
    abort_event.set()

    with pytest.raises(asyncio.CancelledError):
        await invocation

    assert cancelled.is_set()


def test_shared_factory_runtime_override_replaces_catalog_retry_without_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetryAwareModel:
        model_fields = {
            "max_retries": SimpleNamespace(alias=None),
        }

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(
        "deerflow.models.factory.resolve_class",
        lambda *_args, **_kwargs: RetryAwareModel,
    )
    model = ModelConfig(
        name="runtime-model",
        display_name="Runtime model",
        description="",
        use="example:RetryAwareModel",
        model="provider-model",
        max_input_tokens=64_000,
        max_retries=9,
    )
    app_config = AppConfig(
        models=[model],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )

    instance = create_chat_model(
        name=model.name,
        app_config=app_config,
        attach_tracing=False,
        runtime_overrides={"max_retries": 0},
    )

    assert instance.kwargs["max_retries"] == 0
    assert "runtime_overrides" not in instance.kwargs


def test_shared_factory_applies_catalog_input_limit_to_model_profile_without_provider_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProfileAwareModel:
        model_fields: dict[str, object] = {}

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.profile = {
                "max_input_tokens": 128_000,
                "supports_tool_calling": True,
            }

    monkeypatch.setattr(
        "deerflow.models.factory.resolve_class",
        lambda *_args, **_kwargs: ProfileAwareModel,
    )
    model = ModelConfig(
        name="runtime-model",
        display_name="Runtime model",
        description="",
        use="example:ProfileAwareModel",
        model="provider-model",
        max_input_tokens=64_000,
    )
    app_config = AppConfig(
        models=[model],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )

    instance = create_chat_model(
        name=model.name,
        app_config=app_config,
        attach_tracing=False,
    )

    assert "max_input_tokens" not in instance.kwargs
    assert instance.profile == {
        "max_input_tokens": 64_000,
        "supports_tool_calling": True,
    }


def _reasoning_capture_model(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_adapter: str,
    catalog_effort: str | None = None,
) -> tuple[ModelConfig, AppConfig]:
    class ReasoningCaptureModel:
        model_fields: dict[str, object] = {}

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(
        "deerflow.models.factory.resolve_class",
        lambda *_args, **_kwargs: ReasoningCaptureModel,
    )
    model_kwargs: dict[str, object] = {
        "name": "reasoning-model",
        "display_name": "Reasoning model",
        "description": "",
        "use": "example:ReasoningCaptureModel",
        "model": "provider-model",
        "max_input_tokens": 64_000,
        "supports_thinking": True,
        "supports_reasoning_effort": True,
    }
    if catalog_effort is not None:
        model_kwargs["reasoning_effort"] = catalog_effort
    model = ModelConfig(**model_kwargs)
    model._system_provider_adapter = provider_adapter
    return model, AppConfig(
        models=[model],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )


@pytest.mark.parametrize("provider_adapter", ["deepseek", "patched_deepseek"])
@pytest.mark.parametrize(
    ("canonical_effort", "provider_effort"),
    [("low", "low"), ("medium", "high"), ("high", "max")],
)
def test_shared_factory_maps_run_reasoning_effort_for_deepseek(
    monkeypatch: pytest.MonkeyPatch,
    provider_adapter: str,
    canonical_effort: str,
    provider_effort: str,
) -> None:
    model, app_config = _reasoning_capture_model(
        monkeypatch,
        provider_adapter=provider_adapter,
        catalog_effort="low",
    )

    instance = create_chat_model(
        name=model.name,
        thinking_enabled=True,
        reasoning_effort=canonical_effort,
        app_config=app_config,
        attach_tracing=False,
    )

    assert instance.kwargs["reasoning_effort"] == provider_effort


@pytest.mark.parametrize(
    ("canonical_effort", "provider_effort"),
    [("low", "low"), ("medium", "high"), ("high", "max")],
)
def test_shared_factory_deepseek_effort_reaches_openai_wire_payload(
    canonical_effort: str,
    provider_effort: str,
) -> None:
    model = ModelConfig(
        name="deepseek-wire-model",
        display_name="DeepSeek wire model",
        description="",
        use="deerflow.models.patched_deepseek:PatchedChatDeepSeek",
        model="deepseek-v4-flash",
        max_input_tokens=1_000_000,
        api_key=SecretStr("unit-test-key"),
        base_url="https://api.deepseek.com",
        max_tokens=51_200,
        supports_thinking=True,
        supports_reasoning_effort=True,
        when_thinking_enabled={
            "extra_body": {"thinking": {"type": "enabled"}},
        },
        when_thinking_disabled={
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    )
    model._system_provider_adapter = "patched_deepseek"
    app_config = AppConfig(
        models=[model],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )

    chat_model = create_chat_model(
        name=model.name,
        thinking_enabled=True,
        reasoning_effort=canonical_effort,
        app_config=app_config,
        attach_tracing=False,
    )
    payload = chat_model._get_request_payload([HumanMessage(content="hello")])

    assert payload["reasoning_effort"] == provider_effort
    assert payload["extra_body"] == {"thinking": {"type": "enabled"}}


def test_shared_factory_deepseek_flash_reaches_disabled_wire_payload() -> None:
    model = ModelConfig(
        name="deepseek-flash-wire-model",
        display_name="DeepSeek flash wire model",
        description="",
        use="deerflow.models.patched_deepseek:PatchedChatDeepSeek",
        model="deepseek-v4-flash",
        max_input_tokens=1_000_000,
        api_key=SecretStr("unit-test-key"),
        base_url="https://api.deepseek.com",
        reasoning_effort="high",
        supports_thinking=True,
        supports_reasoning_effort=True,
        when_thinking_enabled={
            "extra_body": {"thinking": {"type": "enabled"}},
        },
        when_thinking_disabled={
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    )
    model._system_provider_adapter = "patched_deepseek"
    app_config = AppConfig(
        models=[model],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )

    chat_model = create_chat_model(
        name=model.name,
        thinking_enabled=False,
        app_config=app_config,
        attach_tracing=False,
    )
    payload = chat_model._get_request_payload([HumanMessage(content="hello")])

    assert "reasoning_effort" not in payload
    assert payload["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.parametrize("provider_adapter", ["deepseek", "patched_deepseek"])
def test_shared_factory_run_flash_clears_deepseek_catalog_reasoning_default(
    monkeypatch: pytest.MonkeyPatch,
    provider_adapter: str,
) -> None:
    model, app_config = _reasoning_capture_model(
        monkeypatch,
        provider_adapter=provider_adapter,
        catalog_effort="high",
    )

    instance = create_chat_model(
        name=model.name,
        thinking_enabled=False,
        reasoning_effort="none",
        app_config=app_config,
        attach_tracing=False,
    )

    assert "reasoning_effort" not in instance.kwargs


@pytest.mark.parametrize("provider_adapter", ["deepseek", "patched_deepseek"])
def test_shared_factory_preserves_deepseek_provider_effort_without_run_override(
    monkeypatch: pytest.MonkeyPatch,
    provider_adapter: str,
) -> None:
    model, app_config = _reasoning_capture_model(
        monkeypatch,
        provider_adapter=provider_adapter,
        catalog_effort="max",
    )

    instance = create_chat_model(
        name=model.name,
        thinking_enabled=True,
        app_config=app_config,
        attach_tracing=False,
    )

    assert instance.kwargs["reasoning_effort"] == "max"


@pytest.mark.parametrize("provider_adapter", ["deepseek", "patched_deepseek"])
@pytest.mark.parametrize("canonical_effort", ["none", "minimal"])
def test_shared_factory_rejects_unsupported_run_reasoning_for_deepseek(
    monkeypatch: pytest.MonkeyPatch,
    provider_adapter: str,
    canonical_effort: str,
) -> None:
    model, app_config = _reasoning_capture_model(
        monkeypatch,
        provider_adapter=provider_adapter,
        catalog_effort="high",
    )

    with pytest.raises(RuntimeModelSettingsUnsupported):
        create_chat_model(
            name=model.name,
            thinking_enabled=True,
            reasoning_effort=canonical_effort,
            app_config=app_config,
            attach_tracing=False,
        )


def test_shared_factory_keeps_non_deepseek_run_reasoning_effort_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model, app_config = _reasoning_capture_model(
        monkeypatch,
        provider_adapter="openai",
        catalog_effort="high",
    )

    instance = create_chat_model(
        name=model.name,
        thinking_enabled=True,
        reasoning_effort="medium",
        app_config=app_config,
        attach_tracing=False,
    )

    assert instance.kwargs["reasoning_effort"] == "medium"


def test_shared_factory_rejects_unregistered_runtime_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetryAwareModel:
        model_fields = {
            "max_retries": SimpleNamespace(alias=None),
        }

    monkeypatch.setattr(
        "deerflow.models.factory.resolve_class",
        lambda *_args, **_kwargs: RetryAwareModel,
    )
    model = ModelConfig(
        name="runtime-model",
        display_name="Runtime model",
        description="",
        use="example:RetryAwareModel",
        model="provider-model",
        max_input_tokens=64_000,
    )
    app_config = AppConfig(
        models=[model],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
    )

    with pytest.raises(RuntimeModelSettingsUnsupported):
        create_chat_model(
            name=model.name,
            app_config=app_config,
            attach_tracing=False,
            runtime_overrides={"timeout": 1},
        )


@pytest.mark.anyio
async def test_oneshot_llm_defaults_to_private_runtime_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    app_config = SimpleNamespace()

    class FakeRuntime:
        def __init__(self, *, app_config: object) -> None:
            observed["app_config"] = app_config

        async def ainvoke(
            self,
            messages: object,
            **kwargs: object,
        ) -> AIMessage:
            observed["messages"] = messages
            observed["invoke_kwargs"] = kwargs
            return AIMessage(content="one-shot response")

    monkeypatch.setattr(
        "deerflow.utils.oneshot_llm.ModelRuntime",
        FakeRuntime,
    )

    result = await run_oneshot_llm(
        system_instruction="system",
        user_content="user",
        run_name="admin-probe",
        app_config=app_config,  # type: ignore[arg-type]
        model_name="model-id",
    )

    assert result == "one-shot response"
    assert observed["app_config"] is app_config
    assert observed["invoke_kwargs"] == {
        "profile": ModelRuntimeProfile.PRIVATE_ONESHOT,
        "model_name": "model-id",
        "thinking_enabled": False,
        "reasoning_effort": None,
        "model_overrides": None,
        "config": {"run_name": "admin-probe"},
        "deadline_monotonic": None,
        "abort_event": None,
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("chunks", "expected_reasoning", "expected_output"),
    [
        (
            [
                AIMessageChunk(
                    content="",
                    additional_kwargs={"reasoning_content": "结构化思考"},
                ),
                AIMessageChunk(content='{"decision":"candidate"}'),
            ],
            "结构化思考",
            '{"decision":"candidate"}',
        ),
        (
            [
                AIMessageChunk(
                    content=[{"type": "thinking", "thinking": "内容块思考"}],
                ),
                AIMessageChunk(content='{"decision":"candidate"}'),
            ],
            "内容块思考",
            '{"decision":"candidate"}',
        ),
        (
            [
                AIMessageChunk(content="<thi"),
                AIMessageChunk(content="nk>行内思考</think>"),
                AIMessageChunk(content='{"decision":"candidate"}'),
            ],
            "行内思考",
            '<think>行内思考</think>{"decision":"candidate"}',
        ),
    ],
)
async def test_oneshot_llm_streams_only_real_provider_reasoning(
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[AIMessageChunk],
    expected_reasoning: str,
    expected_output: str,
) -> None:
    class FakeRuntime:
        def __init__(self, *, app_config: object) -> None:
            del app_config

        async def astream(self, *_args: object, **_kwargs: object):
            for chunk in chunks:
                yield chunk

    monkeypatch.setattr("deerflow.utils.oneshot_llm.ModelRuntime", FakeRuntime)
    observed: list[str] = []

    async def record_reasoning(value: str) -> None:
        observed.append(value)

    result = await run_oneshot_llm(
        system_instruction="system",
        user_content="user",
        run_name="builder-stream",
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        on_reasoning_delta=record_reasoning,
    )

    assert "".join(observed) == expected_reasoning
    assert result == expected_output


@pytest.mark.anyio
async def test_oneshot_llm_does_not_invent_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRuntime:
        def __init__(self, *, app_config: object) -> None:
            del app_config

        async def astream(self, *_args: object, **_kwargs: object):
            yield AIMessageChunk(content='{"decision":"candidate"}')

    monkeypatch.setattr("deerflow.utils.oneshot_llm.ModelRuntime", FakeRuntime)
    observed: list[str] = []

    async def record_reasoning(value: str) -> None:
        observed.append(value)

    await run_oneshot_llm(
        system_instruction="system",
        user_content="user",
        run_name="builder-no-reasoning",
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        on_reasoning_delta=record_reasoning,
    )

    assert observed == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "message",
    [
        SystemMessage(
            content="<think>system inline content</think>system response",
            additional_kwargs={"reasoning_content": "system structured content"},
        ),
        ToolMessage(
            content="<think>tool inline content</think>tool response",
            tool_call_id="tool-call-1",
            additional_kwargs={"reasoning_content": "tool structured content"},
        ),
    ],
    ids=("system-message", "tool-message"),
)
async def test_oneshot_llm_projects_reasoning_from_ai_messages_only(
    monkeypatch: pytest.MonkeyPatch,
    message: SystemMessage | ToolMessage,
) -> None:
    class FakeRuntime:
        def __init__(self, *, app_config: object) -> None:
            del app_config

        async def astream(self, *_args: object, **_kwargs: object):
            yield message

    monkeypatch.setattr("deerflow.utils.oneshot_llm.ModelRuntime", FakeRuntime)
    observed: list[str] = []

    async def record_reasoning(value: str) -> None:
        observed.append(value)

    await run_oneshot_llm(
        system_instruction="system",
        user_content="user",
        run_name="builder-non-ai-message",
        app_config=SimpleNamespace(),  # type: ignore[arg-type]
        on_reasoning_delta=record_reasoning,
    )

    assert observed == []


@pytest.mark.anyio
async def test_oneshot_llm_flushes_a_short_reasoning_delta_while_provider_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_response = asyncio.Event()
    reasoning_flushed = asyncio.Event()

    class FakeRuntime:
        def __init__(self, *, app_config: object) -> None:
            del app_config

        async def astream(self, *_args: object, **_kwargs: object):
            yield AIMessageChunk(
                content="",
                additional_kwargs={"reasoning_content": "短思考"},
            )
            await release_response.wait()
            yield AIMessageChunk(content='{"decision":"candidate"}')

    monkeypatch.setattr("deerflow.utils.oneshot_llm.ModelRuntime", FakeRuntime)
    observed: list[str] = []

    async def record_reasoning(value: str) -> None:
        observed.append(value)
        reasoning_flushed.set()

    task = asyncio.create_task(
        run_oneshot_llm(
            system_instruction="system",
            user_content="user",
            run_name="builder-short-reasoning",
            app_config=SimpleNamespace(),  # type: ignore[arg-type]
            on_reasoning_delta=record_reasoning,
        )
    )
    await asyncio.wait_for(reasoning_flushed.wait(), timeout=0.5)
    assert observed == ["短思考"]
    assert not task.done()
    release_response.set()
    assert await task == '{"decision":"candidate"}'
