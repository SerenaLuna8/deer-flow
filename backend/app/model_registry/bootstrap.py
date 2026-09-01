"""One-time protected bootstrap for the default Model Provider registry.

Mirrors the default System Model bootstrap: the plaintext API key is read from
an installation-only environment variable, encrypted before any DDL runs, and
only the encrypted copy crosses the create-schema boundary. The seed installs
one SiliconFlow Provider with one embedding model and one rerank model.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.model_registry.secrets import (
    ModelProviderSecretInvalid,
    protect_provider_api_key,
)
from deerflow.persistence.model_registry import (
    ModelProviderModelRow,
    ModelProviderRow,
)
from deerflow.secrets import SecretKey, SecretKeyInvalid, SecretProtectionFailed

_BOOTSTRAP_API_KEY_ENV = "ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY"
_BOOTSTRAP_SKIP_ENV = "ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP"
_BOOTSTRAP_LOCK_KEY = 0x0DEE_12F1_0BEE_4B02
_ID_NAMESPACE = uuid.UUID("7c2f1de8-4a0b-5c96-b3d4-52e6a8f90c17")

DEFAULT_MODEL_PROVIDER_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "model-registry:siliconflow:provider",
)
DEFAULT_EMBEDDING_MODEL_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "model-registry:siliconflow:embedding:qwen3-vl-embedding-8b",
)
DEFAULT_RERANK_MODEL_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "model-registry:siliconflow:rerank:qwen3-vl-reranker-8b",
)

_DEFAULT_PROVIDER_NAME = "SiliconFlow"
_DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
_DEFAULT_EMBEDDING_MODEL = "Qwen/Qwen3-VL-Embedding-8B"
_DEFAULT_EMBEDDING_DIMENSION = 4096
_DEFAULT_EMBEDDING_MAX_BATCH = 64
_DEFAULT_RERANK_MODEL = "Qwen/Qwen3-VL-Reranker-8B"
_DEFAULT_RERANK_MAX_BATCH = 32


class ModelRegistryBootstrapConfigurationInvalid(RuntimeError):
    """Secret-free preflight failure raised before first schema creation."""

    def __init__(self) -> None:
        super().__init__(
            "首次模型注册表初始化需要 ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_API_KEY 和有效的 ACT_WEAVE_SECRET_KEY；不使用 Knowledge 的部署可设置 ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP=1 显式跳过默认供应商初始化",
        )


class ModelRegistryBootstrapConflict(RuntimeError):
    def __init__(self) -> None:
        super().__init__("MODEL_REGISTRY_BOOTSTRAP_CONFLICT")


class ModelRegistryBootstrapStorageUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("MODEL_REGISTRY_BOOTSTRAP_STORAGE_UNAVAILABLE")


@dataclass(frozen=True, slots=True)
class ModelRegistryBootstrapSkipped:
    """Marker: the operator explicitly skipped seeding the default registry.

    Distinct from ``None`` so a caller that forgot the preflight entirely still
    fails loudly instead of silently installing without the seed.
    """


@dataclass(frozen=True, slots=True)
class ModelRegistrySeed:
    """Pre-encrypted default registry rows crossing the create-schema boundary."""

    provider_id: uuid.UUID
    provider_name: str
    base_url: str
    request_timeout_seconds: int
    embedding_model_id: uuid.UUID
    embedding_model_name: str
    embedding_dimension: int
    embedding_max_batch: int
    rerank_model_id: uuid.UUID
    rerank_model_name: str
    rerank_max_batch: int
    api_key_nonce: bytes = field(repr=False)
    api_key_ciphertext: bytes = field(repr=False)


def prepare_model_registry_bootstrap() -> ModelRegistrySeed | ModelRegistryBootstrapSkipped:
    """Validate and encrypt the default Provider's API key.

    Knowledge is an optional module: a deployment that never enables it may
    set ``ACT_WEAVE_BOOTSTRAP_MODEL_PROVIDER_SKIP=1`` to install Schema V1
    without the seeded Provider instead of supplying an API key. The tables
    still install; enabling Knowledge later only requires creating a Provider
    through the admin API.
    """

    if os.environ.get(_BOOTSTRAP_SKIP_ENV, "").strip() == "1":
        return ModelRegistryBootstrapSkipped()
    try:
        api_key = os.environ.get(_BOOTSTRAP_API_KEY_ENV)
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError
        envelope = protect_provider_api_key(
            provider_id=DEFAULT_MODEL_PROVIDER_ID,
            base_url=_DEFAULT_BASE_URL,
            api_key=api_key,
            key=SecretKey.from_environment(),
        )
        return ModelRegistrySeed(
            provider_id=DEFAULT_MODEL_PROVIDER_ID,
            provider_name=_DEFAULT_PROVIDER_NAME,
            base_url=_DEFAULT_BASE_URL,
            request_timeout_seconds=_DEFAULT_REQUEST_TIMEOUT_SECONDS,
            embedding_model_id=DEFAULT_EMBEDDING_MODEL_ID,
            embedding_model_name=_DEFAULT_EMBEDDING_MODEL,
            embedding_dimension=_DEFAULT_EMBEDDING_DIMENSION,
            embedding_max_batch=_DEFAULT_EMBEDDING_MAX_BATCH,
            rerank_model_id=DEFAULT_RERANK_MODEL_ID,
            rerank_model_name=_DEFAULT_RERANK_MODEL,
            rerank_max_batch=_DEFAULT_RERANK_MAX_BATCH,
            api_key_nonce=envelope.nonce,
            api_key_ciphertext=envelope.ciphertext,
        )
    except (
        ModelProviderSecretInvalid,
        SecretKeyInvalid,
        SecretProtectionFailed,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        raise ModelRegistryBootstrapConfigurationInvalid() from None


async def bootstrap_default_model_registry(
    session_factory: async_sessionmaker[AsyncSession],
    seed: ModelRegistrySeed,
) -> bool:
    """Seed the fixed SiliconFlow identity once; accept it read-only after.

    Installed-ness is decided by the seed's fixed Provider UUID, so other
    Providers (for example the seeded DeepSeek text-model Provider) never
    make this seed skip. A concurrent installer that lost the bootstrap race
    finds the winner's row and must succeed without writing.
    """

    try:
        async with session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _BOOTSTRAP_LOCK_KEY},
            )
            # Installed-ness is the seed's fixed identity, not a row count:
            # the DeepSeek text-model Provider (or any admin-created Provider)
            # existing first must not skip this seed. An existing fixed ID
            # returns read-only, preserving manual renames, URL/timeout/Key
            # changes, model status changes, and deletions.
            existing = await session.get(ModelProviderRow, seed.provider_id)
            if existing is not None:
                return False
            name_taken = await session.scalar(
                select(ModelProviderRow.id).where(
                    ModelProviderRow.name == seed.provider_name,
                    ModelProviderRow.deleted_at.is_(None),
                )
            )
            if name_taken is not None:
                # The default name belongs to a different identity; never
                # reuse or repair that row.
                raise ModelRegistryBootstrapConflict
            session.add(
                ModelProviderRow(
                    id=seed.provider_id,
                    name=seed.provider_name,
                    base_url=seed.base_url,
                    request_timeout_seconds=seed.request_timeout_seconds,
                    api_key_nonce=seed.api_key_nonce,
                    api_key_ciphertext=seed.api_key_ciphertext,
                )
            )
            # Without a relationship the unit of work does not order these
            # inserts by foreign key; the provider row must land first.
            await session.flush()
            session.add(
                ModelProviderModelRow(
                    id=seed.embedding_model_id,
                    provider_id=seed.provider_id,
                    model_type="embedding",
                    model_name=seed.embedding_model_name,
                    embedding_dimension=seed.embedding_dimension,
                    max_batch=seed.embedding_max_batch,
                    status="active",
                )
            )
            session.add(
                ModelProviderModelRow(
                    id=seed.rerank_model_id,
                    provider_id=seed.provider_id,
                    model_type="rerank",
                    model_name=seed.rerank_model_name,
                    embedding_dimension=None,
                    max_batch=seed.rerank_max_batch,
                    status="active",
                )
            )
            return True
    except IntegrityError:
        raise ModelRegistryBootstrapConflict from None
    except SQLAlchemyError:
        raise ModelRegistryBootstrapStorageUnavailable from None


__all__ = [
    "DEFAULT_EMBEDDING_MODEL_ID",
    "DEFAULT_MODEL_PROVIDER_ID",
    "DEFAULT_RERANK_MODEL_ID",
    "ModelRegistryBootstrapConfigurationInvalid",
    "ModelRegistryBootstrapConflict",
    "ModelRegistryBootstrapSkipped",
    "ModelRegistryBootstrapStorageUnavailable",
    "ModelRegistrySeed",
    "bootstrap_default_model_registry",
    "prepare_model_registry_bootstrap",
]
