"""One-time encrypted bootstrap for the local default system model catalog."""

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
    BUILTIN_MODEL_USERNAME,
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
_DEEPSEEK_CREDENTIAL_SOURCE_KEY = "builtin:system-model:deepseek-v4:credential"
_OPENCODE_CREDENTIAL_SOURCE_KEY = "builtin:system-model:opencode-api-key:credential"

DEFAULT_CREDENTIAL_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "deepseek-v4:credential",
)
DEFAULT_CREDENTIAL_VERSION_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "deepseek-v4:credential:version:1",
)
OPENCODE_CREDENTIAL_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "opencode-api-key:credential",
)
OPENCODE_CREDENTIAL_VERSION_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "opencode-api-key:credential:version:1",
)
DEEPSEEK_V4_PRO_MODEL_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "deepseek-v4:model",
)
DEEPSEEK_V4_PRO_MODEL_VERSION_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "deepseek-v4:model:version:1",
)
DEEPSEEK_V4_FLASH_MODEL_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "deepseek-v4-flash:model",
)
DEEPSEEK_V4_FLASH_MODEL_VERSION_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "deepseek-v4-flash:model:version:1",
)
GPT_5_6_LUNA_MODEL_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "gpt-5.6-luna:model",
)
GPT_5_6_LUNA_MODEL_VERSION_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "gpt-5.6-luna:model:version:1",
)
DEFAULT_MODEL_ID = DEEPSEEK_V4_FLASH_MODEL_ID
DEFAULT_MODEL_VERSION_ID = DEEPSEEK_V4_FLASH_MODEL_VERSION_ID


