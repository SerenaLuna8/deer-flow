from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import resolve_system_audit_context
from app.system_settings.errors import SystemModelInvalid, SystemModelNotFound
from app.system_settings.models import LockedSystemModelMaterial
from app.system_settings.service import SystemModelCatalogService
from app.system_settings.validation import (
    ModelSettingsInvalid,
    provider_class_path,
)

RETIRED_PROVIDER_ADAPTERS = (
    "patched_mimo",
    "patched_minimax",
    "patched_stepfun",
    "mindie",
    "claude_code",
    "codex_cli",
)


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
async def test_retired_provider_cannot_be_reactivated_or_made_default() -> None:
    model_id = uuid.UUID("00000000-0000-4000-8000-000000000902")
    model = SimpleNamespace(
        id=model_id,
        revision=1,
        status="suspended",
    )
    version = SimpleNamespace(provider_adapter="mindie")
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

        async def current_version(self, requested_model):
            assert requested_model is model
            return version

    service = _AdminOperationService(Repository())

    with pytest.raises(SystemModelInvalid):
        await service.set_status(
            _admin_context(),
            model_id,
            "active",
            expected_revision=1,
        )

    model.status = "active"
    with pytest.raises(SystemModelInvalid):
        await service.set_default(
            _admin_context(),
            model_id,
            expected_catalog_revision=1,
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
    )
    supported_model = SimpleNamespace(
        id=supported_id,
        display_name="Supported model",
    )
    retired_version = SimpleNamespace(
        provider_adapter="patched_mimo",
        settings={},
        supports_thinking=False,
        supports_reasoning_effort=False,
        supports_vision=False,
    )
    supported_version = SimpleNamespace(
        provider_adapter="openai",
        settings={},
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_vision=False,
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
                (retired_model, retired_version),
                (supported_model, supported_version),
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
        ),
        version=SimpleNamespace(provider_adapter="codex_cli"),
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
