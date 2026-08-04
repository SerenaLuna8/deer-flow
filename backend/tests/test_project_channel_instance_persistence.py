from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from inspect import signature

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from deerflow.persistence.channel_connections.model import (
    ProjectChannelCredentialBindingRow,
    ProjectChannelInstanceLeaseRow,
    ProjectChannelInstanceRow,
)
from deerflow.persistence.channel_connections.project_instance_repository import (
    ProjectChannelInstanceRepository,
)
from deerflow.persistence.channel_connections.sql import ChannelConnectionRepository


def _constraint_names(table) -> set[str]:
    return {constraint.name for constraint in table.constraints if constraint.name is not None}


def _foreign_key_names(table) -> set[str]:
    return {constraint.name for constraint in table.constraints if isinstance(constraint, ForeignKeyConstraint) and constraint.name is not None}


def _index_names(table) -> set[str]:
    return {index.name for index in table.indexes if index.name is not None}


def test_project_channel_instance_metadata_is_project_scoped_and_secret_free() -> None:
    table = ProjectChannelInstanceRow.__table__

    assert set(table.c) == {
        table.c.id,
        table.c.project_id,
        table.c.provider,
        table.c.display_name,
        table.c.desired_status,
        table.c.observed_status,
        table.c.public_config,
        table.c.provider_identity_digest,
        table.c.revision,
        table.c.last_error_code,
        table.c.created_by_user_id,
        table.c.updated_by_user_id,
        table.c.created_at,
        table.c.updated_at,
        table.c.deleted_at,
    }
    assert isinstance(table.c.public_config.type, JSONB)
    assert table.c.id.type.python_type is uuid.UUID
    assert table.c.project_id.type.python_type is uuid.UUID
    assert not {
        "app_secret",
        "client_secret",
        "bot_token",
        "access_token",
        "refresh_token",
        "encrypted_secret",
    } & set(table.c.keys())

    assert {
        "ck_project_channel_instances_provider",
        "ck_project_channel_instances_desired_status",
        "ck_project_channel_instances_observed_status",
        "ck_project_channel_instances_public_config",
        "ck_project_channel_instances_identity_digest",
        "ck_project_channel_instances_revision",
        "uq_project_channel_instances_project_id",
    } <= _constraint_names(table)
    assert {
        "fk_project_channel_instances_project",
        "fk_project_channel_instances_creator",
        "fk_project_channel_instances_updater",
    } <= _foreign_key_names(table)
    assert {
        "uq_project_channel_instances_live_provider",
        "uq_project_channel_instances_live_identity",
    } <= _index_names(table)

    live_provider = next(index for index in table.indexes if index.name == "uq_project_channel_instances_live_provider")
    live_identity = next(index for index in table.indexes if index.name == "uq_project_channel_instances_live_identity")
    assert live_provider.unique is True
    assert live_identity.unique is True
    assert "deleted_at IS NULL" in str(live_provider.dialect_options["postgresql"]["where"])
    assert "deleted_at IS NULL" in str(live_identity.dialect_options["postgresql"]["where"])


def test_project_channel_binding_pins_exact_project_credential_version() -> None:
    table = ProjectChannelCredentialBindingRow.__table__

    assert {
        "project_id",
        "channel_instance_id",
        "credential_id",
        "credential_version_id",
        "binding_revision",
        "status",
    } <= set(table.c.keys())
    assert {
        "fk_project_channel_credential_bindings_instance",
        "fk_project_channel_credential_bindings_project_credential",
        "fk_project_channel_credential_bindings_credential_version",
    } <= _foreign_key_names(table)
    assert {
        "ck_project_channel_credential_bindings_revision",
        "ck_project_channel_credential_bindings_status",
        "ck_project_channel_credential_bindings_revocation",
    } <= _constraint_names(table)
    assert "uq_project_channel_credential_bindings_active_instance" in _index_names(table)


def test_project_channel_instance_lease_has_fencing_generation() -> None:
    table = ProjectChannelInstanceLeaseRow.__table__

    assert {
        "project_id",
        "channel_instance_id",
        "holder_id",
        "lease_token_hash",
        "fencing_generation",
        "lease_expires_at",
        "last_heartbeat_at",
    } <= set(table.c.keys())
    assert {
        "ck_project_channel_instance_leases_token_hash",
        "ck_project_channel_instance_leases_generation",
    } <= _constraint_names(table)
    assert "fk_project_channel_instance_leases_instance" in _foreign_key_names(table)


