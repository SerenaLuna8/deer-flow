from __future__ import annotations

import ast
import base64
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.audit.models import resolve_system_audit_context
from app.gateway.deps import (
    get_config,
    get_system_model_catalog,
    get_system_model_materializer,
)
from app.gateway.routers import admin_model_settings
from app.gateway.system_model_callers import ModelConnectionTester
from app.model_registry import gateway as model_registry_gateway
from app.model_registry.secrets import protect_provider_api_key
from app.system_settings import SystemModelMaterializer
from app.system_settings.models import (
    ConnectionTestSystemModelMaterial,
    SystemModelConnectionCheck,
)
from app.system_settings.service import SystemModelCatalogService
from app.system_settings.validation import (
    ModelSettingsInvalid,
    validate_system_model_connection_test,
)
from deerflow.models import ModelRuntimeProfile

sys.path.insert(0, str(Path(__file__).resolve().parent / "knowledge"))
from registry_helpers import registry_secret_key  # noqa: E402

_PROVIDER_ID = uuid.UUID("00000000-0000-4000-8000-00000000f101")
_PROVIDER_BASE_URL = "https://provider.connection.invalid/v1"
_PROVIDER_STORED_KEY = "sk-stored-provider-key"


def _fake_provider() -> SimpleNamespace:
    envelope = protect_provider_api_key(
        provider_id=_PROVIDER_ID,
        base_url=_PROVIDER_BASE_URL,
        api_key=_PROVIDER_STORED_KEY,
        key=registry_secret_key(),
    )
    return SimpleNamespace(
        id=_PROVIDER_ID,
        name="Connection Test Provider",
        base_url=_PROVIDER_BASE_URL,
        api_key_nonce=envelope.nonce,
        api_key_ciphertext=envelope.ciphertext,
    )


class _RuntimeConfig:
    def __init__(self) -> None:
        self.models: tuple[object, ...] = ()

    def with_runtime_models(self, models: tuple[object, ...]) -> _RuntimeConfig:
        self.models = models
        return self


def test_gateway_model_callers_do_not_import_vision_protocol_implementations() -> None:
    path = Path(__file__).resolve().parents[1] / "app/gateway/system_model_callers.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}

    assert not any(module.startswith("deerflow.vision") for module in imported_modules)


class _FakeRepository:
    async def lock_provider_for_share(self, provider_id: uuid.UUID):
        assert provider_id == _PROVIDER_ID
        return _fake_provider()


class _ConnectionTestService(SystemModelCatalogService):
    def __init__(self) -> None:
        super().__init__(lambda: None, secret_key=registry_secret_key())  # type: ignore[arg-type]

    async def _admin_operation(self, context: object, operation):  # type: ignore[no-untyped-def]
        return await operation(_FakeRepository(), self._require_admin(context))


@pytest.mark.anyio
async def test_model_connection_tester_uses_closed_text_probe_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _RuntimeConfig()
    observed: dict[str, object] = {}

    class Runtime:
        def __init__(self, *, app_config: object) -> None:
            observed["app_config"] = app_config

        async def ainvoke(self, messages: object, **kwargs: object) -> AIMessage:
            observed["messages"] = messages
            observed["invoke_kwargs"] = kwargs
            return AIMessage(content="OK")

    monkeypatch.setattr("app.gateway.system_model_callers.ModelRuntime", Runtime)

    connected = await ModelConnectionTester(config).test(
        SimpleNamespace(
            name="model-connection-test",
            supports_vision=False,
        ),
    )

    assert connected is True
    assert config.models[0].name == "model-connection-test"
    messages = observed["messages"]
    assert isinstance(messages, list)
    assert messages == [
        SystemMessage(content="You are a connectivity probe. Reply with OK."),
        HumanMessage(content="OK"),
    ]
    invoke_kwargs = observed["invoke_kwargs"]
    assert isinstance(invoke_kwargs, dict)
    assert invoke_kwargs["profile"] is ModelRuntimeProfile.ADMIN_PROBE
    assert invoke_kwargs["model_name"] == "model-connection-test"
    assert invoke_kwargs["config"] == {
        "run_name": "admin_model_connection_test",
    }
    assert isinstance(invoke_kwargs["deadline_monotonic"], float)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [RuntimeError("provider failed"), TimeoutError()])
