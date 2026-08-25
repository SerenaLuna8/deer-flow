from __future__ import annotations

import pytest

from app.shared_assets.agent_catalog import (
    AgentCatalogValidator,
    StaticToolGroupCatalog,
    require_agent_catalog_validation,
)
from app.shared_assets.errors import AssetValidationFailed

RETIRED_MODEL_REF = "00000000-0000-4000-8000-000000000309"


class _Session:
    pass


class _ModelCatalog:
    def __init__(self, active_refs: set[str]) -> None:
        self.active_refs = active_refs
        self.calls: list[str | None] = []

    async def resolve_admissible_active_model(self, model_ref: str | None) -> object | None:
        self.calls.append(model_ref)
        return object() if model_ref in self.active_refs else None


def _validator(
    session: _Session,
    *,
    groups: tuple[str, ...] = ("file:read", "task"),
    active_models: set[str] | None = None,
) -> tuple[AgentCatalogValidator, _ModelCatalog]:
    models = _ModelCatalog(active_models if active_models is not None else {"default"})
    factory_sessions: list[object] = []

    def model_catalog_factory(received_session):
        factory_sessions.append(received_session)
        assert received_session is session
        return models

    validator = AgentCatalogValidator(
        StaticToolGroupCatalog(groups),
        model_catalog_factory=model_catalog_factory,
    )
    models.factory_sessions = factory_sessions  # type: ignore[attr-defined]
    return validator, models


@pytest.mark.asyncio
async def test_catalog_validator_accepts_exact_groups_and_active_default_model() -> None:
    session = _Session()
    validator, models = _validator(session)

    await validator.validate(
        session,  # type: ignore[arg-type]
        request_id="catalog-valid",
        model_ref="default",
        tool_groups=("file:read", "task"),
    )

    assert models.calls == ["default"]
    assert models.factory_sessions == [session]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_catalog_validator_rejects_unknown_group_before_model_lookup() -> None:
    session = _Session()
    validator, models = _validator(session)

    with pytest.raises(AssetValidationFailed) as caught:
        await validator.validate(
            session,  # type: ignore[arg-type]
            request_id="catalog-unknown-group",
            model_ref="default",
            tool_groups=("file:read", "unknown"),
        )

    assert caught.value.request_id == "catalog-unknown-group"
    assert models.calls == []


@pytest.mark.asyncio
async def test_catalog_validator_rejects_legacy_model_name_before_lookup() -> None:
    session = _Session()
    validator, models = _validator(session)

    with pytest.raises(AssetValidationFailed):
        await validator.validate(
            session,  # type: ignore[arg-type]
            request_id="catalog-legacy-model-name",
            model_ref="gpt-5.6-luna",
            tool_groups=("file:read",),
        )

    assert models.calls == []


@pytest.mark.asyncio
async def test_catalog_validator_rejects_inactive_or_missing_model() -> None:
    session = _Session()
    validator, models = _validator(session, active_models=set())

    with pytest.raises(AssetValidationFailed) as caught:
        await validator.validate(
            session,  # type: ignore[arg-type]
            request_id="catalog-inactive-model",
            model_ref=RETIRED_MODEL_REF,
            tool_groups=("file:read",),
        )

    assert caught.value.request_id == "catalog-inactive-model"
    assert models.calls == [RETIRED_MODEL_REF]


@pytest.mark.asyncio
async def test_missing_catalog_authority_fails_closed() -> None:
    with pytest.raises(AssetValidationFailed) as caught:
        await require_agent_catalog_validation(
            None,
            _Session(),  # type: ignore[arg-type]
            request_id="catalog-unwired",
            model_ref="default",
            tool_groups=("file:read",),
        )

    assert caught.value.request_id == "catalog-unwired"


def test_catalog_validator_requires_explicit_tool_group_catalog() -> None:
    with pytest.raises(ValueError, match="tool-group catalog is required"):
        AgentCatalogValidator(None)  # type: ignore[arg-type]
