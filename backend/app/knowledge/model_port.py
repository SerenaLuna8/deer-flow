"""``KnowledgeModelPort`` implementation over the host model registry.

The Knowledge package never imports host ORM tables; this adapter queries
``model_providers``/``model_provider_models`` with the caller's session,
validates type and ``active`` status, decrypts the Provider API Key, and hands
the package plain material DTOs. Every unresolvable model — missing, wrong
type, disabled, or with undecryptable material — surfaces as one
``KNOWLEDGE_MODEL_UNAVAILABLE`` error so callers cannot distinguish (or leak)
registry internals.
"""

from __future__ import annotations

import uuid

from actweave_knowledge import (
    KNOWLEDGE_MODEL_UNAVAILABLE,
    KnowledgeEmbeddingMaterial,
    KnowledgeError,
    KnowledgeModelType,
    KnowledgeRerankMaterial,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.model_registry.secrets import (
    ModelProviderSecretInvalid,
    materialize_provider_api_key,
)
from deerflow.models.runtime import ModelRuntime
from deerflow.persistence.knowledge_settings import KnowledgeSystemSettingsRow
from deerflow.persistence.model_registry import ModelProviderModelRow, ModelProviderRow
from deerflow.persistence.system_settings import SystemModelConfigRow
from deerflow.secrets import SecretKey, SecretKeyInvalid, SecretMaterializationFailed


def _model_unavailable() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE, "检索模型不存在或已停用")


def _summary_model_unavailable() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE, "摘要模型不存在或已停用")


class RegistryKnowledgeModelPort:
    """Bind-time locking and call-time materialization for registry models.

    ``model_runtime`` is the optional System-Model dispatch dependency for
    summary generation; composition wires it where summaries actually run
    (the Worker), and an unconfigured port rejects ``generate_summary`` with
    a typed ``KNOWLEDGE_MODEL_UNAVAILABLE`` error.
    """

    def __init__(
        self,
        *,
        secret_key: SecretKey,
        model_runtime: ModelRuntime | None = None,
    ) -> None:
        self._secret_key = secret_key
        self._model_runtime = model_runtime

    @classmethod
    def from_environment(cls) -> RegistryKnowledgeModelPort:
        return cls(secret_key=SecretKey.from_environment())

    async def lock_model_for_binding(
        self,
        session: AsyncSession,
        model_id: uuid.UUID,
        model_type: KnowledgeModelType,
    ) -> None:
        """Lock Provider then Model FOR SHARE and validate the binding target.

        The share locks serialize with the registry's FOR UPDATE write paths
        (disable, delete, endpoint changes), so a base can never commit a
        binding that passed its ``active`` check on a stale snapshot.
        ``provider_id`` is immutable, so resolving it before locking is safe;
        the model row is still re-read under its lock before validation.
        """

        provider_id = await session.scalar(select(ModelProviderModelRow.provider_id).where(ModelProviderModelRow.id == model_id))
        if provider_id is None:
            raise _model_unavailable()
        locked_provider = await session.scalar(select(ModelProviderRow.id).where(ModelProviderRow.id == provider_id).with_for_update(read=True))
        if locked_provider is None:  # pragma: no cover - RESTRICT FK keeps it alive
            raise _model_unavailable()
        model = await session.scalar(select(ModelProviderModelRow).where(ModelProviderModelRow.id == model_id).with_for_update(read=True))
        if model is None or model.model_type != model_type or model.status != "active":
            raise _model_unavailable()

    async def embedding_material(
        self,
        session: AsyncSession,
        model_id: uuid.UUID,
    ) -> KnowledgeEmbeddingMaterial:
        model, provider = await self._resolved_active_model(session, model_id, "embedding")
        if model.embedding_dimension is None:  # pragma: no cover - CHECK enforces the pairing
            raise _model_unavailable()
        return KnowledgeEmbeddingMaterial(
            model_id=model.id,
            base_url=provider.base_url,
            model_name=model.model_name,
            dimension=model.embedding_dimension,
            max_batch=model.max_batch,
            request_timeout_seconds=provider.request_timeout_seconds,
            api_key=self._decrypted_api_key(provider),
        )

    async def rerank_material(
        self,
        session: AsyncSession,
        model_id: uuid.UUID,
    ) -> KnowledgeRerankMaterial:
        model, provider = await self._resolved_active_model(session, model_id, "rerank")
        return KnowledgeRerankMaterial(
            model_id=model.id,
            base_url=provider.base_url,
            model_name=model.model_name,
            max_batch=model.max_batch,
            request_timeout_seconds=provider.request_timeout_seconds,
            api_key=self._decrypted_api_key(provider),
        )

    async def resolve_summary_model(
        self,
        session: AsyncSession,
    ) -> str | None:
        """Return the configured, active summary System Model reference.

        Reads the ``knowledge_system_settings`` singleton with the caller's
        session. A missing row or NULL ``summary_model_name`` means summaries
        are simply not configured (``None``); a configured reference that is
        malformed, missing, or not ``active`` raises the typed
        ``KNOWLEDGE_MODEL_UNAVAILABLE`` error instead of degrading silently.
        """

        model_name = await session.scalar(select(KnowledgeSystemSettingsRow.summary_model_name).where(KnowledgeSystemSettingsRow.id == 1))
        if model_name is None:
            return None
        try:
            model_id = uuid.UUID(model_name)
        except ValueError:
            raise _summary_model_unavailable() from None
        status = await session.scalar(select(SystemModelConfigRow.status).where(SystemModelConfigRow.id == model_id))
        if status != "active":
            raise _summary_model_unavailable()
        return model_name

    async def generate_summary(
        self,
        *,
        model_ref: str,
        prompt: str,
    ) -> str:
        """Generate one segment summary without holding a database session.

        T1 freezes the port signature only: an unconfigured runtime is the
        only reachable branch and raises the typed error. The actual
        ModelRuntime dispatch (with ``KNOWLEDGE_TASK_FAILED`` on call
        failure) lands with the M11 summary pipeline task.
        """

        if self._model_runtime is None:
            raise KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE, "摘要模型运行时未配置")
        raise NotImplementedError("summary generation dispatch lands with the M11 pipeline task")

    async def _resolved_active_model(
        self,
        session: AsyncSession,
        model_id: uuid.UUID,
        model_type: KnowledgeModelType,
    ) -> tuple[ModelProviderModelRow, ModelProviderRow]:
        result = (await session.execute(select(ModelProviderModelRow, ModelProviderRow).join(ModelProviderRow, ModelProviderRow.id == ModelProviderModelRow.provider_id).where(ModelProviderModelRow.id == model_id))).one_or_none()
        if result is None:
            raise _model_unavailable()
        model, provider = result
        if model.model_type != model_type or model.status != "active":
            raise _model_unavailable()
        return model, provider

    def _decrypted_api_key(self, provider: ModelProviderRow) -> str:
        try:
            return materialize_provider_api_key(
                provider_id=provider.id,
                base_url=provider.base_url,
                nonce=provider.api_key_nonce,
                ciphertext=provider.api_key_ciphertext,
                key=self._secret_key,
            )
        except (
            ModelProviderSecretInvalid,
            SecretKeyInvalid,
            SecretMaterializationFailed,
            UnicodeError,
            ValueError,
        ):
            raise _model_unavailable() from None


__all__ = ["RegistryKnowledgeModelPort"]