class DefaultSystemModelBootstrapConfigurationInvalid(RuntimeError):
    """Secret-free preflight failure raised before database creation."""

    def __init__(self) -> None:
        super().__init__(
            "默认模型初始化需要 DEEPSEEK_API_KEY、OPENCODE_API_KEY 和有效的 Credential keyring",
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
class DefaultSystemModelBootstrapEntry:
    command: CreateSystemModel
    model_id: uuid.UUID
    model_version_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class DefaultSystemCredentialBootstrapEntry:
    credential_id: uuid.UUID
    credential_version_id: uuid.UUID
    name: str
    display_name: str
    source_key: str
    env_key: str
    envelope: EncryptedEnvelope = field(repr=False)


@dataclass(frozen=True, slots=True)
class DefaultSystemModelBootstrapMaterial:
    """Pre-encrypted input passed across the create-database boundary."""

    models: tuple[DefaultSystemModelBootstrapEntry, ...]
    credentials: tuple[DefaultSystemCredentialBootstrapEntry, ...]
    default_model_id: uuid.UUID


def _deepseek_model_command(
    *,
    logical_name: str,
    display_name: str,
    provider_model: str,
    sort_order: int,
) -> CreateSystemModel:
    return validate_create_system_model(
        CreateSystemModel(
            logical_name=logical_name,
            display_name=display_name,
            description="",
            status="active",
            provider_adapter="patched_deepseek",
            provider_model=provider_model,
            settings={
                "base_url": "https://api.deepseek.com",
                "request_timeout": 600.0,
                "max_retries": 2,
                "max_tokens": 51_200,
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
            sort_order=sort_order,
        )
    )


def _opencode_model_command() -> CreateSystemModel:
    return validate_create_system_model(
        CreateSystemModel(
            logical_name="gpt-5.6-luna",
            display_name="GPT 5.6 Luna",
            description="",
            status="active",
            provider_adapter="openai",
            provider_model="gpt-5.6-luna",
            settings={
                # This catalog entry is provisioned with the OpenCode Go
                # credential. ChatOpenAI appends the Responses resource, so
                # the configured base must end at /go/v1 rather than duplicate
                # the supplied /go/v1/responses endpoint.
                "base_url": "https://opencode.ai/zen/go/v1",
                "request_timeout": 600.0,
                "max_retries": 2,
                "use_responses_api": True,
                "output_version": "responses/v1",
            },
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_vision=True,
            credential_id=OPENCODE_CREDENTIAL_ID,
            credential_version_id=OPENCODE_CREDENTIAL_VERSION_ID,
            credential_env_key="OPENCODE_API_KEY",
            sort_order=20,
        )
    )


def _default_model_entries() -> tuple[DefaultSystemModelBootstrapEntry, ...]:
    return (
        DefaultSystemModelBootstrapEntry(
            command=_deepseek_model_command(
                logical_name="deepseek-v4-flash",
                display_name="DeepSeek V4 Flash",
                provider_model="deepseek-v4-flash",
                sort_order=0,
            ),
            model_id=DEEPSEEK_V4_FLASH_MODEL_ID,
            model_version_id=DEEPSEEK_V4_FLASH_MODEL_VERSION_ID,
        ),
        DefaultSystemModelBootstrapEntry(
            command=_deepseek_model_command(
                logical_name="deepseek-v4",
                display_name="DeepSeek V4 Pro",
                provider_model="deepseek-v4-pro",
                sort_order=10,
            ),
            model_id=DEEPSEEK_V4_PRO_MODEL_ID,
            model_version_id=DEEPSEEK_V4_PRO_MODEL_VERSION_ID,
        ),
        DefaultSystemModelBootstrapEntry(
            command=_opencode_model_command(),
            model_id=GPT_5_6_LUNA_MODEL_ID,
            model_version_id=GPT_5_6_LUNA_MODEL_VERSION_ID,
        ),
    )


def prepare_default_system_model_bootstrap() -> DefaultSystemModelBootstrapMaterial:
    """Validate and encrypt the bootstrap secret without touching PostgreSQL."""

    try:
        deepseek_api_key = os.environ.get("DEEPSEEK_API_KEY")
        opencode_api_key = os.environ.get("OPENCODE_API_KEY")
        if not isinstance(deepseek_api_key, str) or not deepseek_api_key.strip() or not isinstance(opencode_api_key, str) or not opencode_api_key.strip():
            raise ValueError
        keyring = CredentialKeyring.from_environment()
        credentials = (
            DefaultSystemCredentialBootstrapEntry(
                credential_id=DEFAULT_CREDENTIAL_ID,
                credential_version_id=DEFAULT_CREDENTIAL_VERSION_ID,
                name="deepseek-v4-api-key",
                display_name="DeepSeek V4 API Key",
                source_key=_DEEPSEEK_CREDENTIAL_SOURCE_KEY,
                env_key="DEEPSEEK_API_KEY",
                envelope=encrypt_credential_payload(
                    {"env": {"DEEPSEEK_API_KEY": deepseek_api_key}},
                    AssetScope.SYSTEM,
                    None,
                    DEFAULT_CREDENTIAL_VERSION_ID,
                    keyring,
                ),
            ),
            DefaultSystemCredentialBootstrapEntry(
                credential_id=OPENCODE_CREDENTIAL_ID,
                credential_version_id=OPENCODE_CREDENTIAL_VERSION_ID,
                name="opencode-api-key",
                display_name="OpenCode API Key",
                source_key=_OPENCODE_CREDENTIAL_SOURCE_KEY,
                env_key="OPENCODE_API_KEY",
                envelope=encrypt_credential_payload(
                    {"env": {"OPENCODE_API_KEY": opencode_api_key}},
                    AssetScope.SYSTEM,
                    None,
                    OPENCODE_CREDENTIAL_VERSION_ID,
                    keyring,
                ),
            ),
        )
        models = _default_model_entries()
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
        models=models,
        credentials=credentials,
        default_model_id=DEFAULT_MODEL_ID,
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
                username=BUILTIN_MODEL_USERNAME,
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
        or principal.username != BUILTIN_MODEL_USERNAME
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
    model_ids = {entry.model_id for entry in material.models}
    model_version_ids = {entry.model_version_id for entry in material.models}
    logical_names = {entry.command.logical_name for entry in material.models}
    credential_ids = {entry.credential_id for entry in material.credentials}
    credential_version_ids = {entry.credential_version_id for entry in material.credentials}
    credential_names = {entry.name for entry in material.credentials}
    credential_source_keys = {entry.source_key for entry in material.credentials}
    credential_references = {
        (
            entry.credential_id,
            entry.credential_version_id,
            entry.env_key,
        )
        for entry in material.credentials
    }
    if (
        state.revision != 1
        or state.default_model_config_id is not None
        or not material.models
        or not material.credentials
        or material.default_model_id not in model_ids
        or len(model_ids) != len(material.models)
        or len(model_version_ids) != len(material.models)
        or len(logical_names) != len(material.models)
        or len(credential_ids) != len(material.credentials)
        or len(credential_version_ids) != len(material.credentials)
        or len(credential_names) != len(material.credentials)
        or len(credential_source_keys) != len(material.credentials)
        or any(
            (
                entry.command.credential_id,
                entry.command.credential_version_id,
                entry.command.credential_env_key,
            )
            not in credential_references
            for entry in material.models
        )
    ):
        raise DefaultSystemModelBootstrapConflict
    credential_count = await session.scalar(
        select(func.count()).select_from(CredentialRow),
    )
    if credential_count != 0:
        raise DefaultSystemModelBootstrapConflict

    await _ensure_bootstrap_principal(session)
    actor_id = str(BUILTIN_MODEL_USER_ID)
    for entry in material.credentials:
        credential = CredentialRow(
            id=entry.credential_id,
            scope="system",
            project_id=None,
            name=entry.name,
            display_name=entry.display_name,
            credential_type="model_api_key",
            status="active",
            is_delete=False,
            version=1,
            source_key=entry.source_key,
            created_by_user_id=actor_id,
        )
        session.add(credential)
        await session.flush()
        credential_version = CredentialVersionRow(
            id=entry.credential_version_id,
            credential_id=entry.credential_id,
            version_number=1,
            status="active",
            payload_schema_version=1,
            payload_schema={"env": [entry.env_key]},
            created_by_user_id=actor_id,
        )
        session.add(credential_version)
        await session.flush()
        session.add(
            CredentialEnvelopeRow(
                credential_version_id=entry.credential_version_id,
                envelope_generation=1,
                key_id=entry.envelope.key_id,
                nonce=entry.envelope.nonce,
                ciphertext=entry.envelope.ciphertext,
                is_active=True,
                created_by_user_id=actor_id,
                activated_at=datetime.now(UTC),
            )
        )
        await session.flush()
        credential.current_version_id = credential_version.id
        await session.flush()

    for entry in material.models:
        model = SystemModelConfigRow(
            id=entry.model_id,
            logical_name=entry.command.logical_name,
            display_name=entry.command.display_name,
            description=entry.command.description,
            status=entry.command.status,
            revision=1,
            sort_order=entry.command.sort_order,
            created_by_user_id=actor_id,
            updated_by_user_id=actor_id,
        )
        session.add(model)
        await session.flush()
        model_version = SystemModelConfigVersionRow(
            id=entry.model_version_id,
            model_config_id=entry.model_id,
            version_number=1,
            provider_adapter=entry.command.provider_adapter,
            provider_model=entry.command.provider_model,
            settings=dict(entry.command.settings),
            supports_thinking=entry.command.supports_thinking,
            supports_reasoning_effort=(entry.command.supports_reasoning_effort),
            supports_vision=entry.command.supports_vision,
            credential_id=entry.command.credential_id,
            credential_version_id=entry.command.credential_version_id,
            credential_env_key=entry.command.credential_env_key,
            payload_checksum=canonical_model_payload_checksum(
                entry.model_id,
                entry.command,
            ),
            created_by_user_id=actor_id,
        )
        session.add(model_version)
        await session.flush()
        model.current_version_id = model_version.id
        await session.flush()

    state.default_model_config_id = material.default_model_id
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
    "DEEPSEEK_V4_FLASH_MODEL_ID",
    "DEEPSEEK_V4_FLASH_MODEL_VERSION_ID",
    "DEEPSEEK_V4_PRO_MODEL_ID",
    "DEEPSEEK_V4_PRO_MODEL_VERSION_ID",
    "GPT_5_6_LUNA_MODEL_ID",
    "GPT_5_6_LUNA_MODEL_VERSION_ID",
    "OPENCODE_CREDENTIAL_ID",
    "OPENCODE_CREDENTIAL_VERSION_ID",
    "DefaultSystemModelBootstrapConfigurationInvalid",
    "DefaultSystemModelBootstrapConflict",
    "DefaultSystemCredentialBootstrapEntry",
    "DefaultSystemModelBootstrapEntry",
    "DefaultSystemModelBootstrapMaterial",
    "DefaultSystemModelBootstrapStorageUnavailable",
    "bootstrap_default_system_model",
    "prepare_default_system_model_bootstrap",
]
