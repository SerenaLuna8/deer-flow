from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import resolve_system_audit_context
from app.system_settings.errors import SystemModelInvalid, SystemModelNotFound
from app.system_settings.models import (
    CreateSystemModel,
    LockedSystemModelMaterial,
    SystemModelConnectionCheck,
    UpdateSystemModel,
)
from app.system_settings.service import SystemModelCatalogService
from app.system_settings.validation import (
    BUILTIN_PROVIDER_ADAPTERS,
    ModelSettingsInvalid,
    is_provider_adapter_authorable,
    is_provider_adapter_eligible_for_new_binding,
    provider_adapter_descriptor,
    provider_api_key_required,
    provider_class_path,
    validate_create_system_model,
    validate_system_model_connection_test,
    validate_update_system_model,
)

RETIRED_PROVIDER_ADAPTERS = (
    "patched_mimo",
    "patched_minimax",
    "patched_stepfun",
    "mindie",
    "claude_code",
    "codex_cli",
    "vision_openai_compatible_v1",
)

VISION_RETIRED_PROVIDER_ADAPTER = "vision_openai_compatible_v1"


def test_fake_vision_adapter_is_not_a_builtin_production_dependency() -> None:
    assert "vision_bridge_fake" not in BUILTIN_PROVIDER_ADAPTERS


def test_legacy_vision_adapter_has_no_production_descriptor() -> None:
    assert VISION_RETIRED_PROVIDER_ADAPTER not in BUILTIN_PROVIDER_ADAPTERS
    assert not is_provider_adapter_authorable(VISION_RETIRED_PROVIDER_ADAPTER)
    assert not is_provider_adapter_eligible_for_new_binding(
        VISION_RETIRED_PROVIDER_ADAPTER,
    )
    with pytest.raises(ModelSettingsInvalid):
        provider_adapter_descriptor(VISION_RETIRED_PROVIDER_ADAPTER)
    with pytest.raises(ModelSettingsInvalid):
        provider_class_path(VISION_RETIRED_PROVIDER_ADAPTER)
    with pytest.raises(ModelSettingsInvalid):
        provider_api_key_required(VISION_RETIRED_PROVIDER_ADAPTER)


@pytest.mark.parametrize(
    "validator,command",
    (
        (
            validate_create_system_model,
            CreateSystemModel(
                display_name="Legacy vision",
                status="suspended",
                provider_adapter=VISION_RETIRED_PROVIDER_ADAPTER,
                provider_model="legacy-vision",
                settings={
                    "base_url": "https://legacy-vision.example.test/v1",
                },
                supports_thinking=False,
                supports_reasoning_effort=False,
                supports_vision=True,
                api_key="transient-test-key",
            ),
        ),
        (
            validate_update_system_model,
            UpdateSystemModel(
                display_name="Legacy vision",
                provider_adapter=VISION_RETIRED_PROVIDER_ADAPTER,
                provider_model="legacy-vision",
                settings={
                    "base_url": "https://legacy-vision.example.test/v1",
                },
                supports_thinking=False,
                supports_reasoning_effort=False,
                supports_vision=True,
                api_key="transient-test-key",
            ),
        ),
        (
            validate_system_model_connection_test,
            SystemModelConnectionCheck(
                provider_adapter=VISION_RETIRED_PROVIDER_ADAPTER,
                provider_model="legacy-vision",
                settings={
                    "base_url": "https://legacy-vision.example.test/v1",
                },
                supports_vision=True,
                api_key="transient-test-key",
            ),
        ),
    ),
)
def test_retired_vision_adapter_rejects_all_authoring(
    validator: object,
    command: object,
) -> None:
    with pytest.raises(ModelSettingsInvalid):
        validator(command)  # type: ignore[operator]


@pytest.mark.parametrize("provider_adapter", RETIRED_PROVIDER_ADAPTERS)
def test_retired_provider_adapters_have_no_runtime_class(
    provider_adapter: str,
) -> None:
    with pytest.raises(ModelSettingsInvalid):
        provider_class_path(provider_adapter)


class _AdminOperationService(SystemModelCatalogService):
    def __init__(self, repository: object) -> None:
        super().__init__(lambda: None)  # type: ignore[arg-type]
        self.repository = repository

    async def _admin_operation(self, context: object, operation):  # type: ignore[no-untyped-def]
        return await operation(self.repository, self._require_admin(context))


def _admin_context():
    return resolve_system_audit_context(
        SimpleNamespace(
            id=uuid.UUID("00000000-0000-4000-8000-000000000901"),
            system_role="system_admin",
        ),
        request_id="retired-provider-test",
    )


