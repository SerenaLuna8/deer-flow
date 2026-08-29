"""Install-time seeding interface consumed by the host ``setup-db`` flow.

This module is intentionally not part of the root public API: only the host
installation scripts (through ``backend/app/knowledge/bootstrap.py``) call it,
after the Knowledge tables are staged and before the Schema V1 marker is
published. It never talks to a model provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .persistence.models import KnowledgeModelConfigurationRow

_BOOTSTRAP_LOCK_KEY = 0x0DEE_12F1_0BEE_4B01


class KnowledgeBootstrapConflict(RuntimeError):
    def __init__(self) -> None:
        super().__init__("KNOWLEDGE_BOOTSTRAP_CONFLICT")


class KnowledgeBootstrapStorageUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("KNOWLEDGE_BOOTSTRAP_STORAGE_UNAVAILABLE")


@dataclass(frozen=True, slots=True)
class KnowledgeModelConfigurationSeed:
    """Pre-encrypted default configuration crossing the create-schema boundary."""

    configuration_id: UUID
    display_name: str
    base_url: str
    embedding_model: str
    embedding_dimension: int
    embedding_max_batch: int
    reranker_model: str
    reranker_max_batch: int
    request_timeout_seconds: int
    api_key_nonce: bytes = field(repr=False)
    api_key_ciphertext: bytes = field(repr=False)


async def install_default_model_configuration(
    session_factory: async_sessionmaker[AsyncSession],
    seed: KnowledgeModelConfigurationSeed,
) -> bool:
    """Seed an empty model catalog once; accept an already-seeded one read-only.

    A concurrent installer that lost the bootstrap race finds the winner's
    row and must succeed without writing, exactly like the default System
    Model bootstrap.
    """

    try:
        async with session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _BOOTSTRAP_LOCK_KEY},
            )
            existing = await session.scalar(select(func.count()).select_from(KnowledgeModelConfigurationRow))
            if existing:
                return False
            session.add(
                KnowledgeModelConfigurationRow(
                    id=seed.configuration_id,
                    display_name=seed.display_name,
                    status="active",
                    base_url=seed.base_url,
                    embedding_model=seed.embedding_model,
                    embedding_dimension=seed.embedding_dimension,
                    embedding_max_batch=seed.embedding_max_batch,
                    reranker_model=seed.reranker_model,
                    reranker_max_batch=seed.reranker_max_batch,
                    request_timeout_seconds=seed.request_timeout_seconds,
                    api_key_nonce=seed.api_key_nonce,
                    api_key_ciphertext=seed.api_key_ciphertext,
                )
            )
            return True
    except IntegrityError:
        raise KnowledgeBootstrapConflict from None
    except SQLAlchemyError:
        raise KnowledgeBootstrapStorageUnavailable from None
