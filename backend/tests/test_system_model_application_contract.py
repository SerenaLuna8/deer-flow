from __future__ import annotations

import sys
import uuid
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.audit.models import resolve_system_audit_context
from app.gateway.routers.admin_model_settings import (
    AdminModelConnectionTestRequest,
    AdminModelCreateRequest,
    AdminModelDefaultRequest,
    AdminModelItemResponse,
    AdminModelStatusRequest,
    AdminModelUpdateRequest,
)
from app.gateway.routers.models import ModelResponse, _public_response
from app.model_registry.secrets import protect_provider_api_key
from app.system_settings.errors import SystemModelInvalid
from app.system_settings.models import (
    CreateSystemModel,
    PublicSystemModelView,
    RunModelConfigSnapshotView,
    SystemModelView,
)
from app.system_settings.repository import SystemModelRepository
from app.system_settings.service import SystemModelCatalogService

sys.path.insert(0, str(Path(__file__).resolve().parent / "knowledge"))
from registry_helpers import registry_secret_key  # noqa: E402

_PROVIDER_ID = uuid.UUID("00000000-0000-4000-8000-00000000f001")
_PROVIDER_NAME = "Contract Test Provider"
_PROVIDER_BASE_URL = "https://provider.contract.invalid/v1"


def _fake_provider() -> SimpleNamespace:
    envelope = protect_provider_api_key(
        provider_id=_PROVIDER_ID,
        base_url=_PROVIDER_BASE_URL,
        api_key="sk-contract-provider-key",
        key=registry_secret_key(),
    )
    return SimpleNamespace(
        id=_PROVIDER_ID,
        name=_PROVIDER_NAME,
        base_url=_PROVIDER_BASE_URL,
        api_key_nonce=envelope.nonce,
        api_key_ciphertext=envelope.ciphertext,
    )


def _admin_context():
    return resolve_system_audit_context(
        SimpleNamespace(
            id=uuid.UUID("00000000-0000-4000-8000-000000000a01"),
            system_role="system_admin",
        ),
        request_id="system-model-application-contract",
    )


class _AdminOperationService(SystemModelCatalogService):
    def __init__(self, repository: object) -> None:
        super().__init__(lambda: None, secret_key=registry_secret_key())  # type: ignore[arg-type]
        self.repository = repository

    async def _admin_operation(self, context: object, operation):  # type: ignore[no-untyped-def]
        return await operation(self.repository, self._require_admin(context))


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.deleted: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)

    async def delete(self, row: object) -> None:
        self.deleted.append(row)

    async def flush(self) -> None:
        return None


@pytest.mark.anyio
async def test_admin_create_uses_the_model_uuid_as_its_only_stable_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id = uuid.UUID("00000000-0000-4000-8000-000000000a02")
    real_uuid4 = uuid.uuid4
    generated = iter((model_id,))

    def fake_uuid4() -> uuid.UUID:
        # First draw is the model identity; later draws (for example the
        # secret generation ID) stay unique.
        return next(generated, None) or real_uuid4()

    monkeypatch.setattr("app.system_settings.service.uuid.uuid4", fake_uuid4)
    state = SimpleNamespace(
        revision=1,
        default_model_config_id=None,
        updated_by_user_id=None,
    )
    provider = _fake_provider()

    class Repository:
        session = _FakeSession()
        created_model = None

        async def catalog_state(self, *, for_update: bool = False):
            assert for_update is True
            return state

        async def lock_provider_for_share(self, provider_id: uuid.UUID):
            assert provider_id == _PROVIDER_ID
            return provider

        async def add_model(self, model) -> None:  # type: ignore[no-untyped-def]
            self.created_model = model

    repository = Repository()
    created = await _AdminOperationService(repository).create_model(
        _admin_context(),
        CreateSystemModel(
            display_name="Shared display name",
            status="active",
            provider_id=_PROVIDER_ID,
            provider_adapter="vision_bridge_fake",
            provider_model="vision-bridge-fake-v1",
            max_input_tokens=64_000,
            settings={},
            supports_thinking=False,
            supports_reasoning_effort=False,
            supports_vision=True,
        ),
    )

    assert repository.created_model.id == model_id
    assert repository.created_model.max_input_tokens == 64_000
    # The server derives base_url from the bound Provider and immediately
    # protects the Provider Key as the model's first secret generation.
    assert repository.created_model.settings["base_url"] == _PROVIDER_BASE_URL
    assert repository.created_model.current_secret_generation_id is not None
    assert repository.created_model.secret_revision == 1
    assert created.id == model_id
    assert created.max_input_tokens == 64_000
    assert created.provider_id == _PROVIDER_ID
    assert created.provider_name == _PROVIDER_NAME
    assert state.default_model_config_id == model_id


