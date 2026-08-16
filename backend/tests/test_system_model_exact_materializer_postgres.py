from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.system_settings import SystemModelMaterializer
from app.system_settings.credential_adapter import (
    SystemModelMaterializationUnavailable,
)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_exact_model_materializer_uses_frozen_version_without_run_snapshot(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = uuid.uuid4()
    model_id = uuid.uuid4()
    version_one_id = uuid.uuid4()
    version_two_id = uuid.uuid4()
    checksum_one = "a" * 64
    checksum_two = "b" * 64
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'system_admin',now(),false,0)"""
                ),
                {
                    "id": str(owner_id),
                    "email": f"{owner_id}@example.com",
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO system_model_configs
                    (id,display_name,status,current_version_id,
                     revision,created_by_user_id,updated_by_user_id)
                    VALUES (:id,'Exact model','active',NULL,2,
                            :owner,:owner)"""
                ),
                {
                    "id": model_id,
                    "owner": str(owner_id),
                },
            )
            for version_id, number, provider_model, checksum, supersedes in (
                (version_one_id, 1, "frozen-v1", checksum_one, None),
                (
                    version_two_id,
                    2,
                    "current-v2",
                    checksum_two,
                    version_one_id,
                ),
            ):
                await connection.execute(
                    text(
                        """INSERT INTO system_model_config_versions
                        (id,model_config_id,version_number,provider_adapter,
                         provider_model,settings,supports_thinking,
                         supports_reasoning_effort,supports_vision,credential_id,
                         credential_version_id,credential_env_key,payload_checksum,
                         supersedes_version_id,created_by_user_id)
                        VALUES (:id,:model,:number,'vision_bridge_fake',:provider_model,
                                '{}'::jsonb,false,false,false,NULL,NULL,NULL,
                                :checksum,:supersedes,:owner)"""
                    ),
                    {
                        "id": version_id,
                        "model": model_id,
                        "number": number,
                        "provider_model": provider_model,
                        "checksum": checksum,
                        "supersedes": supersedes,
                        "owner": str(owner_id),
                    },
                )
            await connection.execute(
                text(
                    """UPDATE system_model_configs
                    SET current_version_id=:version
                    WHERE id=:model"""
                ),
                {"version": version_two_id, "model": model_id},
            )
            snapshots_before = int(await connection.scalar(text("SELECT count(*) FROM run_model_config_snapshots")) or 0)

        materializer = SystemModelMaterializer(factory)
        frozen = await materializer.materialize_exact(
            model_config_id=model_id,
            model_config_version_id=version_one_id,
            payload_checksum=checksum_one,
        )
        assert frozen.model == "frozen-v1"

        with pytest.raises(SystemModelMaterializationUnavailable):
            await materializer.materialize_exact(
                model_config_id=model_id,
                model_config_version_id=version_one_id,
                payload_checksum=checksum_two,
            )

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE system_model_configs SET status='suspended' WHERE id=:id"),
                {"id": model_id},
            )
        with pytest.raises(SystemModelMaterializationUnavailable):
            await materializer.materialize_exact(
                model_config_id=model_id,
                model_config_version_id=version_one_id,
                payload_checksum=checksum_one,
            )

        async with engine.connect() as connection:
            snapshots_after = int(await connection.scalar(text("SELECT count(*) FROM run_model_config_snapshots")) or 0)
        assert snapshots_after == snapshots_before
    finally:
        await engine.dispose()
