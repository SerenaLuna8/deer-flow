from __future__ import annotations

from base64 import b64encode

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.system_settings.bootstrap import (
    DEFAULT_MODEL_ID,
    GPT_5_6_LUNA_MODEL_ID,
    DefaultSystemModelBootstrapConfigurationInvalid,
    bootstrap_default_system_model,
    prepare_default_system_model_bootstrap,
)
from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialRow,
    CredentialVersionRow,
)
from deerflow.persistence.system_settings import (
    SystemModelCatalogStateRow,
    SystemModelConfigRow,
    SystemModelConfigVersionRow,
)


@pytest.fixture()
def default_model_bootstrap_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = b64encode(b"s" * 32).decode("ascii")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-bootstrap-secret")
    monkeypatch.setenv("OPENCODE_API_KEY", "unit-opencode-bootstrap-secret")
    monkeypatch.setenv(
        "DEER_FLOW_CREDENTIAL_ACTIVE_KEY_ID",
        "unit-bootstrap",
    )
    monkeypatch.setenv(
        "DEER_FLOW_CREDENTIAL_KEYRING_JSON",
        f'{{"unit-bootstrap":"{encoded}"}}',
    )


def test_default_model_bootstrap_prepares_deepseek_and_opencode_models(
    default_model_bootstrap_environment: None,
) -> None:
    material = prepare_default_system_model_bootstrap()

    assert [entry.command.display_name for entry in material.models] == [
        "DeepSeek V4 Flash",
        "DeepSeek V4 Pro",
        "GPT 5.6 Luna",
    ]
    assert len({entry.model_id for entry in material.models}) == len(material.models)
    assert [entry.command.provider_model for entry in material.models] == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "gpt-5.6-luna",
    ]
    assert [entry.command.provider_adapter for entry in material.models] == [
        "patched_deepseek",
        "patched_deepseek",
        "openai",
    ]
    assert [entry.command.settings["max_tokens"] for entry in material.models if entry.command.provider_adapter == "patched_deepseek"] == [51_200, 51_200]
    assert material.default_model_id == DEFAULT_MODEL_ID
    assert material.models[0].model_id == material.default_model_id
    assert material.models[-1].model_id == GPT_5_6_LUNA_MODEL_ID
    assert material.models[-1].command.settings == {
        "base_url": "https://opencode.ai/zen/go/v1",
        "request_timeout": 600.0,
        "use_responses_api": True,
        "output_version": "responses/v1",
    }
    assert material.models[-1].command.supports_thinking is True
    assert material.models[-1].command.supports_reasoning_effort is True
    assert material.models[-1].command.supports_vision is True
    # Luna is one ordinary visual System Model. Its existing OpenAI adapter
    # selects Responses; inspect_image never resolves a second Bridge protocol.
    assert material.models[-1].command.provider_adapter == "openai"
    assert material.models[-1].command.settings["use_responses_api"] is True
    assert [credential.name for credential in material.credentials] == [
        "deepseek-v4-api-key",
        "opencode-api-key",
    ]
    assert [credential.env_key for credential in material.credentials] == [
        "DEEPSEEK_API_KEY",
        "OPENCODE_API_KEY",
    ]
    assert {
        (
            entry.command.credential_id,
            entry.command.credential_version_id,
            entry.command.credential_env_key,
        )
        for entry in material.models
    } == {
        (
            credential.credential_id,
            credential.credential_version_id,
            credential.env_key,
        )
        for credential in material.credentials
    }
    assert "unit-bootstrap-secret" not in repr(material)
    assert "unit-opencode-bootstrap-secret" not in repr(material)


def test_default_model_bootstrap_requires_opencode_credential(
    default_model_bootstrap_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY")

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
            versions = tuple(
                (
                    await session.execute(
                        select(SystemModelConfigVersionRow).order_by(
                            SystemModelConfigVersionRow.provider_model,
                        )
                    )
                ).scalars()
            )
            credentials = tuple(
                (
                    await session.execute(
                        select(CredentialRow).order_by(CredentialRow.name),
                    )
                ).scalars()
            )
            credential_version_count = await session.scalar(
                select(func.count()).select_from(CredentialVersionRow),
            )
            credential_envelope_count = await session.scalar(
                select(func.count()).select_from(CredentialEnvelopeRow),
            )

        assert state is not None
        assert state.default_model_config_id == DEFAULT_MODEL_ID
        assert {model.id: model.display_name for model in models} == {entry.model_id: entry.command.display_name for entry in material.models}
        assert {version.provider_model for version in versions} == {
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "gpt-5.6-luna",
        }
        assert [version.provider_adapter for version in versions] == [
            "patched_deepseek",
            "patched_deepseek",
            "openai",
        ]
        assert {version.provider_model: version.settings["max_tokens"] for version in versions if version.provider_adapter == "patched_deepseek"} == {
            "deepseek-v4-flash": 51_200,
            "deepseek-v4-pro": 51_200,
        }
        assert {
            (
                version.credential_id,
                version.credential_version_id,
                version.credential_env_key,
            )
            for version in versions
        } == {
            (
                credential.credential_id,
                credential.credential_version_id,
                credential.env_key,
            )
            for credential in material.credentials
        }
        assert [credential.name for credential in credentials] == [
            "deepseek-v4-api-key",
            "opencode-api-key",
        ]
        assert credential_version_count == 2
        assert credential_envelope_count == 2
    finally:
        await engine.dispose()