async def test_model_connection_tester_hides_provider_failures_and_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    class Runtime:
        def __init__(self, *, app_config: object) -> None:
            del app_config

        async def ainvoke(self, *_args: object, **_kwargs: object) -> AIMessage:
            raise failure

    monkeypatch.setattr("app.gateway.system_model_callers.ModelRuntime", Runtime)

    connected = await ModelConnectionTester(_RuntimeConfig()).test(
        SimpleNamespace(
            name="model-connection-test",
            supports_vision=False,
        ),
    )

    assert connected is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("provider_adapter", "use_responses_api"),
    [
        ("openai", False),
        ("openai", True),
        ("anthropic", None),
    ],
    ids=["openai-chat", "openai-responses", "anthropic"],
)
async def test_visual_connection_test_uses_adapter_neutral_multimodal_message(
    monkeypatch: pytest.MonkeyPatch,
    provider_adapter: str,
    use_responses_api: bool | None,
) -> None:
    observed: dict[str, object] = {}

    class ChatModel:
        async def ainvoke(
            self,
            messages: object,
            config: object = None,
        ) -> AIMessage:
            observed["messages"] = messages
            observed["invoke_config"] = config
            return AIMessage(content="OK")

    def model_factory(**kwargs: object) -> ChatModel:
        observed["factory_kwargs"] = kwargs
        return ChatModel()

    monkeypatch.setattr("deerflow.models.runtime.create_chat_model", model_factory)
    model = SimpleNamespace(
        name="vision-probe",
        system_provider_adapter=provider_adapter,
        supports_vision=True,
        use_responses_api=use_responses_api,
    )
    config = _RuntimeConfig()

    connected = await ModelConnectionTester(config).test(model)

    assert connected is True
    assert config.models == (model,)
    factory_kwargs = observed["factory_kwargs"]
    assert isinstance(factory_kwargs, dict)
    assert factory_kwargs["app_config"] is config
    assert factory_kwargs["name"] == "vision-probe"
    assert factory_kwargs["attach_tracing"] is False
    assert factory_kwargs["runtime_overrides"] == {"max_retries": 0}
    assert observed["invoke_config"] == {
        "callbacks": [],
        "run_name": "admin_model_connection_test",
    }
    messages = observed["messages"]
    assert isinstance(messages, list)
    assert len(messages) == 2
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    blocks = messages[1].content
    assert isinstance(blocks, list)
    assert blocks[0] == {
        "type": "text",
        "text": "Inspect this platform-generated image and reply with OK.",
    }
    image = blocks[1]
    assert image["type"] == "image"
    assert image["mime_type"] == "image/png"
    payload = base64.b64decode(image["base64"], validate=True)
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert int.from_bytes(payload[16:20], "big") == 64
    assert int.from_bytes(payload[20:24], "big") == 64


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("supports_vision", "expected_probe"),
    [
        (True, "vision"),
        (False, "text"),
    ],
)
async def test_admin_model_connection_route_uses_requested_probe_profile(
    monkeypatch: pytest.MonkeyPatch,
    supports_vision: bool,
    expected_probe: str,
) -> None:
    request_id = "admin-model-connection-route"
    service = _ConnectionTestService()
    runtime_config = _RuntimeConfig()
    observed: list[str] = []

    class Runtime:
        def __init__(self, *, app_config: object) -> None:
            self.app_config = app_config

        async def ainvoke(self, messages: object, **kwargs: object) -> AIMessage:
            del kwargs
            assert isinstance(messages, list)
            human = messages[-1]
            assert isinstance(human, HumanMessage)
            observed.append("vision" if isinstance(human.content, list) else "text")
            return AIMessage(content="OK")

    monkeypatch.setattr("app.gateway.system_model_callers.ModelRuntime", Runtime)

    context = resolve_system_audit_context(
        SimpleNamespace(id=uuid.uuid4(), system_role="system_admin"),
        request_id=request_id,
    )
    app = FastAPI()
    app.include_router(admin_model_settings.router)
    app.dependency_overrides[admin_model_settings.current_model_admin_context] = lambda: context
    app.dependency_overrides[get_system_model_catalog] = lambda: service
    app.dependency_overrides[get_system_model_materializer] = lambda: SystemModelMaterializer(lambda: None)  # type: ignore[arg-type]
    app.dependency_overrides[get_config] = lambda: runtime_config

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/admin/settings/models/test-connection",
            json={
                "provider_id": str(_PROVIDER_ID),
                "provider_adapter": "vision_bridge_fake",
                "provider_model": "vision-test",
                "max_input_tokens": 64_000,
                "settings": {},
                "supports_vision": supports_vision,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "succeeded",
        "request_id": request_id,
    }
    assert observed == [expected_probe]


