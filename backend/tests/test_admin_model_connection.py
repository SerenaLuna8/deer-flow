from __future__ import annotations

from threading import Event
from types import SimpleNamespace

import pytest

from app.gateway.system_model_callers import ModelConnectionTester
from app.system_settings.models import SystemModelConnectionCheck
from app.system_settings.validation import (
    ModelSettingsInvalid,
    validate_system_model_connection_test,
)


class _RuntimeConfig:
    def __init__(self) -> None:
        self.models: tuple[object, ...] = ()

    def with_runtime_models(self, models: tuple[object, ...]) -> _RuntimeConfig:
        self.models = models
        return self


@pytest.mark.anyio
async def test_model_connection_tester_uses_one_untraced_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _RuntimeConfig()
    observed: dict[str, object] = {}

    async def probe(**kwargs: object) -> str:
        observed.update(kwargs)
        return "OK"

    monkeypatch.setattr(
        "app.gateway.system_model_callers.run_oneshot_llm",
        probe,
    )

    connected = await ModelConnectionTester(config).test(
        SimpleNamespace(name="model-connection-test"),
    )

    assert connected is True
    assert config.models[0].name == "model-connection-test"
    assert observed == {
        "system_instruction": "You are a connectivity probe. Reply with OK.",
        "user_content": "OK",
        "run_name": "admin_model_connection_test",
        "app_config": config,
        "model_name": "model-connection-test",
        "thread_id": None,
        "attach_tracing": False,
    }


@pytest.mark.anyio
async def test_model_connection_tester_hides_provider_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_probe(**_kwargs: object) -> str:
        raise RuntimeError("provider failed")

    monkeypatch.setattr(
        "app.gateway.system_model_callers.run_oneshot_llm",
        failing_probe,
    )

    connected = await ModelConnectionTester(_RuntimeConfig()).test(
        SimpleNamespace(name="model-connection-test"),
    )

    assert connected is False


@pytest.mark.anyio
async def test_real_vision_connection_test_uses_synthetic_image_and_narrow_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class ProbeClient:
        async def analyze(self, **kwargs: object) -> object:
            observed.update(kwargs)
            return object()

    def client_factory(
        model: object,
        contract_version: str,
        *,
        transient_gate_key: str,
    ) -> ProbeClient:
        observed["model"] = model
        observed["contract_version"] = contract_version
        observed["gate_key"] = transient_gate_key
        return ProbeClient()

    monkeypatch.setattr(
        "app.gateway.system_model_callers.build_vision_evidence_client",
        client_factory,
    )
    model = SimpleNamespace(
        name="vision-probe",
        system_provider_adapter="openai",
        supports_vision=True,
        base_url="https://responses.example.test/v1",
        use_responses_api=True,
    )

    connected = await ModelConnectionTester(_RuntimeConfig()).test(model)

    assert connected is True
    assert observed["model"] is model
    assert observed["contract_version"] == "vision.bridge.v1"
    assert observed["gate_key"] == "admin-vision-connection-test"
    assert bytes(observed["image_bytes"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert observed["mime_type"] == "image/png"
    assert observed["mode"] == "auto"
    assert isinstance(observed["deadline_monotonic"], float)
    assert isinstance(observed["abort_signal"], Event)


def test_connection_test_reuses_provider_and_credential_validation() -> None:
    command = SystemModelConnectionCheck(
        provider_adapter="codex_cli",
        provider_model="gpt-5.2",
        settings={"reasoning_effort": "minimal"},
        credential_id=None,
        credential_version_id=None,
        credential_env_key=None,
    )

    assert validate_system_model_connection_test(command) == command

    with pytest.raises(ModelSettingsInvalid):
        validate_system_model_connection_test(
            SystemModelConnectionCheck(
                provider_adapter="openai",
                provider_model="gpt-5.2",
                settings={},
                credential_id=None,
                credential_version_id=None,
                credential_env_key=None,
            ),
        )
