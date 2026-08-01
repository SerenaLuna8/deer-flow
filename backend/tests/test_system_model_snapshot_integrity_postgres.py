from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from support.m4_private_threads import seed_m4_thread_database

from app.audit.models import resolve_system_audit_context
from app.private_work.retention_purge import purge_private_scope
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.credential_service import CreateCredential, CredentialService
from app.shared_assets.keyring import CredentialKeyring
from app.system_settings.credential_adapter import SystemModelCredentialAdapter
from app.system_settings.materializer import SystemModelMaterializer
from app.system_settings.models import CreateSystemModel
from app.system_settings.repository import SystemModelRepository
from app.system_settings.service import SystemModelCatalogService


class _DatabaseUuid(uuid.UUID):
    """Models the UUID subclass returned by asyncpg."""


@pytest.mark.postgres
@pytest.mark.anyio
async def test_model_snapshot_closure_is_exact_and_retention_cascade_is_controlled(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    thread_id = f"model-snapshot-integrity-{uuid.uuid4()}"
    run_id = str(uuid.uuid4())
    try:
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE users SET system_role='system_admin' WHERE id=:user_id",
                ),
                {"user_id": str(seed.owner_a.user_id)},
            )

        keyring = CredentialKeyring(
            active_key_id="model-test-key",
            _keys={"model-test-key": b"k" * 32},
        )
        credential = await CredentialService(
            seed.factory,
            keyring=keyring,
        ).create(
            SystemAssetGovernanceContext(
                user_id=seed.owner_a.user_id,
                request_id="model-snapshot-credential",
            ),
            CreateCredential(
                "model-snapshot-provider",
                "Model snapshot provider",
                "model_api_key",
            ),
            {"env": {"MODEL_PROVIDER_API_KEY": "test-only-provider-value"}},
        )
        assert credential.current_version_id is not None

        catalog = SystemModelCatalogService(seed.factory)
        model = await catalog.create_model(
            resolve_system_audit_context(
                SimpleNamespace(
                    id=seed.owner_a.user_id,
                    system_role="system_admin",
                ),
                request_id="model-snapshot-admin",
            ),
            CreateSystemModel(
                logical_name="snapshot-integrity-model",
                display_name="Snapshot integrity model",
                description="Exact credential closure test",
                status="active",
                provider_adapter="openai",
                provider_model="test-provider-model",
                settings={},
                supports_thinking=False,
                supports_reasoning_effort=False,
                supports_vision=False,
                credential_id=credential.id,
                credential_version_id=credential.current_version_id,
                credential_env_key="MODEL_PROVIDER_API_KEY",
            ),
        )

        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await PrivateRunRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                request=PrivateRunCreate(
                    run_id=run_id,
                    origin_trace_id=seed.owner_a.request_id,
                ),
            )
            resolved = await SystemModelRepository(session).resolve_active_model(
                model.logical_name,
                load_envelope=False,
            )
            assert resolved is not None
            admitted = await catalog.admit_model_snapshot(
                session,
                project_id=_DatabaseUuid(str(seed.owner_a.project_id)),
                owner_user_id=str(seed.owner_a.user_id),
                thread_id=thread_id,
                run_id=run_id,
                purpose="lead",
                model_ref=model.logical_name,
                request_id=seed.owner_a.request_id,
            )

        assert admitted.model_config_version_id == model.current_version.id
        assert admitted.credential_id == credential.id
        assert admitted.credential_version_id == credential.current_version_id
        assert admitted.credential_env_key == "MODEL_PROVIDER_API_KEY"
        runtime = await SystemModelMaterializer(
            seed.factory,
            credential_adapter=SystemModelCredentialAdapter(keyring=keyring),
        ).materialize_snapshot(
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            run_id=run_id,
            purpose="lead",
        )
        assert runtime.name == "snapshot-integrity-model"
        assert runtime.api_key is not None
        assert runtime.api_key.get_secret_value() == "test-only-provider-value"

        # MATCH SIMPLE on the nullable composite FK must not allow a snapshot
        # to omit the exact Credential closure pinned by its model version.
        with pytest.raises(DBAPIError):
            async with seed.factory() as session, session.begin():
                await session.execute(
                    text(
                        """INSERT INTO run_model_config_snapshots
                        (project_id,owner_user_id,thread_id,run_id,purpose,
                         logical_name,model_config_id,model_config_version_id,
                         payload_checksum,credential_id,credential_version_id,
                         credential_env_key)
                        SELECT project_id,owner_user_id,thread_id,run_id,
                               'credential-bypass',logical_name,model_config_id,
                               model_config_version_id,payload_checksum,
                               NULL,NULL,NULL
                        FROM run_model_config_snapshots
                        WHERE project_id=:project_id
                          AND owner_user_id=:owner_user_id
                          AND run_id=:run_id
                          AND purpose='lead'"""
                    ),
                    {
                        "project_id": seed.owner_a.project_id,
                        "owner_user_id": str(seed.owner_a.user_id),
                        "run_id": run_id,
                    },
                )

        for mutation in (
            """UPDATE run_model_config_snapshots
                  SET logical_name='mutated'
                WHERE project_id=:project_id
                  AND owner_user_id=:owner_user_id
                  AND run_id=:run_id
                  AND purpose='lead'""",
            """DELETE FROM run_model_config_snapshots
                WHERE project_id=:project_id
                  AND owner_user_id=:owner_user_id
                  AND run_id=:run_id
                  AND purpose='lead'""",
        ):
            with pytest.raises(DBAPIError):
                async with seed.factory() as session, session.begin():
                    await session.execute(
                        text(mutation),
                        {
                            "project_id": seed.owner_a.project_id,
                            "owner_user_id": str(seed.owner_a.user_id),
                            "run_id": run_id,
                        },
                    )

        # The retention boundary deletes only an unreferenced Run. Its
        # database-owned ON DELETE CASCADE may remove the immutable child,
        # while the direct child mutations above remain forbidden.
        async with seed.factory() as session, session.begin():
            await purge_private_scope(
                session,
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
            )

        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM runs WHERE run_id=:run_id),
                        (SELECT count(*) FROM run_model_config_snapshots
                          WHERE run_id=:run_id)"""
                    ),
                    {"run_id": run_id},
                )
            ).one()
        assert tuple(counts) == (0, 0)
    finally:
        await seed.engine.dispose()