def test_admin_and_public_api_contracts_omit_removed_model_metadata() -> None:
    for contract in (
        CreateSystemModel,
        SystemModelView,
        PublicSystemModelView,
        RunModelConfigSnapshotView,
    ):
        names = {item.name for item in fields(contract)}
        assert "logical_name" not in names
        assert "description" not in names
        assert "sort_order" not in names
    for contract in (
        AdminModelCreateRequest,
        AdminModelUpdateRequest,
        AdminModelItemResponse,
    ):
        assert "logical_name" not in contract.model_fields
        assert "description" not in contract.model_fields
        assert "sort_order" not in contract.model_fields
    assert "description" not in ModelResponse.model_fields

    with pytest.raises(ValidationError):
        AdminModelCreateRequest.model_validate(
            {
                "logical_name": "operator-supplied-name",
                "display_name": "Display name",
                "status": "suspended",
                "provider_adapter": "vision_bridge_fake",
                "provider_model": "vision-bridge-fake-v1",
                "max_input_tokens": 64_000,
                "settings": {},
                "supports_thinking": False,
                "supports_reasoning_effort": False,
                "supports_vision": True,
                "credential_id": None,
                "credential_version_id": None,
                "credential_env_key": None,
            }
        )


def test_admin_model_api_binds_a_provider_and_carries_no_model_level_key() -> None:
    created = AdminModelCreateRequest.model_validate(
        {
            "display_name": "DeepSeek Flash",
            "status": "active",
            "provider_id": str(_PROVIDER_ID),
            "provider_adapter": "deepseek",
            "provider_model": "deepseek-v4-flash",
            "max_input_tokens": 64_000,
            "settings": {},
        }
    )
    assert created.provider_id == _PROVIDER_ID

    request_fields = set(AdminModelCreateRequest.model_fields) | set(AdminModelUpdateRequest.model_fields)
    response_fields = set(AdminModelItemResponse.model_fields)
    for removed in (
        "credential_id",
        "credential_version_id",
        "credential_env_key",
        "version_number",
        "current_version_id",
        # API Keys are Provider-owned now: authoring never carries one and
        # the item response only reports configuration state.
        "api_key",
    ):
        assert removed not in request_fields
        assert removed not in response_fields
    assert "provider_id" in AdminModelCreateRequest.model_fields
    assert "provider_id" in AdminModelUpdateRequest.model_fields
    assert {
        "api_key_configured",
        "secret_readiness",
        "secret_revision",
        "provider_id",
        "provider_name",
    }.issubset(response_fields)
    assert "expected_revision" not in AdminModelUpdateRequest.model_fields
    assert "expected_revision" not in AdminModelStatusRequest.model_fields
    assert "expected_catalog_revision" not in AdminModelDefaultRequest.model_fields

    # The stored-Key connection test names a Provider and rejects a submitted
    # transient Key entirely.
    assert "provider_id" in AdminModelConnectionTestRequest.model_fields
    assert "api_key" not in AdminModelConnectionTestRequest.model_fields
    with pytest.raises(ValidationError):
        AdminModelConnectionTestRequest.model_validate(
            {
                "provider_adapter": "deepseek",
                "provider_model": "deepseek-v4-flash",
                "max_input_tokens": 64_000,
                "settings": {},
                "supports_vision": False,
            }
        )
    with pytest.raises(ValidationError):
        AdminModelConnectionTestRequest.model_validate(
            {
                "provider_id": str(_PROVIDER_ID),
                "provider_adapter": "deepseek",
                "provider_model": "deepseek-v4-flash",
                "max_input_tokens": 64_000,
                "settings": {},
                "supports_vision": False,
                "api_key": "transient-test-key",
            }
        )


