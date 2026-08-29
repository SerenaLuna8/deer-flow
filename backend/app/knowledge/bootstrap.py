"""One-time protected bootstrap for the default Knowledge model configuration.

Mirrors the default System Model bootstrap: the plaintext API key is read from
an installation-only environment variable, encrypted before any DDL runs, and
only the encrypted copy crosses the create-schema boundary.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from actweave_knowledge.bootstrap import (
    KnowledgeModelConfigurationSeed,
    install_default_model_configuration,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.knowledge.secret_adapter import EnvelopeKnowledgeSecretAdapter
from deerflow.secrets import SecretKeyInvalid, SecretProtectionFailed

_BOOTSTRAP_API_KEY_ENV = "ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY"
_BOOTSTRAP_SKIP_ENV = "ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_SKIP"
_ID_NAMESPACE = uuid.UUID("7c2f1de8-4a0b-5c96-b3d4-52e6a8f90c17")

KNOWLEDGE_DEFAULT_MODEL_CONFIGURATION_ID = uuid.uuid5(
    _ID_NAMESPACE,
    "knowledge:siliconflow-qwen3-vl-retrieval:model-configuration",
)


class KnowledgeBootstrapConfigurationInvalid(RuntimeError):
    """Secret-free preflight failure raised before first schema creation."""

    def __init__(self) -> None:
        super().__init__(
            "首次 Knowledge 初始化需要 ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_API_KEY 和有效的 ACT_WEAVE_SECRET_KEY；不使用 Knowledge 的部署可设置 ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_SKIP=1 显式跳过默认配置初始化",
        )


@dataclass(frozen=True, slots=True)
class KnowledgeBootstrapSkipped:
    """Marker: the operator explicitly skipped seeding the default configuration.

    Distinct from ``None`` so a caller that forgot the preflight entirely still
    fails loudly instead of silently installing without the seed.
    """


def prepare_knowledge_bootstrap() -> KnowledgeModelConfigurationSeed | KnowledgeBootstrapSkipped:
    """Validate and encrypt the default retrieval configuration's API key.

    Knowledge is an optional module: a deployment that never enables it may
    set ``ACT_WEAVE_BOOTSTRAP_KNOWLEDGE_SKIP=1`` to install Schema V1 without
    the seeded configuration instead of supplying a provider API key. The
    tables still install; enabling Knowledge later only requires creating a
    model configuration through the admin API.
    """

    if os.environ.get(_BOOTSTRAP_SKIP_ENV, "").strip() == "1":
        return KnowledgeBootstrapSkipped()
    try:
        api_key = os.environ.get(_BOOTSTRAP_API_KEY_ENV)
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError
        adapter = EnvelopeKnowledgeSecretAdapter.from_environment()
        protected = adapter.protect_api_key(
            KNOWLEDGE_DEFAULT_MODEL_CONFIGURATION_ID,
            api_key,
        )
        return KnowledgeModelConfigurationSeed(
            configuration_id=KNOWLEDGE_DEFAULT_MODEL_CONFIGURATION_ID,
            display_name="SiliconFlow Qwen3-VL Retrieval",
            base_url="https://api.siliconflow.cn/v1",
            embedding_model="Qwen/Qwen3-VL-Embedding-8B",
            embedding_dimension=4096,
            embedding_max_batch=64,
            reranker_model="Qwen/Qwen3-VL-Reranker-8B",
            reranker_max_batch=32,
            request_timeout_seconds=30,
            api_key_nonce=protected.nonce,
            api_key_ciphertext=protected.ciphertext,
        )
    except (SecretKeyInvalid, SecretProtectionFailed, TypeError, UnicodeError, ValueError):
        raise KnowledgeBootstrapConfigurationInvalid() from None


async def bootstrap_default_knowledge_model_configuration(
    session_factory: async_sessionmaker[AsyncSession],
    seed: KnowledgeModelConfigurationSeed,
) -> bool:
    """Insert the pre-encrypted seed once the Knowledge tables are staged."""

    return await install_default_model_configuration(session_factory, seed)