@pytest.mark.anyio
async def test_retired_vision_adapter_remains_admin_readable() -> None:
    model_id = uuid.UUID("00000000-0000-4000-8000-000000000911")
    now = datetime.now(UTC)
    model = SimpleNamespace(
        id=model_id,
        display_name="Legacy vision",
        status="suspended",
        provider_adapter=VISION_RETIRED_PROVIDER_ADAPTER,
        provider_model="legacy-vision",
        settings={"base_url": "https://legacy-vision.example.test/v1"},
        supports_thinking=False,
        supports_reasoning_effort=False,
        supports_vision=True,
        payload_checksum="b" * 64,
        current_secret_generation_id=None,
        secret_revision=0,
        revision=2,
        created_by_user_id=str(_admin_context().user_id),
        updated_by_user_id=str(_admin_context().user_id),
        created_at=now,
        updated_at=now,
    )

    class Repository:
        async def catalog_state(self):
            return SimpleNamespace(
                revision=4,
                default_model_config_id=None,
            )

        async def list_models(self):
            return (model,)

    catalog = await _AdminOperationService(Repository()).list_models(
        _admin_context(),
    )

    assert len(catalog.items) == 1
    assert catalog.items[0].provider_adapter == VISION_RETIRED_PROVIDER_ADAPTER
    assert catalog.items[0].status == "suspended"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "provider_adapter",
    ("mindie", VISION_RETIRED_PROVIDER_ADAPTER),
)
async def test_retired_provider_cannot_be_reactivated_or_made_default(
    provider_adapter: str,
) -> None:
    model_id = uuid.UUID("00000000-0000-4000-8000-000000000902")
    model = SimpleNamespace(
        id=model_id,
        revision=1,
        status="suspended",
        provider_adapter=provider_adapter,
    )
    state = SimpleNamespace(
        revision=1,
        default_model_config_id=None,
    )

    class Repository:
        async def catalog_state(self, *, for_update: bool = False):
            del for_update
            return state

        async def lock_model(self, requested_id: uuid.UUID):
            assert requested_id == model_id
            return model

    service = _AdminOperationService(Repository())

    with pytest.raises(SystemModelInvalid):
        await service.set_status(
            _admin_context(),
            model_id,
            "active",
        )

    model.status = "active"
    with pytest.raises(SystemModelInvalid):
        await service.set_default(
            _admin_context(),
            model_id,
        )


@pytest.mark.anyio
async def test_retired_provider_is_hidden_from_the_public_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retired_id = uuid.UUID("00000000-0000-4000-8000-000000000906")
    supported_id = uuid.UUID("00000000-0000-4000-8000-000000000907")
    retired_model = SimpleNamespace(
        id=retired_id,
        display_name="Retired model",
        provider_adapter=VISION_RETIRED_PROVIDER_ADAPTER,
        settings={"base_url": "https://legacy-vision.example.test/v1"},
        supports_thinking=False,
        supports_reasoning_effort=False,
        supports_vision=False,
        current_secret_generation_id=None,
    )
    supported_model = SimpleNamespace(
        id=supported_id,
        display_name="Supported model",
        provider_adapter="openai",
        settings={},
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_vision=False,
        current_secret_generation_id=uuid.uuid4(),
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
            return SimpleNamespace(default_model_config_id=retired_id)

        async def list_models(self, *, active_only: bool):
            assert active_only is True
            return (
                retired_model,
                supported_model,
            )

    monkeypatch.setattr(
        "app.system_settings.service.SystemModelRepository",
        Repository,
    )
    service = SystemModelCatalogService(Session)

    models = await service.list_available_models()

    assert [model.model_ref for model in models] == [str(supported_id)]
    assert models[0].is_default is False


@pytest.mark.anyio
async def test_retired_provider_cannot_be_admitted_to_a_new_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    material = LockedSystemModelMaterial(
        model=SimpleNamespace(
            id=uuid.UUID("00000000-0000-4000-8000-000000000903"),
            status="active",
            provider_adapter=VISION_RETIRED_PROVIDER_ADAPTER,
        ),
    )

    class Repository:
        def __init__(self, _session: AsyncSession) -> None:
            pass

        async def resolve_active_model(self, *_args, **_kwargs):
            return material

    monkeypatch.setattr(
        "app.system_settings.service.SystemModelRepository",
        Repository,
    )
    service = SystemModelCatalogService(lambda: None)  # type: ignore[arg-type]

    async with AsyncSession() as session, session.begin():
        with pytest.raises(SystemModelNotFound):
            await service.admit_model_snapshot(
                session,
                project_id=uuid.UUID(
                    "00000000-0000-4000-8000-000000000904",
                ),
                owner_user_id="00000000-0000-4000-8000-000000000905",
                thread_id="retired-provider-thread",
                run_id="retired-provider-run",
                purpose="lead",
                model_ref="00000000-0000-4000-8000-000000000903",
                request_id="retired-provider-admission",
            )