def test_channel_instance_indexes_are_explicit_about_legacy_null_rows() -> None:
    from deerflow.persistence.channel_connections.model import (
        ChannelConnectionRow,
        ChannelOAuthStateRow,
    )

    assert ChannelConnectionRow.__table__.c.channel_instance_id.nullable is True
    assert ChannelOAuthStateRow.__table__.c.channel_instance_id.nullable is True
    assert {
        "fk_channel_connections_project_instance",
        "fk_channel_oauth_states_project_instance",
    } <= _foreign_key_names(ChannelConnectionRow.__table__) | _foreign_key_names(ChannelOAuthStateRow.__table__)
    assert {
        "uq_channel_connection_active_legacy_identity",
        "uq_channel_connection_active_instance_identity",
        "uq_channel_connection_owner_legacy_identity",
        "uq_channel_connection_owner_instance_identity",
    } <= _index_names(ChannelConnectionRow.__table__)


def test_project_channel_models_use_named_constraints_and_indexes() -> None:
    for model in (
        ProjectChannelInstanceRow,
        ProjectChannelCredentialBindingRow,
        ProjectChannelInstanceLeaseRow,
    ):
        table = model.__table__
        for item in table.constraints | table.indexes:
            assert item.name, f"unnamed schema object on {table.name}: {item!r}"
        assert any(isinstance(item, (CheckConstraint, UniqueConstraint, Index)) for item in table.constraints | table.indexes)


@pytest.mark.parametrize(
    "public_config",
    [
        {"app_secret": "plaintext"},
        {"nested": {"bot_token": "plaintext"}},
        {"items": [{"client_secret": "plaintext"}]},
    ],
)
def test_project_channel_repository_rejects_secret_bearing_public_config(
    public_config: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="public_config"):
        ProjectChannelInstanceRepository.validate_public_config(public_config)


def test_project_channel_repository_accepts_only_public_provider_metadata() -> None:
    config = {
        "app_id": "cli_public",
        "domain": "https://open.feishu.cn",
        "features": ["im.message.receive_v1"],
    }

    assert ProjectChannelInstanceRepository.validate_public_config(config) == config


def test_project_channel_repository_exposes_runtime_and_binding_transitions() -> None:
    observed = signature(ProjectChannelInstanceRepository.set_observed_status)
    fenced_observed = signature(ProjectChannelInstanceRepository.set_observed_status_with_lease)
    lease_authorized = signature(ProjectChannelInstanceRepository.is_instance_lease_authorized)
    revoke = signature(ProjectChannelInstanceRepository.revoke_credential_binding)

    assert {
        "session",
        "channel_instance_id",
        "observed_status",
        "last_error_code",
    } <= set(observed.parameters)
    assert {
        "session",
        "channel_instance_id",
        "observed_status",
        "last_error_code",
        "holder_id",
        "lease_token",
        "fencing_generation",
    } <= set(fenced_observed.parameters)
    assert {
        "session",
        "channel_instance_id",
        "holder_id",
        "lease_token",
        "fencing_generation",
    } <= set(lease_authorized.parameters)
    assert {
        "session",
        "project_id",
        "channel_instance_id",
        "actor_user_id",
    } <= set(revoke.parameters)


