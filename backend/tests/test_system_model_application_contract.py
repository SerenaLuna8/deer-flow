from __future__ import annotations

import uuid
from dataclasses import fields
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.audit.models import resolve_system_audit_context
from app.gateway.routers.admin_model_settings import (
    AdminModelCreateRequest,
    AdminModelItemResponse,
    AdminModelUpdateRequest,
)
from app.gateway.routers.models import ModelResponse, _public_response
from app.system_settings.models import (
    CreateSystemModel,
    PublicSystemModelView,
    RunModelConfigSnapshotView,
    SystemModelView,
)
from app.system_settings.repository import SystemModelRepository
from app.system_settings.service import SystemModelCatalogService


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
        super().__init__(lambda: None)  # type: ignore[arg-type]
        self.repository = repository

    async def _admin_operation(self, context: object, operation):  # type: ignore[no-untyped-def]
        return await operation(self.repository, self._require_admin(context))


@pytest.mark.anyio
async def test_admin_create_uses_the_model_uuid_as_its_only_stable_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id = uuid.UUID("00000000-0000-4000-8000-000000000a02")
    version_id = uuid.UUID("00000000-0000-4000-8000-000000000a03")
    generated_ids = iter((model_id, version_id))
    monkeypatch.setattr(
        "app.system_settings.service.uuid.uuid4",
        lambda: next(generated_ids),
    )
    state = SimpleNamespace(
        revision=1,
        default_model_config_id=None,
        updated_by_user_id=None,
    )

    class Session:
        async def flush(self) -> None:
            return None

    class Repository:
        session = Session()
        created_model = None

        async def catalog_state(self, *, for_update: bool = False):
            assert for_update is True
            return state

        async def lock_system_credential_reference(self, *args, **kwargs):
            assert args == (None, None, None)
            assert kwargs == {
                "require_current": True,
                "load_envelope": False,
            }
            return None

        async def add_model(self, model, version) -> None:  # type: ignore[no-untyped-def]
            self.created_model = model
            model.current_version_id = version.id

    repository = Repository()
    created = await _AdminOperationService(repository).create_model(
        _admin_context(),
        CreateSystemModel(
            display_name="Shared display name",
            status="active",
            provider_adapter="vision_bridge_fake",
            provider_model="vision-bridge-fake-v1",
            settings={},
            supports_thinking=False,
            supports_reasoning_effort=False,
            supports_vision=True,
            credential_id=None,
            credential_version_id=None,
            credential_env_key=None,
        ),
    )

    assert repository.created_model.id == model_id
    assert created.id == model_id
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
                "settings": {},
                "supports_thinking": False,
                "supports_reasoning_effort": False,
                "supports_vision": True,
                "credential_id": None,
                "credential_version_id": None,
                "credential_env_key": None,
            }
        )


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
            load_envelope=False,
        )
        is None
    )
    assert (
        await repository.resolve_active_model(
            str(model_id).upper(),
            load_envelope=False,
        )
        is None
    )
    assert captured == []

    assert (
        await repository.resolve_active_model(
            str(model_id),
            load_envelope=False,
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
        )

    version = SimpleNamespace(
        provider_adapter="openai",
        settings={},
        supports_thinking=False,
        supports_reasoning_effort=False,
        supports_vision=False,
    )
    repository_rows = (
        (model(0), version),
        (model(1), version),
        (model(2), version),
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
