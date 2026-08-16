from __future__ import annotations

import uuid
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.audit.models import resolve_system_audit_context
from app.gateway.deps import (
    get_config,
    get_system_model_catalog,
    get_system_model_materializer,
)
from app.gateway.routers import admin_model_settings
from app.gateway.system_model_callers import ModelConnectionTester
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


class _RuntimeConfig:
    def __init__(self) -> None:
        self.models: tuple[object, ...] = ()

    def with_runtime_models(self, models: tuple[object, ...]) -> _RuntimeConfig:
        self.models = models
        return self


class _ConnectionTestRepository:
    async def lock_system_credential_reference(
        self,
        credential_id: uuid.UUID | None,
        credential_version_id: uuid.UUID | None,
        credential_env_key: str | None,
        *,
        require_current: bool,
        load_envelope: bool,
    ) -> None:
        assert (credential_id, credential_version_id, credential_env_key) == (
            None,
            None,
            None,
        )
        assert require_current is True
        assert load_envelope is True
        return None


class _CredentialConnectionTestRepository:
    def __init__(self, reference: object) -> None:
        self.reference = reference

    async def lock_system_credential_reference(
        self,
        credential_id: uuid.UUID | None,
        credential_version_id: uuid.UUID | None,
        credential_env_key: str | None,
        *,
        require_current: bool,
        load_envelope: bool,
    ) -> object:
        assert credential_id == self.reference.credential.id  # type: ignore[attr-defined]
        assert credential_version_id == self.reference.version.id  # type: ignore[attr-defined]
        assert credential_env_key == "OPENAI_API_KEY"
        assert require_current is True
        assert load_envelope is True
        return self.reference


class _ConnectionTestService(SystemModelCatalogService):
    def __init__(self, repository: _ConnectionTestRepository) -> None:
        super().__init__(lambda: None)  # type: ignore[arg-type]
        self.repository = repository

    async def _admin_operation(self, context: object, operation):  # type: ignore[no-untyped-def]
        return await operation(self.repository, self._require_admin(context))


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


@pytest.mark.anyio
async def test_visual_connection_test_never_falls_back_to_text_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_probe_called = False

    async def text_probe(**_kwargs: object) -> str:
        nonlocal text_probe_called
        text_probe_called = True
        return "OK"

    monkeypatch.setattr(
        "app.gateway.system_model_callers.run_oneshot_llm",
        text_probe,
    )
    model = SimpleNamespace(
        name="unsupported-vision-probe",
        system_provider_adapter="anthropic",
        supports_vision=True,
        base_url="https://vision.example.test/v1",
        use_responses_api=False,
    )

    connected = await ModelConnectionTester(_RuntimeConfig()).test(model)

    assert connected is False
    assert text_probe_called is False


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
    repository = _ConnectionTestRepository()
    service = _ConnectionTestService(repository)
    runtime_config = _RuntimeConfig()
    observed: list[str] = []

    class ProbeClient:
        async def analyze(self, **_kwargs: object) -> object:
            observed.append("vision")
            return object()

    def client_factory(*_args: object, **_kwargs: object) -> ProbeClient:
        return ProbeClient()

    async def text_probe(**_kwargs: object) -> str:
        observed.append("text")
        return "OK"

    monkeypatch.setattr(
        "app.gateway.system_model_callers.build_vision_evidence_client",
        client_factory,
    )
    monkeypatch.setattr(
        "app.gateway.system_model_callers.run_oneshot_llm",
        text_probe,
    )

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
                "provider_adapter": "vision_bridge_fake",
                "provider_model": "vision-test",
                "settings": {},
                "supports_vision": supports_vision,
                "credential_id": None,
                "credential_version_id": None,
                "credential_env_key": None,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "succeeded",
        "request_id": request_id,
    }
    assert observed == [expected_probe]


@pytest.mark.anyio
async def test_admin_visual_connection_route_materializes_credential_without_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shared_assets.keyring import CredentialKeyring
    from app.system_settings.credential_adapter import SystemModelCredentialAdapter

    credential_id = uuid.uuid4()
    credential_version_id = uuid.uuid4()
    secret = "connection-test-secret"
    reference = SimpleNamespace(
        credential=SimpleNamespace(
            id=credential_id,
            scope="system",
            project_id=None,
            credential_type="model_api_key",
            status="active",
            is_delete=False,
        ),
        version=SimpleNamespace(
            id=credential_version_id,
            credential_id=credential_id,
            payload_schema={"env": ["OPENAI_API_KEY"]},
            status="active",
        ),
        envelope=SimpleNamespace(
            credential_version_id=credential_version_id,
            key_id="test",
            nonce=b"nonce",
            ciphertext=b"ciphertext",
            is_active=True,
        ),
    )
    service = _ConnectionTestService(
        _CredentialConnectionTestRepository(reference),  # type: ignore[arg-type]
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        "app.system_settings.credential_adapter.decrypt_credential_payload",
        lambda *_args, **_kwargs: {"env": {"OPENAI_API_KEY": secret}},
    )

    class ProbeClient:
        async def analyze(self, **_kwargs: object) -> object:
            observed["probe"] = "vision"
            return object()

    def client_factory(model: object, *_args: object, **_kwargs: object) -> ProbeClient:
        observed["secret"] = model.api_key.get_secret_value()  # type: ignore[attr-defined]
        return ProbeClient()

    monkeypatch.setattr(
        "app.gateway.system_model_callers.build_vision_evidence_client",
        client_factory,
    )
    context = resolve_system_audit_context(
        SimpleNamespace(id=uuid.uuid4(), system_role="system_admin"),
        request_id="credential-vision-probe",
    )
    materializer = SystemModelMaterializer(
        lambda: None,  # type: ignore[arg-type]
        credential_adapter=SystemModelCredentialAdapter(
            keyring=CredentialKeyring(
                active_key_id="test",
                _keys={"test": b"k" * 32},
            ),
        ),
    )
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
                "provider_adapter": "openai",
                "provider_model": "vision-test",
                "settings": {"base_url": "https://vision.example.test/v1"},
                "supports_vision": True,
                "credential_id": str(credential_id),
                "credential_version_id": str(credential_version_id),
                "credential_env_key": "OPENAI_API_KEY",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "succeeded",
        "request_id": "credential-vision-probe",
    }
    assert observed == {"secret": secret, "probe": "vision"}
    assert secret not in response.text


@pytest.mark.parametrize("supports_vision", [False, True])
def test_connection_test_materializer_uses_requested_probe_profile(
    supports_vision: bool,
) -> None:
    from app.system_settings.credential_adapter import SystemModelCredentialAdapter

    command = SystemModelConnectionCheck(
        provider_adapter="vision_bridge_fake",
        provider_model="vision-test",
        settings={},
        supports_vision=supports_vision,
        credential_id=None,
        credential_version_id=None,
        credential_env_key=None,
    )

    model = SystemModelCredentialAdapter().materialize_connection_test(
        ConnectionTestSystemModelMaterial(
            command=command,
        ),
    )

    assert model.supports_vision is supports_vision


def test_connection_test_reuses_provider_and_credential_validation() -> None:
    command = SystemModelConnectionCheck(
        provider_adapter="vision_bridge_fake",
        provider_model="gpt-5.2",
        settings={},
        supports_vision=False,
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
                supports_vision=False,
                credential_id=None,
                credential_version_id=None,
                credential_env_key=None,
            ),
        )