@pytest.mark.anyio
async def test_admin_stored_key_connection_route_materializes_the_provider_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored-Key test derives URL and Key from the Provider without leaks."""

    service = _ConnectionTestService()
    observed: dict[str, object] = {}

    class Runtime:
        def __init__(self, *, app_config: object) -> None:
            model = app_config.models[0]  # type: ignore[attr-defined]
            observed["secret"] = model.api_key.get_secret_value()
            observed["base_url"] = model.base_url

        async def ainvoke(self, messages: object, **_kwargs: object) -> AIMessage:
            del messages
            return AIMessage(content="OK")

    monkeypatch.setattr("app.gateway.system_model_callers.ModelRuntime", Runtime)
    context = resolve_system_audit_context(
        SimpleNamespace(id=uuid.uuid4(), system_role="system_admin"),
        request_id="stored-key-probe",
    )
    materializer = SystemModelMaterializer(lambda: None)  # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(admin_model_settings.router)
    app.dependency_overrides[admin_model_settings.current_model_admin_context] = lambda: context
    app.dependency_overrides[get_system_model_catalog] = lambda: service
    app.dependency_overrides[get_system_model_materializer] = lambda: materializer
    app.dependency_overrides[get_config] = _RuntimeConfig

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/admin/settings/models/test-connection",
            json={
                "provider_id": str(_PROVIDER_ID),
                "provider_adapter": "openai",
                "provider_model": "gpt-5.2",
                "max_input_tokens": 64_000,
                "settings": {},
                "supports_vision": False,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "succeeded",
        "request_id": "stored-key-probe",
    }
    assert observed["secret"] == _PROVIDER_STORED_KEY
    assert observed["base_url"] == _PROVIDER_BASE_URL
    assert _PROVIDER_STORED_KEY not in response.text


@pytest.mark.anyio
async def test_provider_candidate_connection_route_uses_transient_key_without_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate test: explicit URL and transient Key, nothing persisted."""

    secret = "connection-test-secret"
    service = _ConnectionTestService()
    observed: dict[str, object] = {}

    class Runtime:
        def __init__(self, *, app_config: object) -> None:
            model = app_config.models[0]  # type: ignore[attr-defined]
            observed["secret"] = model.api_key.get_secret_value()
            observed["base_url"] = model.base_url

        async def ainvoke(self, messages: object, **_kwargs: object) -> AIMessage:
            assert isinstance(messages, list)
            assert isinstance(messages[-1], HumanMessage)
            assert isinstance(messages[-1].content, list)
            observed["probe"] = "vision"
            return AIMessage(content="OK")

    monkeypatch.setattr("app.gateway.system_model_callers.ModelRuntime", Runtime)
    context = resolve_system_audit_context(
        SimpleNamespace(id=uuid.uuid4(), system_role="system_admin"),
        request_id="transient-key-vision-probe",
    )
    materializer = SystemModelMaterializer(lambda: None)  # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(model_registry_gateway.router)
    app.dependency_overrides[model_registry_gateway.require_model_registry_admin_context] = lambda: context
    app.dependency_overrides[get_system_model_catalog] = lambda: service
    app.dependency_overrides[get_system_model_materializer] = lambda: materializer
    app.dependency_overrides[get_config] = _RuntimeConfig

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/admin/settings/model-providers/test-connection",
            json={
                "base_url": "https://vision.example.test/v1",
                "api_key": secret,
                "provider_adapter": "openai",
                "provider_model": "vision-test",
                "max_input_tokens": 64_000,
                "settings": {},
                "supports_vision": True,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "succeeded",
        "request_id": "transient-key-vision-probe",
    }
    assert observed == {
        "secret": secret,
        "base_url": "https://vision.example.test/v1",
        "probe": "vision",
    }
    assert secret not in response.text


@pytest.mark.parametrize("supports_vision", [False, True])
def test_connection_test_materializer_uses_requested_probe_profile(
    supports_vision: bool,
) -> None:
    from app.system_settings.execution_adapter import SystemModelExecutionAdapter

    command = SystemModelConnectionCheck(
        provider_adapter="vision_bridge_fake",
        provider_model="vision-test",
        max_input_tokens=64_000,
        settings={},
        supports_vision=supports_vision,
        api_key="request-only-test-key",
    )

    model = SystemModelExecutionAdapter().materialize_connection_test(
        ConnectionTestSystemModelMaterial(
            command=command,
        ),
    )

    assert model.supports_vision is supports_vision


def test_connection_test_reuses_provider_and_credential_validation() -> None:
    command = SystemModelConnectionCheck(
        provider_adapter="vision_bridge_fake",
        provider_model="gpt-5.2",
        max_input_tokens=64_000,
        settings={},
        supports_vision=False,
        api_key="request-only-test-key",
    )

    assert validate_system_model_connection_test(command) == command

    with pytest.raises(ModelSettingsInvalid):
        validate_system_model_connection_test(
            SystemModelConnectionCheck(
                provider_adapter="openai",
                provider_model="gpt-5.2",
                max_input_tokens=64_000,
                settings={},
                supports_vision=False,
                api_key="",
            ),
        )
