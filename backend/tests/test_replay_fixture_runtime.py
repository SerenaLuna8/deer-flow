from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from _replay_fixture import (
    bootstrap_replay_test_database,
    build_config_yaml,
    prepare_replay_runtime_catalog,
    replay_model_adapter,
)


def test_replay_config_uses_only_current_process_config(tmp_path: Path) -> None:
    config = build_config_yaml(home=tmp_path)

    assert "models:" not in config
    assert "memory:" not in config
    assert "summarization:" not in config
    assert "database:\n  url: $DATABASE_URL" in config


def test_replay_adapter_override_is_process_local_test_wiring() -> None:
    from app.system_settings import validation

    original = validation.PROVIDER_ADAPTERS["codex_cli"]
    with replay_model_adapter():
        assert validation.provider_class_path("codex_cli") == ("replay_provider:ReplayChatModel")
        assert validation.provider_credential_required("codex_cli") is False
    assert validation.PROVIDER_ADAPTERS["codex_cli"] is original


def test_replay_schema_bootstrap_rejects_non_test_database() -> None:
    with pytest.raises(
        RuntimeError,
        match=r"deerflow_test_replay_\*",
    ):
        asyncio.run(
            bootstrap_replay_test_database(
                "postgresql+asyncpg://localhost/deerflow",
            )
        )


def test_replay_runtime_catalog_rejects_non_test_database() -> None:
    with pytest.raises(
        RuntimeError,
        match=r"deerflow_test_\*",
    ):
        asyncio.run(
            prepare_replay_runtime_catalog(
                "postgresql+asyncpg://localhost/deerflow",
            )
        )


def test_record_gateway_uses_database_catalog_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import record_gateway

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("DEERFLOW_RECORD_OUT", raising=False)

    assert record_gateway.main() == 2
    assert "DEERFLOW_RECORD_OUT" in capsys.readouterr().err


@pytest.mark.postgres
@pytest.mark.anyio
async def test_replay_runtime_catalog_is_idempotent_and_materializable(
    migrated_postgres_database_url: str,
) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.system_runtime_settings.materializer import (
        SystemRuntimePolicyMaterializer,
    )
    from app.system_runtime_settings.models import (
        AgentRuntimePolicyValue,
        RuntimePolicySection,
    )
    from app.system_settings.materializer import SystemModelMaterializer

    await prepare_replay_runtime_catalog(migrated_postgres_database_url)
    await prepare_replay_runtime_catalog(migrated_postgres_database_url)

    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            model_count, model_version_count, is_default = (
                await session.execute(
                    text(
                        """SELECT
                        count(DISTINCT m.id),
                        count(v.id),
                        bool_and(s.default_model_config_id = m.id)
                        FROM system_model_configs m
                        JOIN system_model_config_versions v
                          ON v.model_config_id = m.id
                        CROSS JOIN system_model_catalog_state s
                        WHERE m.logical_name = 'scenario-model'"""
                    )
                )
            ).one()
            policy_revision, policy_version_count = (
                await session.execute(
                    text(
                        """SELECT p.revision, count(v.id)
                        FROM system_runtime_policies p
                        JOIN system_runtime_policy_versions v
                          ON v.section = p.section
                        WHERE p.section = 'agent_runtime'
                        GROUP BY p.revision"""
                    )
                )
            ).one()
        assert (model_count, model_version_count, is_default) == (1, 1, True)
        assert (policy_revision, policy_version_count) == (2, 2)

        with replay_model_adapter():
            model = await SystemModelMaterializer(factory).materialize_active("scenario-model")
        assert model.use == "replay_provider:ReplayChatModel"

        policy = await SystemRuntimePolicyMaterializer(factory).materialize_current(RuntimePolicySection.AGENT_RUNTIME)
        assert isinstance(policy, AgentRuntimePolicyValue)
        assert policy.summarization.enabled is False
        assert policy.memory.enabled is False
        assert policy.memory.search_enabled is False
        assert policy.memory.injection_enabled is False
    finally:
        await engine.dispose()
