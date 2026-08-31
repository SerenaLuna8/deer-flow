"""Shared model-registry seeding for Knowledge tests (M9).

Knowledge Bases bind host-owned ``model_provider_models`` rows. These helpers
seed one Provider (with an API key the deterministic test SecretKey can open)
plus typed models, and build the production ``RegistryKnowledgeModelPort`` so
package services exercise the same binding locks and materialization paths as
the running system.
"""

from __future__ import annotations

import os
import uuid
from base64 import b64encode

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.knowledge.model_port import RegistryKnowledgeModelPort
from app.model_registry.secrets import protect_provider_api_key
from deerflow.persistence.model_registry import (
    ModelProviderModelRow,
    ModelProviderRow,
)
from deerflow.secrets import SecretKey

TEST_REGISTRY_API_KEY = "sk-test-registry-key"
_TEST_SECRET_KEY = b64encode(b"k" * 32).decode("ascii")

_SessionFactory = async_sessionmaker[AsyncSession]


def registry_secret_key() -> SecretKey:
    """Deterministic 32-byte key; the env round-trip is SecretKey's only ctor."""

    previous = os.environ.get("ACT_WEAVE_SECRET_KEY")
    os.environ["ACT_WEAVE_SECRET_KEY"] = _TEST_SECRET_KEY
    try:
        return SecretKey.from_environment()
    finally:
        if previous is None:
            del os.environ["ACT_WEAVE_SECRET_KEY"]
        else:
            os.environ["ACT_WEAVE_SECRET_KEY"] = previous


def registry_model_port() -> RegistryKnowledgeModelPort:
    return RegistryKnowledgeModelPort(secret_key=registry_secret_key())


async def seed_provider(
    factory: _SessionFactory,
    *,
    base_url: str = "https://provider.invalid/v1",
    request_timeout_seconds: int = 30,
    api_key: str = TEST_REGISTRY_API_KEY,
) -> uuid.UUID:
    provider_id = uuid.uuid4()
    envelope = protect_provider_api_key(
        provider_id=provider_id,
        base_url=base_url,
        api_key=api_key,
        key=registry_secret_key(),
    )
    async with factory() as session, session.begin():
        session.add(
            ModelProviderRow(
                id=provider_id,
                name=f"provider-{provider_id.hex[:12]}",
                base_url=base_url,
                request_timeout_seconds=request_timeout_seconds,
                api_key_nonce=envelope.nonce,
                api_key_ciphertext=envelope.ciphertext,
            )
        )
    return provider_id


async def seed_embedding_model(
    factory: _SessionFactory,
    provider_id: uuid.UUID,
    *,
    status: str = "active",
    dimension: int = 1024,
    max_batch: int = 64,
    model_name: str | None = None,
) -> uuid.UUID:
    model_id = uuid.uuid4()
    async with factory() as session, session.begin():
        session.add(
            ModelProviderModelRow(
                id=model_id,
                provider_id=provider_id,
                model_type="embedding",
                model_name=model_name or f"embed-{model_id.hex[:12]}",
                embedding_dimension=dimension,
                max_batch=max_batch,
                status=status,
            )
        )
    return model_id


async def seed_rerank_model(
    factory: _SessionFactory,
    provider_id: uuid.UUID,
    *,
    status: str = "active",
    max_batch: int = 32,
    model_name: str | None = None,
) -> uuid.UUID:
    model_id = uuid.uuid4()
    async with factory() as session, session.begin():
        session.add(
            ModelProviderModelRow(
                id=model_id,
                provider_id=provider_id,
                model_type="rerank",
                model_name=model_name or f"rerank-{model_id.hex[:12]}",
                embedding_dimension=None,
                max_batch=max_batch,
                status=status,
            )
        )
    return model_id


async def seed_registry_models(
    factory: _SessionFactory,
    *,
    status: str = "active",
    dimension: int = 1024,
    base_url: str = "https://provider.invalid/v1",
    api_key: str = TEST_REGISTRY_API_KEY,
) -> tuple[uuid.UUID, uuid.UUID]:
    """One Provider with one embedding and one rerank model; returns their IDs."""

    provider_id = await seed_provider(factory, base_url=base_url, api_key=api_key)
    embedding_id = await seed_embedding_model(factory, provider_id, status=status, dimension=dimension)
    rerank_id = await seed_rerank_model(factory, provider_id, status=status)
    return embedding_id, rerank_id


__all__ = [
    "TEST_REGISTRY_API_KEY",
    "registry_model_port",
    "registry_secret_key",
    "seed_embedding_model",
    "seed_provider",
    "seed_registry_models",
    "seed_rerank_model",
]