def test_admin_model_max_input_tokens_is_required_and_bounded() -> None:
    payload = {
        "display_name": "DeepSeek Flash",
        "status": "active",
        "provider_id": str(_PROVIDER_ID),
        "provider_adapter": "deepseek",
        "provider_model": "deepseek-v4-flash",
        "settings": {},
        "max_input_tokens": 64_000,
    }

    created = AdminModelCreateRequest.model_validate(payload)

    assert created.max_input_tokens == 64_000
    for contract in (
        AdminModelCreateRequest,
        AdminModelUpdateRequest,
        AdminModelConnectionTestRequest,
        AdminModelItemResponse,
    ):
        assert contract.model_fields["max_input_tokens"].is_required()
    with pytest.raises(ValidationError):
        AdminModelCreateRequest.model_validate({key: value for key, value in payload.items() if key != "max_input_tokens"})
    for invalid in (0, 2_000_001, True, 1.5):
        with pytest.raises(ValidationError):
            AdminModelCreateRequest.model_validate({**payload, "max_input_tokens": invalid})


@pytest.mark.anyio
async def test_unready_active_model_is_not_auto_selected_or_accepted_as_default() -> None:
    model_id = uuid.UUID("00000000-0000-4000-8000-000000000a05")
    state = SimpleNamespace(
        revision=1,
        default_model_config_id=None,
        updated_by_user_id=None,
        updated_at=None,
    )
    model = SimpleNamespace(
        id=model_id,
        display_name="Unready model",
        status="suspended",
        provider_id=_PROVIDER_ID,
        provider_adapter="deepseek",
        provider_model="deepseek-v4-flash",
        max_input_tokens=64_000,
        settings={"base_url": "https://api.deepseek.com"},
        supports_thinking=False,
        supports_reasoning_effort=False,
        supports_vision=False,
        payload_checksum="a" * 64,
        current_secret_generation_id=None,
        secret_revision=0,
        revision=1,
        created_by_user_id=str(_admin_context().user_id),
        updated_by_user_id=str(_admin_context().user_id),
        created_at=None,
        updated_at=None,
    )

    class Repository:
        session = _FakeSession()

        async def catalog_state(self, *, for_update: bool = False):
            assert for_update is True
            return state

        async def provider_names(self) -> dict[uuid.UUID, str]:
            return {_PROVIDER_ID: _PROVIDER_NAME}

        async def lock_model(self, requested_id: uuid.UUID):
            assert requested_id == model_id
            return model

    service = _AdminOperationService(Repository())
    activated = await service.set_status(_admin_context(), model_id, "active")

    assert activated.secret_readiness == "unready"
    assert state.default_model_config_id is None
    with pytest.raises(SystemModelInvalid):
        await service.set_default(_admin_context(), model_id)
    assert state.default_model_config_id is None


def test_public_api_projects_the_model_uuid_without_provider_identifiers() -> None:
    model_ref = "00000000-0000-4000-8000-000000000a04"

    response = _public_response(
        PublicSystemModelView(
            model_ref=model_ref,
            display_name="Shared display name",
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_vision=False,
            supports_vision_bridge=False,
            is_default=True,
        )
    )

    assert response.name == model_ref
    assert response.model == model_ref
    assert response.display_name == "Shared display name"


@pytest.mark.anyio
async def test_repository_orders_models_by_created_time_then_id_descending() -> None:
    captured: list[object] = []

    class Result:
        def scalars(self):
            return self

        def all(self):
            return ()

    class Session:
        async def execute(self, statement):  # type: ignore[no-untyped-def]
            captured.append(statement)
            return Result()

    assert (
        await SystemModelRepository(Session()).list_models(  # type: ignore[arg-type]
            active_only=True,
        )
        == ()
    )
    sql = " ".join(str(captured[0]).split())
    assert "ORDER BY system_model_configs.created_at DESC, system_model_configs.id DESC" in sql


