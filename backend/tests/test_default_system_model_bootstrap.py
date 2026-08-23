from __future__ import annotations

from base64 import b64encode

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.system_settings.bootstrap import (
    DEEPSEEK_V4_FLASH_VISION_EXP_MODEL_ID,
    DEFAULT_MODEL_ID,
    DefaultSystemModelBootstrapConfigurationInvalid,
    bootstrap_default_system_model,
    prepare_default_system_model_bootstrap,
)
from deerflow.persistence.system_settings import (
    SystemModelCatalogStateRow,
    SystemModelConfigRow,
    SystemModelSecretGenerationRow,
)


@pytest.fixture()
def default_model_bootstrap_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = b64encode(b"s" * 32).decode("ascii")
    monkeypatch.setenv(
        "ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY",
        "unit-bootstrap-secret",
    )
    monkeypatch.setenv("ACT_WEAVE_SECRET_KEY", encoded)


def test_default_model_bootstrap_prepares_three_independent_deepseek_models(
    default_model_bootstrap_environment: None,
) -> None:
    material = prepare_default_system_model_bootstrap()

    assert [entry.command.display_name for entry in material.models] == [
        "DeepSeek V4 Flash",
        "DeepSeek V4 Pro",
        "DeepSeek V4 Flash Vision Exp",
    ]
    assert len({entry.model_id for entry in material.models}) == len(material.models)
    assert [entry.command.provider_model for entry in material.models] == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash-vision-exp",
    ]
    assert [entry.command.provider_adapter for entry in material.models] == [
        "patched_deepseek",
        "patched_deepseek",
        "patched_deepseek",
    ]
    assert [entry.command.settings["max_tokens"] for entry in material.models] == [
        51_200,
        51_200,
        51_200,
    ]
    assert [entry.command.max_input_tokens for entry in material.models] == [
        1_000_000,
        1_000_000,
        1_000_000,
    ]
    assert material.default_model_id == DEFAULT_MODEL_ID
    assert material.models[0].model_id == material.default_model_id
    assert material.models[-1].model_id == DEEPSEEK_V4_FLASH_VISION_EXP_MODEL_ID
    assert material.models[-1].command.supports_thinking is True
    assert material.models[-1].command.supports_reasoning_effort is True
    assert material.models[-1].command.supports_vision is True
    assert len({entry.envelope.nonce for entry in material.models}) == 3
    assert len({entry.envelope.ciphertext for entry in material.models}) == 3
    assert "unit-bootstrap-secret" not in repr(material)


def test_default_model_bootstrap_requires_the_deepseek_bootstrap_key(
    default_model_bootstrap_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY")

    with pytest.raises(DefaultSystemModelBootstrapConfigurationInvalid):
        prepare_default_system_model_bootstrap()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_default_model_bootstrap_persists_flash_as_default(
    migrated_postgres_database_url: str,
    default_model_bootstrap_environment: None,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    material = prepare_default_system_model_bootstrap()
    try:
        assert await bootstrap_default_system_model(factory, material) is True
        assert await bootstrap_default_system_model(factory, material) is False

        async with factory() as session:
            state = await session.get(SystemModelCatalogStateRow, 1)
            models = tuple(
                (
                    await session.execute(
                        select(SystemModelConfigRow).order_by(
                            SystemModelConfigRow.created_at.desc(),
                            SystemModelConfigRow.id.desc(),
                        )
                    )
                ).scalars()
            )
            secret_generations = tuple(
                (
                    await session.execute(
                        select(SystemModelSecretGenerationRow).order_by(
                            SystemModelSecretGenerationRow.model_config_id,
                        )
                    )
                ).scalars()
            )

        assert state is not None
        assert state.default_model_config_id == DEFAULT_MODEL_ID
        assert {model.id: model.display_name for model in models} == {entry.model_id: entry.command.display_name for entry in material.models}
        assert {model.provider_model for model in models} == {
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash-vision-exp",
        }
        assert {model.provider_model: model.settings["max_tokens"] for model in models} == {
            "deepseek-v4-flash": 51_200,
            "deepseek-v4-pro": 51_200,
            "deepseek-v4-flash-vision-exp": 51_200,
        }
        assert {model.provider_model: model.max_input_tokens for model in models} == {
            "deepseek-v4-flash": 1_000_000,
            "deepseek-v4-pro": 1_000_000,
            "deepseek-v4-flash-vision-exp": 1_000_000,
        }
        assert len(secret_generations) == 3
        assert len({generation.id for generation in secret_generations}) == 3
        assert len({generation.ciphertext for generation in secret_generations}) == 3
    finally:
        await engine.dispose()
