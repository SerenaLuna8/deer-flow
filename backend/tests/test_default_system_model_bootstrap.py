from __future__ import annotations

from base64 import b64encode

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.model_registry.secrets import materialize_provider_api_key
from app.system_settings.bootstrap import (
    DEEPSEEK_PROVIDER_BASE_URL,
    DEEPSEEK_PROVIDER_ID,
    DEEPSEEK_PROVIDER_NAME,
    DEEPSEEK_V4_FLASH_VISION_EXP_MODEL_ID,
    DEFAULT_MODEL_ID,
    DefaultSystemModelBootstrapConfigurationInvalid,
    DefaultSystemModelBootstrapConflict,
    bootstrap_default_system_model,
    prepare_default_system_model_bootstrap,
)
from deerflow.persistence.model_registry import ModelProviderRow
from deerflow.persistence.system_settings import (
    SystemModelCatalogStateRow,
    SystemModelConfigRow,
    SystemModelSecretGenerationRow,
)
from deerflow.secrets import SecretKey


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
        "deepseek",
        "deepseek",
        "deepseek",
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

    # One DeepSeek Key protects one Provider envelope plus three independent
    # model envelopes; the seeded models derive their URL from the Provider.
    assert material.provider_id == DEEPSEEK_PROVIDER_ID
    assert material.provider_name == DEEPSEEK_PROVIDER_NAME
    assert material.provider_base_url == DEEPSEEK_PROVIDER_BASE_URL
    assert all(entry.command.provider_id == DEEPSEEK_PROVIDER_ID for entry in material.models)
    assert all(entry.command.settings["base_url"] == DEEPSEEK_PROVIDER_BASE_URL for entry in material.models)
    assert (
        materialize_provider_api_key(
            provider_id=material.provider_id,
            base_url=material.provider_base_url,
            nonce=material.provider_envelope.nonce,
            ciphertext=material.provider_envelope.ciphertext,
            key=SecretKey(b"s" * 32),
        )
        == "unit-bootstrap-secret"
    )


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

        # The seed installs the DeepSeek Provider row first and binds all
        # three text models to that fixed identity.
        async with factory() as session:
            provider = await session.get(ModelProviderRow, DEEPSEEK_PROVIDER_ID)
        assert provider is not None
        assert provider.name == DEEPSEEK_PROVIDER_NAME
        assert provider.base_url == DEEPSEEK_PROVIDER_BASE_URL
        assert all(model.provider_id == DEEPSEEK_PROVIDER_ID for model in models)
        assert all(model.settings["base_url"] == DEEPSEEK_PROVIDER_BASE_URL for model in models)
        assert (
            materialize_provider_api_key(
                provider_id=DEEPSEEK_PROVIDER_ID,
                base_url=provider.base_url,
                nonce=bytes(provider.api_key_nonce),
                ciphertext=bytes(provider.api_key_ciphertext),
                key=SecretKey(b"s" * 32),
            )
            == "unit-bootstrap-secret"
        )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_default_model_bootstrap_rejects_a_same_name_foreign_provider(
    migrated_postgres_database_url: str,
    default_model_bootstrap_environment: None,
) -> None:
    """The default name held by another identity fails loudly with zero writes."""

    import uuid as _uuid

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    material = prepare_default_system_model_bootstrap()
    foreign_id = _uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            session.add(
                ModelProviderRow(
                    id=foreign_id,
                    name=DEEPSEEK_PROVIDER_NAME,
                    base_url="https://foreign.deepseek.invalid",
                    request_timeout_seconds=30,
                    api_key_nonce=b"\x00" * 12,
                    api_key_ciphertext=b"\x00" * 16,
                )
            )

        with pytest.raises(DefaultSystemModelBootstrapConflict):
            await bootstrap_default_system_model(factory, material)

        async with factory() as session:
            assert await session.get(ModelProviderRow, DEEPSEEK_PROVIDER_ID) is None
            models = (await session.execute(select(SystemModelConfigRow))).scalars().all()
            state = await session.get(SystemModelCatalogStateRow, 1)
        assert models == []
        assert state is not None
        assert state.default_model_config_id is None
    finally:
        await engine.dispose()
