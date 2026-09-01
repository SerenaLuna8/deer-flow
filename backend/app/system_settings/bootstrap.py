"""One-time protected bootstrap for the default System Model catalog."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap_identities import (
    BUILTIN_MODEL_EMAIL,
    BUILTIN_MODEL_USER_ID,
    BUILTIN_MODEL_USERNAME,
)
from app.model_registry.secrets import (
    ModelProviderSecretInvalid,
    protect_provider_api_key,
)
from app.system_settings.models import CreateSystemModel
from app.system_settings.secrets import (
    model_secret_envelope_digest,
    model_secret_recipient,
)
from app.system_settings.validation import (
    ModelSettingsInvalid,
    canonical_model_payload_checksum,
    validate_create_system_model,
)
from deerflow.persistence.model_registry import ModelProviderRow
from deerflow.persistence.projects import ProjectMembershipRow
from deerflow.persistence.system_settings import (
    SystemModelCatalogStateRow,
    SystemModelConfigRow,
    SystemModelSecretGenerationRow,
)
from deerflow.persistence.user import UserRow
from deerflow.secrets import (
    SecretEnvelope,
    SecretKey,
    SecretKeyInvalid,
    SecretProtectionFailed,
)

_BOOTSTRAP_LOCK_KEY = 0x0DEE_12F1_4D4F_444C
_ID_NAMESPACE = uuid.UUID("e9ef2794-807b-5d89-967c-c67be15b42e7")
_BOOTSTRAP_API_KEY_ENV = "ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY"
DEEPSEEK_V4_MAX_INPUT_TOKENS = 1_000_000

DEEPSEEK_PROVIDER_ID = uuid.uuid5(_ID_NAMESPACE, "deepseek:provider")
DEEPSEEK_PROVIDER_NAME = "DeepSeek"
DEEPSEEK_PROVIDER_BASE_URL = "https://api.deepseek.com"
# The Provider timeout only governs Embedding/Rerank requests; the seeded
# text models keep their own adapter request timeouts in ``settings``.
_DEEPSEEK_PROVIDER_REQUEST_TIMEOUT_SECONDS = 30

DEEPSEEK_V4_PRO_MODEL_ID = uuid.uuid5(_ID_NAMESPACE, "deepseek-v4:model")
DEEPSEEK_V4_FLASH_MODEL_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "deepseek-v4-flash:model",
)
DEEPSEEK_V4_FLASH_VISION_EXP_MODEL_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "deepseek-v4-flash-vision-exp:model",
)
DEFAULT_MODEL_ID = DEEPSEEK_V4_FLASH_MODEL_ID


class DefaultSystemModelBootstrapConfigurationInvalid(RuntimeError):
    """Secret-free preflight failure raised before first schema creation."""

    def __init__(self) -> None:
        super().__init__(
            "首次模型初始化需要 ACT_WEAVE_BOOTSTRAP_DEEPSEEK_API_KEY 和有效的 ACT_WEAVE_SECRET_KEY",
        )


class DefaultSystemModelBootstrapConflict(RuntimeError):
    def __init__(self) -> None:
        super().__init__("DEFAULT_SYSTEM_MODEL_BOOTSTRAP_CONFLICT")


class DefaultSystemModelBootstrapStorageUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("DEFAULT_SYSTEM_MODEL_BOOTSTRAP_STORAGE_UNAVAILABLE")


@dataclass(frozen=True, slots=True)
class DefaultSystemModelBootstrapEntry:
    command: CreateSystemModel
    model_id: uuid.UUID
    envelope: SecretEnvelope = field(repr=False)
    envelope_digest: str


@dataclass(frozen=True, slots=True)
class DefaultSystemModelBootstrapMaterial:
    """Pre-encrypted input passed across the create-schema boundary."""

    provider_id: uuid.UUID
    provider_name: str
    provider_base_url: str
    models: tuple[DefaultSystemModelBootstrapEntry, ...]
    default_model_id: uuid.UUID
    provider_envelope: SecretEnvelope = field(repr=False, kw_only=True)


def _deepseek_model_command(
    *,
    display_name: str,
    provider_model: str,
    supports_vision: bool,
) -> CreateSystemModel:
    # The seed derives ``settings.base_url`` from the DeepSeek Provider row,
    # exactly like the admin service; validation therefore runs with the
    # internal derived-URL allowance.
    return validate_create_system_model(
        CreateSystemModel(
            display_name=display_name,
            status="active",
            provider_id=DEEPSEEK_PROVIDER_ID,
            provider_adapter="deepseek",
            provider_model=provider_model,
            max_input_tokens=DEEPSEEK_V4_MAX_INPUT_TOKENS,
            settings={
                "base_url": DEEPSEEK_PROVIDER_BASE_URL,
                "request_timeout": 600.0,
                "max_tokens": 51_200,
                "temperature": 0.7,
                "reasoning_effort": "high",
                "when_thinking_enabled": {
                    "extra_body": {"thinking": {"type": "enabled"}},
                },
                "when_thinking_disabled": {
                    "extra_body": {"thinking": {"type": "disabled"}},
                },
            },
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_vision=supports_vision,
        ),
        allow_derived_base_url=True,
    )


def prepare_default_system_model_bootstrap() -> DefaultSystemModelBootstrapMaterial:
    """Validate and encrypt three independent model-owned Key copies."""

    try:
        api_key = os.environ.get(_BOOTSTRAP_API_KEY_ENV)
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError
        key = SecretKey.from_environment()
        definitions = (
            (
                DEEPSEEK_V4_FLASH_MODEL_ID,
                "DeepSeek V4 Flash",
                "deepseek-v4-flash",
                False,
            ),
            (
                DEEPSEEK_V4_PRO_MODEL_ID,
                "DeepSeek V4 Pro",
                "deepseek-v4-pro",
                False,
            ),
            (
                DEEPSEEK_V4_FLASH_VISION_EXP_MODEL_ID,
                "DeepSeek V4 Flash Vision Exp",
                "deepseek-v4-flash-vision-exp",
                True,
            ),
        )
        entries: list[DefaultSystemModelBootstrapEntry] = []
        for model_id, display_name, provider_model, supports_vision in definitions:
            command = _deepseek_model_command(
                display_name=display_name,
                provider_model=provider_model,
                supports_vision=supports_vision,
            )
            recipient = model_secret_recipient(
                model_id,
                command.provider_adapter,
                command.settings,
            )
            envelope = SecretEnvelope.protect(
                api_key.encode("utf-8"),
                recipient=recipient,
                key=key,
            )
            entries.append(
                DefaultSystemModelBootstrapEntry(
                    command=command,
                    model_id=model_id,
                    envelope=envelope,
                    envelope_digest=model_secret_envelope_digest(
                        recipient,
                        envelope,
                    ),
                )
            )
        # One DeepSeek Key protects one Provider envelope plus three
        # independent model envelopes; distinct nonces do not rotate the
        # actual upstream Key.
        provider_envelope = protect_provider_api_key(
            provider_id=DEEPSEEK_PROVIDER_ID,
            base_url=DEEPSEEK_PROVIDER_BASE_URL,
            api_key=api_key,
            key=key,
        )
        return DefaultSystemModelBootstrapMaterial(
            provider_id=DEEPSEEK_PROVIDER_ID,
            provider_name=DEEPSEEK_PROVIDER_NAME,
            provider_base_url=DEEPSEEK_PROVIDER_BASE_URL,
            models=tuple(entries),
            default_model_id=DEFAULT_MODEL_ID,
            provider_envelope=provider_envelope,
        )
    except (
        ModelProviderSecretInvalid,
        ModelSettingsInvalid,
        SecretKeyInvalid,
        SecretProtectionFailed,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        raise DefaultSystemModelBootstrapConfigurationInvalid() from None


async def _ensure_bootstrap_principal(session: AsyncSession) -> None:
    principal_id = str(BUILTIN_MODEL_USER_ID)
    principal = await session.get(UserRow, principal_id, with_for_update=True)
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
    state = (await session.execute(select(SystemModelCatalogStateRow).where(SystemModelCatalogStateRow.id == 1).with_for_update(read=True, of=SystemModelCatalogStateRow))).scalar_one_or_none()
    models = tuple((await session.execute(select(SystemModelConfigRow).order_by(SystemModelConfigRow.id).with_for_update(read=True, of=SystemModelConfigRow))).scalars().all())
    if state is None or not models:
        raise DefaultSystemModelBootstrapConflict
    live_model_ids = {model.id for model in models if model.deleted_at is None}
    if state.default_model_config_id is not None and state.default_model_config_id not in live_model_ids:
        raise DefaultSystemModelBootstrapConflict
    for model in models:
        command = validate_create_system_model(
            CreateSystemModel(
                display_name=model.display_name,
                status=model.status,
                provider_id=uuid.UUID(str(model.provider_id)),
                provider_adapter=model.provider_adapter,
                provider_model=model.provider_model,
                max_input_tokens=model.max_input_tokens,
                settings=dict(model.settings),
                supports_thinking=model.supports_thinking,
                supports_reasoning_effort=model.supports_reasoning_effort,
                supports_vision=model.supports_vision,
            ),
            allow_derived_base_url=True,
        )
        if canonical_model_payload_checksum(model.id, command) != model.payload_checksum:
            raise DefaultSystemModelBootstrapConflict
        if model.secret_revision < 0:
            raise DefaultSystemModelBootstrapConflict
        # A cleared API Key is a valid, intentionally unready model state.  A
        # repeated setup-db validates the catalog structure without requiring
        # operators to restore material that they explicitly destroyed.
        if model.current_secret_generation_id is None:
            continue
        if model.secret_revision < 1:
            raise DefaultSystemModelBootstrapConflict
        generation = await session.scalar(
            select(SystemModelSecretGenerationRow)
            .where(
                SystemModelSecretGenerationRow.id == model.current_secret_generation_id,
                SystemModelSecretGenerationRow.model_config_id == model.id,
                SystemModelSecretGenerationRow.revision == model.secret_revision,
            )
            .with_for_update(read=True, of=SystemModelSecretGenerationRow)
        )
        if generation is None:
            raise DefaultSystemModelBootstrapConflict


async def _bootstrap_fresh_catalog(
    session: AsyncSession,
    state: SystemModelCatalogStateRow,
    material: DefaultSystemModelBootstrapMaterial,
) -> None:
    ids = {entry.model_id for entry in material.models}
    if state.revision != 1 or state.default_model_config_id is not None or len(material.models) != 3 or len(ids) != 3 or material.default_model_id not in ids:
        raise DefaultSystemModelBootstrapConflict
    if await session.scalar(select(func.count()).select_from(SystemModelConfigRow)):
        raise DefaultSystemModelBootstrapConflict
    # Fixed Provider identity: never adopt or repair a same-name row that
    # belongs to a different identity, and never reuse a pre-existing fixed
    # ID while the text catalog is empty (that state is not ours).
    if await session.get(ModelProviderRow, material.provider_id) is not None:
        raise DefaultSystemModelBootstrapConflict
    if (
        await session.scalar(
            select(ModelProviderRow.id).where(
                ModelProviderRow.name == material.provider_name,
                ModelProviderRow.deleted_at.is_(None),
            )
        )
        is not None
    ):
        raise DefaultSystemModelBootstrapConflict
    await _ensure_bootstrap_principal(session)
    actor_id = str(BUILTIN_MODEL_USER_ID)
    provider = ModelProviderRow(
        id=material.provider_id,
        name=material.provider_name,
        base_url=material.provider_base_url,
        request_timeout_seconds=_DEEPSEEK_PROVIDER_REQUEST_TIMEOUT_SECONDS,
        api_key_nonce=material.provider_envelope.nonce,
        api_key_ciphertext=material.provider_envelope.ciphertext,
    )
    session.add(provider)
    # The provider row must land before the models that reference it.
    await session.flush()
    for entry in material.models:
        command = entry.command
        model = SystemModelConfigRow(
            id=entry.model_id,
            display_name=command.display_name,
            status=command.status,
            provider_id=material.provider_id,
            provider_adapter=command.provider_adapter,
            provider_model=command.provider_model,
            max_input_tokens=command.max_input_tokens,
            settings=dict(command.settings),
            supports_thinking=command.supports_thinking,
            supports_reasoning_effort=command.supports_reasoning_effort,
            supports_vision=command.supports_vision,
            payload_checksum=canonical_model_payload_checksum(entry.model_id, command),
            current_secret_generation_id=None,
            secret_revision=1,
            revision=1,
            created_by_user_id=actor_id,
            updated_by_user_id=actor_id,
        )
        session.add(model)
        await session.flush()
        generation = SystemModelSecretGenerationRow(
            model_config_id=model.id,
            revision=1,
            nonce=entry.envelope.nonce,
            ciphertext=entry.envelope.ciphertext,
            envelope_digest=entry.envelope_digest,
            created_by_user_id=actor_id,
        )
        session.add(generation)
        await session.flush()
        model.current_secret_generation_id = generation.id
        await session.flush()
    state.default_model_config_id = material.default_model_id
    state.revision += 1
    state.updated_by_user_id = actor_id
    await session.flush()


async def bootstrap_default_system_model(
    session_factory: async_sessionmaker[AsyncSession],
    material: DefaultSystemModelBootstrapMaterial | None,
) -> bool:
    """Create once with prepared material, or validate existing rows read-only."""

    if material is not None and not isinstance(
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
            model_count = await session.scalar(select(func.count()).select_from(SystemModelConfigRow))
            if model_count:
                await _validate_existing_catalog(session)
                return False
            if material is None:
                raise DefaultSystemModelBootstrapConflict
            state = (await session.execute(select(SystemModelCatalogStateRow).where(SystemModelCatalogStateRow.id == 1).with_for_update(of=SystemModelCatalogStateRow))).scalar_one_or_none()
            if state is None:
                raise DefaultSystemModelBootstrapConflict
            await _bootstrap_fresh_catalog(session, state, material)
            return True
    except (
        DefaultSystemModelBootstrapConfigurationInvalid,
        DefaultSystemModelBootstrapConflict,
    ):
        raise
    except IntegrityError:
        raise DefaultSystemModelBootstrapConflict from None
    except (DBAPIError, ModelSettingsInvalid):
        raise DefaultSystemModelBootstrapStorageUnavailable from None


__all__ = [
    "DEFAULT_MODEL_ID",
    "DEEPSEEK_PROVIDER_BASE_URL",
    "DEEPSEEK_PROVIDER_ID",
    "DEEPSEEK_PROVIDER_NAME",
    "DEEPSEEK_V4_MAX_INPUT_TOKENS",
    "DEEPSEEK_V4_FLASH_MODEL_ID",
    "DEEPSEEK_V4_FLASH_VISION_EXP_MODEL_ID",
    "DEEPSEEK_V4_PRO_MODEL_ID",
    "DefaultSystemModelBootstrapConfigurationInvalid",
    "DefaultSystemModelBootstrapConflict",
    "DefaultSystemModelBootstrapEntry",
    "DefaultSystemModelBootstrapMaterial",
    "DefaultSystemModelBootstrapStorageUnavailable",
    "bootstrap_default_system_model",
    "prepare_default_system_model_bootstrap",
]
