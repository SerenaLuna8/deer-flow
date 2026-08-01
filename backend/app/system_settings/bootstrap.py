"""One-time encrypted bootstrap for the local default system model."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap_identities import (
    BUILTIN_MODEL_EMAIL,
    BUILTIN_MODEL_USER_ID,
)
from app.shared_assets.crypto import (
    CredentialEncryptFailed,
    CredentialPayloadInvalid,
    EncryptedEnvelope,
    encrypt_credential_payload,
)
from app.shared_assets.keyring import (
    CredentialKeyring,
    CredentialKeyringInvalid,
)
from app.shared_assets.models import AssetScope
from app.system_settings.credential_adapter import (
    SystemModelCredentialAdapter,
    SystemModelMaterializationUnavailable,
)
from app.system_settings.models import CreateSystemModel
from app.system_settings.repository import (
    SystemModelRepository,
    SystemModelRepositoryInvariant,
)
from app.system_settings.validation import (
    ModelSettingsInvalid,
    canonical_model_payload_checksum,
    validate_create_system_model,
)
from deerflow.persistence.projects import ProjectMembershipRow
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
from deerflow.persistence.user import UserRow

_BOOTSTRAP_LOCK_KEY = 0x0DEE_12F1_4D4F_444C
_ID_NAMESPACE = uuid.UUID("e9ef2794-807b-5d89-967c-c67be15b42e7")
_CREDENTIAL_SOURCE_KEY = "builtin:system-model:deepseek-v4:credential"

DEFAULT_CREDENTIAL_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "deepseek-v4:credential",
)
DEFAULT_CREDENTIAL_VERSION_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "deepseek-v4:credential:version:1",
)
DEFAULT_MODEL_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "deepseek-v4:model",
)
DEFAULT_MODEL_VERSION_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "deepseek-v4:model:version:1",
)


class DefaultSystemModelBootstrapConfigurationInvalid(RuntimeError):
    """Secret-free preflight failure raised before database creation."""

    def __init__(self) -> None:
        super().__init__(
            "默认模型初始化需要 DEEPSEEK_API_KEY 和有效的 Credential keyring",
        )


class DefaultSystemModelBootstrapConflict(RuntimeError):
    """Existing rows do not form a complete, usable model catalog."""

    def __init__(self) -> None:
        super().__init__("DEFAULT_SYSTEM_MODEL_BOOTSTRAP_CONFLICT")


class DefaultSystemModelBootstrapStorageUnavailable(RuntimeError):
    """Secret-free persistence failure."""

    def __init__(self) -> None:
        super().__init__("DEFAULT_SYSTEM_MODEL_BOOTSTRAP_STORAGE_UNAVAILABLE")


@dataclass(frozen=True, slots=True)
class DefaultSystemModelBootstrapMaterial:
    """Pre-encrypted input passed across the create-database boundary."""

    command: CreateSystemModel
    credential_id: uuid.UUID
    credential_version_id: uuid.UUID
    model_id: uuid.UUID
    model_version_id: uuid.UUID
    envelope: EncryptedEnvelope = field(repr=False)


def _default_model_command() -> CreateSystemModel:
    return validate_create_system_model(
        CreateSystemModel(
            logical_name="deepseek-v4",
            display_name="DeepSeek V4 Pro",
            description="",
            status="active",
            provider_adapter="patched_deepseek",
            provider_model="deepseek-v4-pro",
            settings={
                "base_url": "https://api.deepseek.com",
                "request_timeout": 600.0,
                "max_retries": 2,
                "max_tokens": 16384,
                "temperature": 0.7,
                "reasoning_effort": "high",
                "when_thinking_enabled": {
                    "extra_body": {
                        "thinking": {
                            "type": "enabled",
                        },
                    },
                },
                "when_thinking_disabled": {
                    "extra_body": {
                        "thinking": {
                            "type": "disabled",
                        },
                    },
                },
            },
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_vision=False,
            credential_id=DEFAULT_CREDENTIAL_ID,
            credential_version_id=DEFAULT_CREDENTIAL_VERSION_ID,
            credential_env_key="DEEPSEEK_API_KEY",
        )
    )


def prepare_default_system_model_bootstrap() -> DefaultSystemModelBootstrapMaterial:
    """Validate and encrypt the bootstrap secret without touching PostgreSQL."""

    try:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError
        keyring = CredentialKeyring.from_environment()
        envelope = encrypt_credential_payload(
            {"env": {"DEEPSEEK_API_KEY": api_key}},
            AssetScope.SYSTEM,
            None,
            DEFAULT_CREDENTIAL_VERSION_ID,
            keyring,
        )
        command = _default_model_command()
    except (
        CredentialEncryptFailed,
        CredentialKeyringInvalid,
        CredentialPayloadInvalid,
        ModelSettingsInvalid,
        TypeError,
        ValueError,
    ):
        raise DefaultSystemModelBootstrapConfigurationInvalid() from None
    return DefaultSystemModelBootstrapMaterial(
        command=command,
        credential_id=DEFAULT_CREDENTIAL_ID,
        credential_version_id=DEFAULT_CREDENTIAL_VERSION_ID,
        model_id=DEFAULT_MODEL_ID,
        model_version_id=DEFAULT_MODEL_VERSION_ID,
        envelope=envelope,
    )


async def _ensure_bootstrap_principal(session: AsyncSession) -> None:
    principal_id = str(BUILTIN_MODEL_USER_ID)
    principal = await session.get(
        UserRow,
        principal_id,
        with_for_update=True,
    )
    if principal is None:
        session.add(
            UserRow(
                id=principal_id,
                email=BUILTIN_MODEL_EMAIL,
                password_hash=None,
                system_role="user",
                oauth_provider=None,
                oauth_id=None,
                needs_setup=False,
                token_version=0,
            )
        )
        await session.flush()
    elif (
        principal.email != BUILTIN_MODEL_EMAIL
        or principal.password_hash is not None
        or principal.system_role != "user"
        or principal.oauth_provider is not None
        or principal.oauth_id is not None
        or principal.needs_setup
        or principal.token_version != 0
    ):
        raise DefaultSystemModelBootstrapConflict

    membership = await session.scalar(select(ProjectMembershipRow.id).where(ProjectMembershipRow.user_id == principal_id).limit(1))
    if membership is not None:
        raise DefaultSystemModelBootstrapConflict


async def _validate_existing_catalog(session: AsyncSession) -> None:
    try:
        material = await SystemModelRepository(session).resolve_active_model(
            "default",
            load_envelope=True,
        )
        if material is None:
            raise DefaultSystemModelBootstrapConflict
        SystemModelCredentialAdapter().materialize(material)
        command = validate_create_system_model(
            CreateSystemModel(
                logical_name=material.model.logical_name,
                display_name=material.model.display_name,
                description=material.model.description,
                status=material.model.status,
                provider_adapter=material.version.provider_adapter,
                provider_model=material.version.provider_model,
                settings=material.version.settings,
                supports_thinking=material.version.supports_thinking,
                supports_reasoning_effort=(material.version.supports_reasoning_effort),
                supports_vision=material.version.supports_vision,
                credential_id=(uuid.UUID(str(material.version.credential_id)) if material.version.credential_id is not None else None),
                credential_version_id=(uuid.UUID(str(material.version.credential_version_id)) if material.version.credential_version_id is not None else None),
                credential_env_key=material.version.credential_env_key,
                sort_order=material.model.sort_order,
            )
        )
        if (
            canonical_model_payload_checksum(
                uuid.UUID(str(material.model.id)),
                command,
            )
            != material.version.payload_checksum
        ):
            raise DefaultSystemModelBootstrapConflict
    except (
        ModelSettingsInvalid,
        SystemModelMaterializationUnavailable,
        SystemModelRepositoryInvariant,
        ValueError,
    ):
        raise DefaultSystemModelBootstrapConflict from None


async def _bootstrap_fresh_catalog(
    session: AsyncSession,
    state: SystemModelCatalogStateRow,
    material: DefaultSystemModelBootstrapMaterial,
) -> None:
    if state.revision != 1 or state.default_model_config_id is not None or material.command.credential_id != material.credential_id or material.command.credential_version_id != material.credential_version_id:
        raise DefaultSystemModelBootstrapConflict
    credential_count = await session.scalar(
        select(func.count()).select_from(CredentialRow),
    )
    if credential_count != 0:
        raise DefaultSystemModelBootstrapConflict

    await _ensure_bootstrap_principal(session)
    actor_id = str(BUILTIN_MODEL_USER_ID)
    credential = CredentialRow(
        id=material.credential_id,
        scope="system",
        project_id=None,
        name="deepseek-v4-api-key",
        display_name="DeepSeek V4 API Key",
        credential_type="model_api_key",
        status="active",
        is_delete=False,
        version=1,
        source_key=_CREDENTIAL_SOURCE_KEY,
        created_by_user_id=actor_id,
    )
    session.add(credential)
    await session.flush()
    credential_version = CredentialVersionRow(
        id=material.credential_version_id,
        credential_id=material.credential_id,
        version_number=1,
        status="active",
        payload_schema_version=1,
        payload_schema={"env": ["DEEPSEEK_API_KEY"]},
        created_by_user_id=actor_id,
    )
    session.add(credential_version)
    await session.flush()
    session.add(
        CredentialEnvelopeRow(
            credential_version_id=material.credential_version_id,
            envelope_generation=1,
            key_id=material.envelope.key_id,
            nonce=material.envelope.nonce,
            ciphertext=material.envelope.ciphertext,
            is_active=True,
            created_by_user_id=actor_id,
            activated_at=datetime.now(UTC),
        )
    )
    await session.flush()
    credential.current_version_id = credential_version.id
    await session.flush()

    model = SystemModelConfigRow(
        id=material.model_id,
        logical_name=material.command.logical_name,
        display_name=material.command.display_name,
        description=material.command.description,
        status=material.command.status,
        revision=1,
        sort_order=material.command.sort_order,
        created_by_user_id=actor_id,
        updated_by_user_id=actor_id,
    )
    session.add(model)
    await session.flush()
    model_version = SystemModelConfigVersionRow(
        id=material.model_version_id,
        model_config_id=material.model_id,
        version_number=1,
        provider_adapter=material.command.provider_adapter,
        provider_model=material.command.provider_model,
        settings=dict(material.command.settings),
        supports_thinking=material.command.supports_thinking,
        supports_reasoning_effort=(material.command.supports_reasoning_effort),
        supports_vision=material.command.supports_vision,
        credential_id=material.credential_id,
        credential_version_id=material.credential_version_id,
        credential_env_key=material.command.credential_env_key,
        payload_checksum=canonical_model_payload_checksum(
            material.model_id,
            material.command,
        ),
        created_by_user_id=actor_id,
    )
    session.add(model_version)
    await session.flush()
    model.current_version_id = model_version.id
    await session.flush()

    state.default_model_config_id = model.id
    state.revision += 1
    state.updated_by_user_id = actor_id
    await session.flush()


async def bootstrap_default_system_model(
    session_factory: async_sessionmaker[AsyncSession],
    material: DefaultSystemModelBootstrapMaterial,
) -> bool:
    """Create the catalog once, or validate and preserve an existing catalog."""

    if not isinstance(
        material,
        DefaultSystemModelBootstrapMaterial,
    ):
        raise DefaultSystemModelBootstrapConfigurationInvalid
    try:
        async with session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _BOOTSTRAP_LOCK_KEY},
            )
            state = (await session.execute(select(SystemModelCatalogStateRow).where(SystemModelCatalogStateRow.id == 1).with_for_update(of=SystemModelCatalogStateRow))).scalar_one_or_none()
            if state is None:
                raise DefaultSystemModelBootstrapConflict
            model_count = await session.scalar(
                select(func.count()).select_from(SystemModelConfigRow),
            )
            if model_count:
                await _validate_existing_catalog(session)
                return False
            await _bootstrap_fresh_catalog(session, state, material)
            return True
    except (
        DefaultSystemModelBootstrapConfigurationInvalid,
        DefaultSystemModelBootstrapConflict,
    ):
        raise
    except IntegrityError:
        raise DefaultSystemModelBootstrapConflict from None
    except DBAPIError:
        raise DefaultSystemModelBootstrapStorageUnavailable from None


__all__ = [
    "DEFAULT_CREDENTIAL_ID",
    "DEFAULT_CREDENTIAL_VERSION_ID",
    "DEFAULT_MODEL_ID",
    "DEFAULT_MODEL_VERSION_ID",
    "DefaultSystemModelBootstrapConfigurationInvalid",
    "DefaultSystemModelBootstrapConflict",
    "DefaultSystemModelBootstrapMaterial",
    "DefaultSystemModelBootstrapStorageUnavailable",
    "bootstrap_default_system_model",
    "prepare_default_system_model_bootstrap",
]