@pytest.mark.anyio
async def test_repository_resolves_only_canonical_model_uuid_references() -> None:
    captured: list[object] = []

    class Result:
        def scalar_one_or_none(self):
            return None

    class Session:
        async def execute(self, statement):  # type: ignore[no-untyped-def]
            captured.append(statement)
            return Result()

    class Repository(SystemModelRepository):
        async def catalog_state(self, *, for_update: bool = False):
            assert for_update is False
            return SimpleNamespace(default_model_config_id=None)

    repository = Repository(Session())  # type: ignore[arg-type]
    model_id = uuid.UUID("00000000-0000-4000-8000-000000000a10")

    assert (
        await repository.resolve_active_model(
            "legacy-logical-name",
            load_secret=False,
        )
        is None
    )
    assert (
        await repository.resolve_active_model(
            str(model_id).upper(),
            load_secret=False,
        )
        is None
    )
    assert captured == []

    assert (
        await repository.resolve_active_model(
            str(model_id),
            load_secret=False,
        )
        is None
    )
    assert len(captured) == 1
    sql = " ".join(str(captured[0]).split())
    assert "system_model_configs.id =" in sql
    assert "logical_name" not in sql


@pytest.mark.anyio
async def test_public_catalog_keeps_default_first_then_repository_time_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = tuple(
        uuid.UUID(value)
        for value in (
            "00000000-0000-4000-8000-000000000a11",
            "00000000-0000-4000-8000-000000000a12",
            "00000000-0000-4000-8000-000000000a13",
        )
    )

    def model(index: int):
        return SimpleNamespace(
            id=ids[index],
            display_name=f"Model {index}",
            provider_adapter="openai",
            supports_thinking=False,
            supports_reasoning_effort=False,
            supports_vision=False,
            current_secret_generation_id=uuid.uuid4(),
        )

    repository_rows = (
        model(0),
        model(1),
        model(2),
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def begin(self):
            return self

    class Repository:
        def __init__(self, _session: Session) -> None:
            pass

        async def catalog_state(self):
            return SimpleNamespace(default_model_config_id=ids[1])

        async def list_models(self, *, active_only: bool):
            assert active_only is True
            return repository_rows

    monkeypatch.setattr(
        "app.system_settings.service.SystemModelRepository",
        Repository,
    )

    catalog = await SystemModelCatalogService(Session).list_available_models()

    assert [item.model_ref for item in catalog] == [
        str(ids[1]),
        str(ids[0]),
        str(ids[2]),
    ]


@pytest.mark.anyio
async def test_public_catalog_projects_vision_capability_without_a_second_protocol_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = tuple(uuid.uuid4() for _ in range(5))
    adapters = (
        (
            "openai",
            {
                "base_url": "https://responses.example.test/v1",
                "use_responses_api": True,
            },
            True,
        ),
        ("anthropic", {}, True),
        ("vllm", {}, True),
        ("openai", {}, False),
        (
            "vision_openai_compatible_v1",
            {"base_url": "https://legacy.example.test/v1"},
            True,
        ),
    )
    repository_rows = tuple(
        SimpleNamespace(
            id=model_id,
            display_name=f"Model {index}",
            provider_adapter=provider_adapter,
            settings=settings,
            supports_thinking=False,
            supports_reasoning_effort=False,
            supports_vision=supports_vision,
            current_secret_generation_id=uuid.uuid4(),
        )
        for index, (model_id, (provider_adapter, settings, supports_vision)) in enumerate(zip(ids, adapters, strict=True))
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def begin(self):
            return self

    class Repository:
        def __init__(self, _session: Session) -> None:
            pass

        async def catalog_state(self):
            return SimpleNamespace(default_model_config_id=ids[0])

        async def list_models(self, *, active_only: bool):
            assert active_only is True
            return repository_rows

    monkeypatch.setattr(
        "app.system_settings.service.SystemModelRepository",
        Repository,
    )

    catalog = await SystemModelCatalogService(Session).list_available_models()

    assert [item.model_ref for item in catalog] == [str(model_id) for model_id in ids[:4]]
    assert [item.supports_vision_bridge for item in catalog] == [
        True,
        True,
        True,
        False,
    ]
    assert all(item.supports_vision_bridge is item.supports_vision for item in catalog)