@pytest.mark.asyncio
async def test_project_channel_instance_postgres_constraints(
    migrated_postgres_database_url: str,
) -> None:
    """The full-schema snapshot enforces project, identity, and Credential closure."""

    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_postgres_database_url)
    user_id = str(uuid.uuid4())
    other_user_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    instance_id = uuid.uuid4()
    other_instance_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    credential_version_id = uuid.uuid4()
    other_credential_id = uuid.uuid4()
    other_credential_version_id = uuid.uuid4()

    async def insert_instance(
        *,
        target_project_id: uuid.UUID,
        target_instance_id: uuid.UUID,
        provider: str = "feishu",
        identity_digest: str = "a" * 64,
        public_config: str = '{"app_id":"cli_public"}',
    ) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO project_channel_instances
                    (id,project_id,provider,display_name,desired_status,
                     observed_status,public_config,provider_identity_digest,
                     created_by_user_id,updated_by_user_id)
                    VALUES (:id,:project,:provider,'Feishu','enabled','stopped',
                            CAST(:config AS jsonb),:digest,:actor,:actor)"""
                ),
                {
                    "id": target_instance_id,
                    "project": target_project_id,
                    "provider": provider,
                    "config": public_config,
                    "digest": identity_digest,
                    "actor": user_id,
                },
            )

    try:
        async with engine.begin() as connection:
            for actor_id, email in (
                (user_id, "channel-owner@example.com"),
                (other_user_id, "channel-other@example.com"),
            ):
                await connection.execute(
                    text(
                        """INSERT INTO users
                        (id,email,system_role,created_at,needs_setup,token_version)
                        VALUES (:id,:email,'user',now(),false,0)"""
                    ),
                    {"id": actor_id, "email": email},
                )
            for target_project, slug in (
                (project_id, "channel-project-a"),
                (other_project_id, "channel-project-b"),
            ):
                await connection.execute(
                    text(
                        """INSERT INTO projects
                        (id,slug,display_name,created_by_user_id)
                        VALUES (:id,:slug,:slug,:actor)"""
                    ),
                    {"id": target_project, "slug": slug, "actor": user_id},
                )
                await connection.execute(
                    text(
                        """INSERT INTO project_memberships
                        (id,project_id,user_id,role)
                        VALUES (:id,:project,:user,'admin')"""
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project": target_project,
                        "user": user_id,
                    },
                )

            for target_project, target_credential, target_version, name in (
                (
                    project_id,
                    credential_id,
                    credential_version_id,
                    "channel-a",
                ),
                (
                    other_project_id,
                    other_credential_id,
                    other_credential_version_id,
                    "channel-b",
                ),
            ):
                await connection.execute(
                    text(
                        """INSERT INTO credentials
                        (id,scope,project_id,name,display_name,credential_type,
                         created_by_user_id)
                        VALUES (:id,'project',:project,:name,:name,
                                'channel.feishu',:actor)"""
                    ),
                    {
                        "id": target_credential,
                        "project": target_project,
                        "name": name,
                        "actor": user_id,
                    },
                )
                await connection.execute(
                    text(
                        """INSERT INTO credential_versions
                        (id,credential_id,version_number,payload_schema,
                         created_by_user_id)
                        VALUES (:id,:credential,1,'{}'::jsonb,:actor)"""
                    ),
                    {
                        "id": target_version,
                        "credential": target_credential,
                        "actor": user_id,
                    },
                )

        await insert_instance(
            target_project_id=project_id,
            target_instance_id=instance_id,
        )

        with pytest.raises(IntegrityError):
            await insert_instance(
                target_project_id=project_id,
                target_instance_id=uuid.uuid4(),
                identity_digest="b" * 64,
            )

        with pytest.raises(IntegrityError):
            await insert_instance(
                target_project_id=other_project_id,
                target_instance_id=uuid.uuid4(),
            )

        await insert_instance(
            target_project_id=other_project_id,
            target_instance_id=other_instance_id,
            identity_digest="b" * 64,
        )

        with pytest.raises(IntegrityError):
            await insert_instance(
                target_project_id=other_project_id,
                target_instance_id=uuid.uuid4(),
                provider="slack",
                identity_digest="d" * 64,
                public_config='{"nested":{"bot_token":"plaintext"}}',
            )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO channel_connections
                    (id,owner_user_id,provider,status,external_account_id,
                     workspace_id,scopes_json,capabilities_json,metadata_json,
                     created_at,updated_at,project_id,channel_instance_id)
                    VALUES
                    ('scoped-a',:owner,'feishu','connected','shared-user',
                     'shared-workspace','[]','{}','{}',now(),now(),:project_a,:instance_a),
                    ('scoped-b',:owner,'feishu','connected','shared-user',
                     'shared-workspace','[]','{}','{}',now(),now(),:project_b,:instance_b)"""
                ),
                {
                    "owner": user_id,
                    "project_a": project_id,
                    "instance_a": instance_id,
                    "project_b": other_project_id,
                    "instance_b": other_instance_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO project_channel_credential_bindings
                    (id,project_id,channel_instance_id,credential_id,
                     credential_version_id,binding_revision,status,
                     created_by_user_id)
                    VALUES (:id,:project,:instance,:credential,:version,1,
                            'active',:actor)"""
                ),
                {
                    "id": uuid.uuid4(),
                    "project": project_id,
                    "instance": instance_id,
                    "credential": credential_id,
                    "version": credential_version_id,
                    "actor": user_id,
                },
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO project_channel_credential_bindings
                        (id,project_id,channel_instance_id,credential_id,
                         credential_version_id,binding_revision,status,
                         created_by_user_id,revoked_at,revoked_by_user_id)
                        VALUES (:id,:project,:instance,:credential,:version,2,
                                'revoked',:actor,now(),:actor)"""
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project": project_id,
                        "instance": instance_id,
                        "credential": other_credential_id,
                        "version": other_credential_version_id,
                        "actor": user_id,
                    },
                )

        from sqlalchemy.ext.asyncio import async_sessionmaker

        connection_repository = ChannelConnectionRepository(async_sessionmaker(engine, expire_on_commit=False))
        project_a_connection = await connection_repository.find_connection_by_external_identity(
            provider="feishu",
            external_account_id="shared-user",
            workspace_id="shared-workspace",
            channel_instance_id=instance_id,
        )
        project_b_connection = await connection_repository.find_connection_by_external_identity(
            provider="feishu",
            external_account_id="shared-user",
            workspace_id="shared-workspace",
            channel_instance_id=other_instance_id,
        )
        assert project_a_connection is not None
        assert project_b_connection is not None
        assert project_a_connection["project_id"] == str(project_id)
        assert project_b_connection["project_id"] == str(other_project_id)
        assert (
            await connection_repository.find_connection_by_external_identity(
                provider="feishu",
                external_account_id="shared-user",
                workspace_id="shared-workspace",
            )
            is None
        )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO project_channel_credential_bindings
                        (id,project_id,channel_instance_id,credential_id,
                         credential_version_id,binding_revision,status,
                         created_by_user_id,revoked_at,revoked_by_user_id)
                        VALUES (:id,:project,:instance,:credential,:version,3,
                                'revoked',:actor,now(),:actor)"""
                    ),
                    {
                        "id": uuid.uuid4(),
                        "project": project_id,
                        "instance": instance_id,
                        "credential": credential_id,
                        "version": other_credential_version_id,
                        "actor": user_id,
                    },
                )

        # NULL is intentionally retained only for the deployment-config legacy
        # path. New UI/API code must always use the concrete project instance.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO channel_connections
                    (id,owner_user_id,provider,status,external_account_id,
                     workspace_id,scopes_json,capabilities_json,metadata_json,
                     created_at,updated_at,project_id,channel_instance_id)
                    VALUES ('instance-connection',:owner,'feishu','connected',
                            'instance-user','instance-workspace','[]','{}','{}',
                            now(),now(),:project,:instance)"""
                ),
                {
                    "owner": user_id,
                    "project": project_id,
                    "instance": instance_id,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO channel_connections
                    (id,owner_user_id,provider,status,external_account_id,
                     workspace_id,scopes_json,capabilities_json,metadata_json,
                     created_at,updated_at,project_id,channel_instance_id)
                    VALUES ('legacy-connection',:owner,'feishu','connected',
                            'legacy-user','legacy-workspace','[]','{}','{}',
                            now(),now(),:project,NULL)"""
                ),
                {"owner": user_id, "project": project_id},
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """INSERT INTO channel_connections
                        (id,owner_user_id,provider,status,external_account_id,
                         workspace_id,scopes_json,capabilities_json,metadata_json,
                         created_at,updated_at,project_id,channel_instance_id)
                        VALUES ('wrong-project-instance',:owner,'feishu','connected',
                                'wrong-project-user','wrong-workspace','[]','{}','{}',
                                now(),now(),:project,:instance)"""
                    ),
                    {
                        "owner": user_id,
                        "project": other_project_id,
                        "instance": instance_id,
                    },
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_channel_instance_lease_is_single_owner_and_fenced(
    migrated_postgres_database_url: str,
) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(migrated_postgres_database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repository = ProjectChannelInstanceRepository()
    user_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    instance_id = uuid.uuid4()
    first_holder = uuid.uuid4()
    second_holder = uuid.uuid4()
    now = datetime(2026, 8, 3, tzinfo=UTC)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,'channel-lease@example.com','user',now(),false,0)"""
                ),
                {"id": user_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO projects
                    (id,slug,display_name,created_by_user_id)
                    VALUES (:id,'channel-lease','Channel Lease',:actor)"""
                ),
                {"id": project_id, "actor": user_id},
            )
            await connection.execute(
                text(
                    """INSERT INTO project_channel_instances
                    (id,project_id,provider,display_name,desired_status,
                     observed_status,public_config,provider_identity_digest,
                     created_by_user_id,updated_by_user_id)
                    VALUES (:id,:project,'feishu','Feishu','enabled','stopped',
                            '{"app_id":"cli_public"}'::jsonb,:digest,:actor,:actor)"""
                ),
                {
                    "id": instance_id,
                    "project": project_id,
                    "digest": "c" * 64,
                    "actor": user_id,
                },
            )

        async with sessions.begin() as session:
            first = await repository.claim_instance_lease(
                session,
                instance_id,
                first_holder,
                30,
                now=now,
            )
        assert first is not None
        assert first.fencing_generation == 1

        async with sessions.begin() as session:
            assert await repository.is_instance_lease_authorized(
                session,
                channel_instance_id=instance_id,
                holder_id=first_holder,
                lease_token=first.lease_token,
                fencing_generation=first.fencing_generation,
                now=now + timedelta(seconds=1),
            )
            assert not await repository.is_instance_lease_authorized(
                session,
                channel_instance_id=instance_id,
                holder_id=first_holder,
                lease_token="wrong-token",
                fencing_generation=first.fencing_generation,
                now=now + timedelta(seconds=1),
            )
            assert (
                await repository.set_observed_status_with_lease(
                    session,
                    channel_instance_id=instance_id,
                    observed_status="running",
                    last_error_code=None,
                    holder_id=first_holder,
                    lease_token="wrong-token",
                    fencing_generation=first.fencing_generation,
                    now=now + timedelta(seconds=1),
                )
                is None
            )
            observed = await repository.set_observed_status_with_lease(
                session,
                channel_instance_id=instance_id,
                observed_status="running",
                last_error_code=None,
                holder_id=first_holder,
                lease_token=first.lease_token,
                fencing_generation=first.fencing_generation,
                now=now + timedelta(seconds=1),
            )
            assert observed is not None
            assert observed.observed_status == "running"

        async with sessions.begin() as session:
            assert (
                await repository.claim_instance_lease(
                    session,
                    instance_id,
                    second_holder,
                    30,
                    now=now + timedelta(seconds=1),
                )
                is None
            )
            assert (
                await repository.renew_instance_lease(
                    session,
                    instance_id,
                    first_holder,
                    "wrong-token",
                    first.fencing_generation,
                    30,
                    now=now + timedelta(seconds=1),
                )
                is None
            )

        async with engine.begin() as connection:
            stored_hash = await connection.scalar(
                text(
                    """SELECT lease_token_hash
                    FROM project_channel_instance_leases
                    WHERE channel_instance_id=:id"""
                ),
                {"id": instance_id},
            )
            assert stored_hash != first.lease_token
            await connection.execute(
                text(
                    """UPDATE project_channel_instances
                    SET desired_status='disabled' WHERE id=:id"""
                ),
                {"id": instance_id},
            )

        async with sessions.begin() as session:
            assert (
                await repository.renew_instance_lease(
                    session,
                    instance_id,
                    first_holder,
                    first.lease_token,
                    first.fencing_generation,
                    30,
                    now=now + timedelta(seconds=2),
                )
                is None
            )
            assert await repository.release_instance_lease(
                session,
                instance_id,
                first_holder,
                first.lease_token,
                first.fencing_generation,
                now=now + timedelta(seconds=2),
            )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE project_channel_instances
                    SET desired_status='enabled' WHERE id=:id"""
                ),
                {"id": instance_id},
            )

        async with sessions.begin() as session:
            second = await repository.claim_instance_lease(
                session,
                instance_id,
                second_holder,
                30,
                now=now + timedelta(seconds=3),
            )
        assert second is not None
        assert second.fencing_generation == 2
        assert second.lease_token != first.lease_token
    finally:
        await engine.dispose()
