"""Host-owned retrieval model registry: Providers plus typed models.

One Provider row is one OpenAI-compatible endpoint with one encrypted API
Key; its model rows are type-specific (``embedding``/``rerank``) and have an
immutable identity — changing name, type, or dimension means creating a new
row. Endpoint-affecting updates follow freeze → probe → settle: material is
snapshotted in a short transaction, probed outside any transaction, then
re-locked, compared, and committed, so a mid-flight concurrent change turns
into ``KNOWLEDGE_CONFLICT`` instead of persisting an unprobed combination.

Reference protection is delegated: ``model_in_use`` is the Knowledge
package's own query (both binding columns, deleting bases included), executed
inside this service's transactions so FOR UPDATE row locks serialize it
against binding writes. Errors reuse the ``KNOWLEDGE_*`` code space because
the registry exists for the Knowledge module and shares its admin surface.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_NAME_CONFLICT,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeEmbeddingMaterial,
    KnowledgeError,
    KnowledgeRerankMaterial,
)
from actweave_knowledge.models import KnowledgeModelClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditOutcome,
    AuditTarget,
    AuditTargetKind,
    SystemAuditContext,
    is_issued_system_audit_context,
)
from app.audit.service import AuditService
from app.model_registry.secrets import (
    ModelProviderSecretInvalid,
    materialize_provider_api_key,
    protect_provider_api_key,
)
from app.system_settings.provider_key_fanout import (
    ProviderKeyFanout,
    ProviderKeyFanoutInvariant,
    ProviderKeyFanoutLockBusy,
    count_bound_text_models,
)
from app.system_settings.repository import (
    SystemModelRepository,
    SystemModelRepositoryInvariant,
)
from app.system_settings.validation import ModelSettingsInvalid
from deerflow.persistence.model_registry import (
    ModelProviderModelRow,
    ModelProviderRow,
)
from deerflow.persistence.user.model import UserRow
from deerflow.secrets import (
    SecretKey,
    SecretKeyInvalid,
    SecretMaterializationFailed,
    SecretProtectionFailed,
)

MAX_PROVIDER_NAME_LENGTH = 120
MAX_BASE_URL_LENGTH = 1024
MAX_MODEL_NAME_LENGTH = 255
MIN_REQUEST_TIMEOUT_SECONDS = 1
MAX_REQUEST_TIMEOUT_SECONDS = 300
MAX_EMBEDDING_DIMENSION = 16000
MAX_EMBEDDING_BATCH = 2048
MAX_RERANK_BATCH = 256

ModelInUseCheck = Callable[[AsyncSession, uuid.UUID], Awaitable[bool]]


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelProviderView:
    id: uuid.UUID
    name: str
    base_url: str
    request_timeout_seconds: int
    api_key_configured: bool
    model_count: int
    active_model_count: int
    endpoint_frozen: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderModelView:
    id: uuid.UUID
    provider_id: uuid.UUID
    model_type: str
    model_name: str
    embedding_dimension: int | None
    max_batch: int
    status: str
    in_use: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderModelTestResult:
    ok: bool
    message: str


@dataclass(frozen=True, slots=True)
class RetrievalModelOption:
    """One active registry model exposed to project members for binding."""

    id: uuid.UUID
    provider_name: str
    model_name: str
    embedding_dimension: int | None


# ---------------------------------------------------------------------------
# Frozen snapshots for the freeze -> probe -> settle flows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FrozenProvider:
    base_url: str
    request_timeout_seconds: int
    api_key_nonce: bytes = field(repr=False)
    api_key_ciphertext: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class _FrozenModel:
    id: uuid.UUID
    model_type: str
    model_name: str
    embedding_dimension: int | None
    max_batch: int
    status: str


def _freeze_provider(provider: ModelProviderRow) -> _FrozenProvider:
    return _FrozenProvider(
        base_url=provider.base_url,
        request_timeout_seconds=provider.request_timeout_seconds,
        api_key_nonce=bytes(provider.api_key_nonce),
        api_key_ciphertext=bytes(provider.api_key_ciphertext),
    )


def _freeze_model(model: ModelProviderModelRow) -> _FrozenModel:
    return _FrozenModel(
        id=model.id,
        model_type=model.model_type,
        model_name=model.model_name,
        embedding_dimension=model.embedding_dimension,
        max_batch=model.max_batch,
        status=model.status,
    )


def _provider_matches(provider: ModelProviderRow, frozen: _FrozenProvider) -> bool:
    return (
        provider.base_url == frozen.base_url
        and provider.request_timeout_seconds == frozen.request_timeout_seconds
        and bytes(provider.api_key_nonce) == frozen.api_key_nonce
        and bytes(provider.api_key_ciphertext) == frozen.api_key_ciphertext
    )


# ---------------------------------------------------------------------------
# Errors and validation
# ---------------------------------------------------------------------------


def _not_found() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")


def _invalid(message: str) -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_INVALID_REQUEST, message)


def _conflict() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_CONFLICT, "供应商配置刚被其他管理员修改，请刷新后重试")


def _models_busy() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_CONFLICT, "模型正在使用，请稍后重试")


def _storage_unavailable() -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "模型注册表存储暂时不可用")


def _validated_provider_name(name: object) -> str:
    if type(name) is not str:
        raise _invalid("供应商名称必须是字符串")
    cleaned = name.strip()
    if not cleaned or len(cleaned) > MAX_PROVIDER_NAME_LENGTH:
        raise _invalid(f"供应商名称长度需在 1-{MAX_PROVIDER_NAME_LENGTH} 字符之间")
    return cleaned


def _validated_base_url(base_url: object) -> str:
    if type(base_url) is not str:
        raise _invalid("服务地址必须是字符串")
    cleaned = base_url.strip().rstrip("/")
    if not cleaned or len(cleaned) > MAX_BASE_URL_LENGTH:
        raise _invalid(f"服务地址长度需在 1-{MAX_BASE_URL_LENGTH} 字符之间")
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        raise _invalid("服务地址不是合法的 URL") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _invalid("服务地址必须是 http(s) URL")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise _invalid("服务地址不能携带查询串、片段或用户信息")
    return cleaned


def _validated_timeout(value: object) -> int:
    if type(value) is not int or not (MIN_REQUEST_TIMEOUT_SECONDS <= value <= MAX_REQUEST_TIMEOUT_SECONDS):
        raise _invalid(f"请求超时需在 {MIN_REQUEST_TIMEOUT_SECONDS}-{MAX_REQUEST_TIMEOUT_SECONDS} 秒之间")
    return value


def _validated_api_key(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise _invalid("API Key 不能为空")
    return value


def _validated_model_name(name: object) -> str:
    if type(name) is not str:
        raise _invalid("模型名称必须是字符串")
    cleaned = name.strip()
    if not cleaned or len(cleaned) > MAX_MODEL_NAME_LENGTH:
        raise _invalid(f"模型名称长度需在 1-{MAX_MODEL_NAME_LENGTH} 字符之间")
    return cleaned


def _validated_model_shape(
    model_type: object,
    embedding_dimension: object,
    max_batch: object,
) -> tuple[str, int | None, int]:
    if model_type not in {"embedding", "rerank"}:
        raise _invalid("模型类型必须是 embedding 或 rerank")
    if model_type == "embedding":
        if type(embedding_dimension) is not int or not (1 <= embedding_dimension <= MAX_EMBEDDING_DIMENSION):
            raise _invalid(f"Embedding 维度需在 1-{MAX_EMBEDDING_DIMENSION} 之间")
        if type(max_batch) is not int or not (1 <= max_batch <= MAX_EMBEDDING_BATCH):
            raise _invalid(f"Embedding 批量上限需在 1-{MAX_EMBEDDING_BATCH} 之间")
        return "embedding", embedding_dimension, max_batch
    if embedding_dimension is not None:
        raise _invalid("Rerank 模型不接受 Embedding 维度")
    if type(max_batch) is not int or not (1 <= max_batch <= MAX_RERANK_BATCH):
        raise _invalid(f"Rerank 批量上限需在 1-{MAX_RERANK_BATCH} 之间")
    return "rerank", None, max_batch


_PROVIDER_NAME_CONFLICT = KnowledgeError(KNOWLEDGE_NAME_CONFLICT, "同名供应商已存在")
_MODEL_NAME_CONFLICT = KnowledgeError(
    KNOWLEDGE_NAME_CONFLICT,
    "该供应商下已存在同类型同名模型",
)


# ---------------------------------------------------------------------------
# Project-facing options (no admin authority required)
# ---------------------------------------------------------------------------


async def list_active_retrieval_model_options(
    session: AsyncSession,
) -> tuple[list[RetrievalModelOption], list[RetrievalModelOption]]:
    """Return (embedding, rerank) options every project member may bind."""

    rows = (
        await session.execute(
            select(ModelProviderModelRow, ModelProviderRow.name)
            .join(
                ModelProviderRow,
                ModelProviderRow.id == ModelProviderModelRow.provider_id,
            )
            .where(ModelProviderModelRow.status == "active")
            .order_by(
                ModelProviderRow.name,
                ModelProviderModelRow.model_name,
                ModelProviderModelRow.id,
            )
        )
    ).all()
    embedding: list[RetrievalModelOption] = []
    rerank: list[RetrievalModelOption] = []
    for model, provider_name in rows:
        option = RetrievalModelOption(
            id=model.id,
            provider_name=provider_name,
            model_name=model.model_name,
            embedding_dimension=model.embedding_dimension,
        )
        if model.model_type == "embedding":
            embedding.append(option)
        else:
            rerank.append(option)
    return embedding, rerank


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ModelRegistryService:
    """Admin CRUD plus typed probes over the registry tables."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        secret_key: SecretKey,
        client: KnowledgeModelClient,
        model_in_use: ModelInUseCheck,
        audit_service: AuditService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_key = secret_key
        self._client = client
        self._model_in_use = model_in_use
        self._audit_service = audit_service
        # Provider Keys fan out to bound text models inside this service's
        # settle transaction; the collaborator owns only the locked-scope
        # re-encryption and per-model audit, never the transaction itself.
        self._key_fanout = ProviderKeyFanout(
            secret_key=secret_key,
            audit_service=audit_service,
        )

    # -- admin authority -------------------------------------------------

    @staticmethod
    def _require_issued(context: object) -> SystemAuditContext:
        if not is_issued_system_audit_context(context):
            raise _not_found()
        return context

    @staticmethod
    async def _lock_admin(
        session: AsyncSession,
        context: SystemAuditContext,
        *,
        write: bool = True,
    ) -> None:
        """Re-verify system_admin under a row lock inside this transaction.

        Mutations take FOR UPDATE so a concurrent demotion serializes against
        registry writes; read-only lists take FOR SHARE so concurrent reads do
        not serialize on the admin row while still blocking a demotion.
        """

        statement = select(UserRow.id, UserRow.system_role).where(UserRow.id == str(context.user_id)).with_for_update(of=UserRow, read=not write)
        row = (await session.execute(statement)).one_or_none()
        if row is None or row.system_role != "system_admin":
            raise _not_found()

    async def _append_audit(
        self,
        session: AsyncSession,
        context: SystemAuditContext,
        *,
        action: AuditAction,
        target_id: uuid.UUID,
        outcome: AuditOutcome,
        metadata: dict[str, object],
    ) -> None:
        if self._audit_service is None:
            return
        await self._audit_service.append(
            session,
            AuditActor.system_admin(context),
            action,
            # Rows loaded through asyncpg surface a uuid.UUID subclass that the
            # audit target's exact-type check rejects; normalize first.
            AuditTarget(AuditTargetKind.ASSET, uuid.UUID(str(target_id)), None),
            outcome,
            metadata,
            request_id=context.request_id,
        )

    # -- providers -------------------------------------------------------

    async def list_providers(self, context: object) -> list[ModelProviderView]:
        issued = self._require_issued(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued, write=False)
                providers = (await session.scalars(select(ModelProviderRow).order_by(ModelProviderRow.created_at, ModelProviderRow.id))).all()
                views: list[ModelProviderView] = []
                for provider in providers:
                    models = (await session.scalars(select(ModelProviderModelRow).where(ModelProviderModelRow.provider_id == provider.id))).all()
                    text_total, text_active = await count_bound_text_models(
                        session,
                        provider.id,
                    )
                    views.append(
                        ModelProviderView(
                            id=provider.id,
                            name=provider.name,
                            base_url=provider.base_url,
                            request_timeout_seconds=provider.request_timeout_seconds,
                            api_key_configured=True,
                            # Counts aggregate bound text models with typed
                            # retrieval models; endpoint freezing stays a pure
                            # Embedding-reference decision.
                            model_count=len(models) + text_total,
                            active_model_count=sum(1 for model in models if model.status == "active") + text_active,
                            endpoint_frozen=await self._any_embedding_in_use(session, models),
                            created_at=provider.created_at,
                            updated_at=provider.updated_at,
                        )
                    )
                return views
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def create_provider(
        self,
        context: object,
        *,
        name: str,
        base_url: str,
        request_timeout_seconds: int,
        api_key: str,
    ) -> ModelProviderView:
        issued = self._require_issued(context)
        cleaned_name = _validated_provider_name(name)
        cleaned_base_url = _validated_base_url(base_url)
        timeout = _validated_timeout(request_timeout_seconds)
        plaintext = _validated_api_key(api_key)
        provider_id = uuid.uuid4()
        envelope = self._protect(provider_id, cleaned_base_url, plaintext)
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued)
                provider = ModelProviderRow(
                    id=provider_id,
                    name=cleaned_name,
                    base_url=cleaned_base_url,
                    request_timeout_seconds=timeout,
                    api_key_nonce=envelope.nonce,
                    api_key_ciphertext=envelope.ciphertext,
                )
                session.add(provider)
                await session.flush()
                await self._append_audit(
                    session,
                    issued,
                    action=AuditAction.ASSET_CREATED,
                    target_id=provider.id,
                    outcome=AuditOutcome.SUCCESS,
                    metadata={
                        "asset_kind": "model_provider",
                        "operation": "model_provider.create",
                    },
                )
                return ModelProviderView(
                    id=provider.id,
                    name=provider.name,
                    base_url=provider.base_url,
                    request_timeout_seconds=provider.request_timeout_seconds,
                    api_key_configured=True,
                    model_count=0,
                    active_model_count=0,
                    endpoint_frozen=False,
                    created_at=provider.created_at,
                    updated_at=provider.updated_at,
                )
        except KnowledgeError:
            raise
        except IntegrityError:
            raise _PROVIDER_NAME_CONFLICT from None
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def update_provider(
        self,
        context: object,
        provider_id: uuid.UUID,
        *,
        name: str | None = None,
        base_url: str | None = None,
        request_timeout_seconds: int | None = None,
        api_key: str | None = None,
    ) -> ModelProviderView:
        issued = self._require_issued(context)
        cleaned_name = _validated_provider_name(name) if name is not None else None
        cleaned_base_url = _validated_base_url(base_url) if base_url is not None else None
        timeout = _validated_timeout(request_timeout_seconds) if request_timeout_seconds is not None else None
        plaintext = _validated_api_key(api_key) if api_key is not None else None

        # Freeze: snapshot material and the full sub-model set.
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued)
                provider = await session.get(ModelProviderRow, provider_id)
                if provider is None:
                    raise _not_found()
                frozen = _freeze_provider(provider)
                models = await self._provider_models(session, provider_id)
                frozen_models = tuple(_freeze_model(model) for model in models)
                embedding_in_use = await self._any_embedding_in_use(session, models)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

        new_base_url = cleaned_base_url if cleaned_base_url is not None else frozen.base_url
        new_timeout = timeout if timeout is not None else frozen.request_timeout_seconds
        base_url_changed = new_base_url != frozen.base_url
        material_changed = base_url_changed or new_timeout != frozen.request_timeout_seconds or plaintext is not None
        if base_url_changed:
            if embedding_in_use:
                raise _invalid("该供应商存在被知识库引用的 Embedding 模型，不能修改服务地址")
            if plaintext is None:
                raise _invalid("修改服务地址必须重新提交 API Key")

        # Probe: every active sub-model must answer with the merged material.
        active_models = [model for model in frozen_models if model.status == "active"]
        if material_changed and active_models:
            probe_key = plaintext if plaintext is not None else self._materialize(provider_id, frozen)
            for model in active_models:
                await self._probe_frozen_model(
                    model,
                    base_url=new_base_url,
                    request_timeout_seconds=new_timeout,
                    api_key=probe_key,
                )

        envelope = self._protect(provider_id, new_base_url, plaintext) if plaintext is not None else None

        # Name, URL, and Key changes surface on bound text models (display
        # name in the admin catalog; URL/Key through fan-out), so those
        # settles follow the unified admin → catalog → provider → models →
        # generation order. A timeout-only change is retrieval-scoped and
        # never takes the catalog lock.
        touches_text_models = cleaned_name is not None or cleaned_base_url is not None or plaintext is not None

        # Settle: re-lock, compare with the frozen snapshot, then commit.
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued)
                catalog_state = await SystemModelRepository(session).catalog_state(for_update=True) if touches_text_models else None
                provider = await session.get(ModelProviderRow, provider_id, with_for_update=True)
                if provider is None:
                    raise _not_found()
                current_models = await self._provider_models(session, provider_id, for_update=True)
                if material_changed:
                    if not _provider_matches(provider, frozen):
                        raise _conflict()
                    if tuple(_freeze_model(model) for model in current_models) != frozen_models:
                        raise _conflict()
                if base_url_changed and await self._any_embedding_in_use(session, current_models):
                    raise _invalid("该供应商存在被知识库引用的 Embedding 模型，不能修改服务地址")
                name_changed = cleaned_name is not None and provider.name != cleaned_name
                if cleaned_name is not None:
                    provider.name = cleaned_name
                # Assign only caller-provided fields: a rename-only settle
                # skips the material comparison above, so writing back the
                # frozen endpoint values here would silently revert a
                # concurrent base_url/timeout/Key update committed between
                # freeze and settle (and orphan its origin-bound ciphertext).
                if cleaned_base_url is not None:
                    provider.base_url = new_base_url
                if timeout is not None:
                    provider.request_timeout_seconds = new_timeout
                if envelope is not None:
                    provider.api_key_nonce = envelope.nonce
                    provider.api_key_ciphertext = envelope.ciphertext
                provider.updated_at = datetime.now(UTC)
                await session.flush()
                # Fan-out re-encrypts the validated plaintext for the current
                # bound set — including suspended models and any model created
                # or rebound here since freeze — never a frozen member list.
                if plaintext is not None:
                    await self._key_fanout.rotate_bound_text_models(
                        session,
                        issued,
                        provider_id=uuid.UUID(str(provider.id)),
                        base_url=provider.base_url,
                        api_key=plaintext,
                    )
                text_total, text_active = await count_bound_text_models(
                    session,
                    provider.id,
                )
                if catalog_state is not None and text_total > 0 and (plaintext is not None or name_changed):
                    catalog_state.revision += 1
                    catalog_state.updated_by_user_id = str(issued.user_id)
                    await session.flush()
                key_only_rotation = plaintext is not None and not base_url_changed and new_timeout == frozen.request_timeout_seconds and cleaned_name is None
                await self._append_audit(
                    session,
                    issued,
                    action=AuditAction.ASSET_UPDATED,
                    target_id=provider.id,
                    outcome=AuditOutcome.SUCCESS,
                    metadata={
                        "asset_kind": "model_provider",
                        "operation": ("model_provider.secret.replace" if key_only_rotation else "model_provider.update"),
                    },
                )
                return ModelProviderView(
                    id=provider.id,
                    name=provider.name,
                    base_url=provider.base_url,
                    request_timeout_seconds=provider.request_timeout_seconds,
                    api_key_configured=True,
                    model_count=len(current_models) + text_total,
                    active_model_count=sum(1 for model in current_models if model.status == "active") + text_active,
                    endpoint_frozen=await self._any_embedding_in_use(session, current_models),
                    created_at=provider.created_at,
                    updated_at=provider.updated_at,
                )
        except KnowledgeError:
            raise
        except ProviderKeyFanoutLockBusy:
            # Only an explicit NOWAIT lock-busy signal becomes a retryable
            # 409; other database failures keep the 503 storage semantics.
            raise _models_busy() from None
        except (
            ModelSettingsInvalid,
            ProviderKeyFanoutInvariant,
            SystemModelRepositoryInvariant,
        ):
            raise _storage_unavailable() from None
        except IntegrityError:
            raise _PROVIDER_NAME_CONFLICT from None
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def delete_provider(self, context: object, provider_id: uuid.UUID) -> None:
        issued = self._require_issued(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued)
                provider = await session.get(ModelProviderRow, provider_id, with_for_update=True)
                if provider is None:
                    raise _not_found()
                # Any bound text model — suspended included — blocks deletion;
                # rebind text models first, then remove retrieval models.
                text_total, _text_active = await count_bound_text_models(
                    session,
                    provider_id,
                )
                if text_total:
                    raise _invalid("该供应商下仍有绑定的文本模型，请先将它们改绑到其他供应商")
                models = await self._provider_models(session, provider_id, for_update=True)
                if models:
                    raise _invalid("该供应商下仍有模型，请先删除全部模型")
                await session.delete(provider)
                await session.flush()
                await self._append_audit(
                    session,
                    issued,
                    action=AuditAction.ASSET_DELETED,
                    target_id=provider_id,
                    outcome=AuditOutcome.SUCCESS,
                    metadata={
                        "asset_kind": "model_provider",
                        "operation": "model_provider.delete",
                    },
                )
        except KnowledgeError:
            raise
        except IntegrityError:
            raise _invalid("该供应商下仍有模型，请先删除全部模型") from None
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    # -- models ----------------------------------------------------------

    async def list_models(
        self,
        context: object,
        provider_id: uuid.UUID,
    ) -> list[ProviderModelView]:
        issued = self._require_issued(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued, write=False)
                provider = await session.get(ModelProviderRow, provider_id)
                if provider is None:
                    raise _not_found()
                models = (
                    await session.scalars(
                        select(ModelProviderModelRow)
                        .where(ModelProviderModelRow.provider_id == provider_id)
                        .order_by(
                            ModelProviderModelRow.created_at,
                            ModelProviderModelRow.id,
                        )
                    )
                ).all()
                return [await self._model_view(session, model) for model in models]
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def create_model(
        self,
        context: object,
        provider_id: uuid.UUID,
        *,
        model_type: str,
        model_name: str,
        embedding_dimension: int | None,
        max_batch: int,
    ) -> ProviderModelView:
        issued = self._require_issued(context)
        cleaned_name = _validated_model_name(model_name)
        cleaned_type, dimension, batch = _validated_model_shape(
            model_type,
            embedding_dimension,
            max_batch,
        )

        # Freeze the Provider material the probe will exercise.
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued)
                provider = await session.get(ModelProviderRow, provider_id)
                if provider is None:
                    raise _not_found()
                frozen = _freeze_provider(provider)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

        # Creation must prove the model answers before it becomes bindable.
        model_id = uuid.uuid4()
        await self._probe_frozen_model(
            _FrozenModel(
                id=model_id,
                model_type=cleaned_type,
                model_name=cleaned_name,
                embedding_dimension=dimension,
                max_batch=batch,
                status="active",
            ),
            base_url=frozen.base_url,
            request_timeout_seconds=frozen.request_timeout_seconds,
            api_key=self._materialize(provider_id, frozen),
        )

        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued)
                provider = await session.get(ModelProviderRow, provider_id, with_for_update=True)
                if provider is None:
                    raise _not_found()
                if not _provider_matches(provider, frozen):
                    raise _conflict()
                model = ModelProviderModelRow(
                    id=model_id,
                    provider_id=provider_id,
                    model_type=cleaned_type,
                    model_name=cleaned_name,
                    embedding_dimension=dimension,
                    max_batch=batch,
                    status="active",
                )
                session.add(model)
                await session.flush()
                await self._append_audit(
                    session,
                    issued,
                    action=AuditAction.ASSET_CREATED,
                    target_id=model.id,
                    outcome=AuditOutcome.SUCCESS,
                    metadata={
                        "asset_kind": "provider_model",
                        "operation": "provider_model.create",
                    },
                )
                return ProviderModelView(
                    id=model.id,
                    provider_id=model.provider_id,
                    model_type=model.model_type,
                    model_name=model.model_name,
                    embedding_dimension=model.embedding_dimension,
                    max_batch=model.max_batch,
                    status=model.status,
                    in_use=False,
                    created_at=model.created_at,
                    updated_at=model.updated_at,
                )
        except KnowledgeError:
            raise
        except IntegrityError:
            raise _MODEL_NAME_CONFLICT from None
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def set_model_status(
        self,
        context: object,
        model_id: uuid.UUID,
        status: str,
    ) -> ProviderModelView:
        issued = self._require_issued(context)
        if status not in {"active", "disabled"}:
            raise _invalid("模型状态必须是 active 或 disabled")
        if status == "disabled":
            return await self._disable_model(issued, model_id)
        return await self._enable_model(issued, model_id)

    async def _disable_model(
        self,
        issued: SystemAuditContext,
        model_id: uuid.UUID,
    ) -> ProviderModelView:
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued)
                model = await self._locked_model(session, model_id)
                if model.status == "disabled":
                    return await self._model_view(session, model)
                if await self._model_in_use(session, model.id):
                    raise _invalid("该模型正被知识库引用，不能停用")
                model.status = "disabled"
                model.updated_at = datetime.now(UTC)
                await session.flush()
                await self._append_audit(
                    session,
                    issued,
                    action=AuditAction.ASSET_UPDATED,
                    target_id=model.id,
                    outcome=AuditOutcome.SUCCESS,
                    metadata={
                        "asset_kind": "provider_model",
                        "operation": "provider_model.disable",
                    },
                )
                return await self._model_view(session, model)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def _enable_model(
        self,
        issued: SystemAuditContext,
        model_id: uuid.UUID,
    ) -> ProviderModelView:
        # Freeze model parameters plus Provider material.
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued)
                model = await session.get(ModelProviderModelRow, model_id)
                if model is None:
                    raise _not_found()
                if model.status == "active":
                    return await self._model_view(session, model)
                provider = await session.get(ModelProviderRow, model.provider_id)
                if provider is None:  # pragma: no cover - RESTRICT FK keeps it alive
                    raise _not_found()
                frozen = _freeze_provider(provider)
                frozen_model = _freeze_model(model)
                provider_id = provider.id
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

        # Re-enabling must prove the model still answers.
        await self._probe_frozen_model(
            frozen_model,
            base_url=frozen.base_url,
            request_timeout_seconds=frozen.request_timeout_seconds,
            api_key=self._materialize(provider_id, frozen),
        )

        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued)
                model = await self._locked_model(session, model_id)
                provider = await session.get(ModelProviderRow, model.provider_id)
                if provider is None:  # pragma: no cover - RESTRICT FK keeps it alive
                    raise _not_found()
                if not _provider_matches(provider, frozen):
                    raise _conflict()
                if model.status == "active":
                    return await self._model_view(session, model)
                model.status = "active"
                model.updated_at = datetime.now(UTC)
                await session.flush()
                await self._append_audit(
                    session,
                    issued,
                    action=AuditAction.ASSET_UPDATED,
                    target_id=model.id,
                    outcome=AuditOutcome.SUCCESS,
                    metadata={
                        "asset_kind": "provider_model",
                        "operation": "provider_model.enable",
                    },
                )
                return await self._model_view(session, model)
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def delete_model(self, context: object, model_id: uuid.UUID) -> None:
        issued = self._require_issued(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued)
                model = await self._locked_model(session, model_id)
                if await self._model_in_use(session, model.id):
                    raise _invalid("该模型正被知识库引用，不能删除")
                await session.delete(model)
                await session.flush()
                await self._append_audit(
                    session,
                    issued,
                    action=AuditAction.ASSET_DELETED,
                    target_id=model_id,
                    outcome=AuditOutcome.SUCCESS,
                    metadata={
                        "asset_kind": "provider_model",
                        "operation": "provider_model.delete",
                    },
                )
        except KnowledgeError:
            raise
        except IntegrityError:
            raise _invalid("该模型正被知识库引用，不能删除") from None
        except SQLAlchemyError:
            raise _storage_unavailable() from None

    async def test_model(
        self,
        context: object,
        model_id: uuid.UUID,
    ) -> ProviderModelTestResult:
        issued = self._require_issued(context)
        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued)
                model = await session.get(ModelProviderModelRow, model_id)
                if model is None:
                    raise _not_found()
                provider = await session.get(ModelProviderRow, model.provider_id)
                if provider is None:  # pragma: no cover - RESTRICT FK keeps it alive
                    raise _not_found()
                frozen = _freeze_provider(provider)
                frozen_model = _freeze_model(model)
                provider_id = provider.id
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None

        try:
            await self._probe_frozen_model(
                frozen_model,
                base_url=frozen.base_url,
                request_timeout_seconds=frozen.request_timeout_seconds,
                api_key=self._materialize(provider_id, frozen),
            )
        except KnowledgeError as error:
            result = ProviderModelTestResult(ok=False, message=error.message)
        else:
            result = ProviderModelTestResult(ok=True, message="连接正常")

        try:
            async with self._session_factory() as session, session.begin():
                await self._lock_admin(session, issued)
                await self._append_audit(
                    session,
                    issued,
                    action=AuditAction.ASSET_UPDATED,
                    target_id=model_id,
                    outcome=(AuditOutcome.SUCCESS if result.ok else AuditOutcome.FAILED),
                    metadata={
                        "asset_kind": "provider_model",
                        "operation": "provider_model.test",
                    },
                )
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise _storage_unavailable() from None
        return result

    # -- shared helpers ----------------------------------------------------

    @staticmethod
    async def _provider_models(
        session: AsyncSession,
        provider_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> list[ModelProviderModelRow]:
        """Provider's models ordered by ID — the registry's lock order."""

        statement = select(ModelProviderModelRow).where(ModelProviderModelRow.provider_id == provider_id).order_by(ModelProviderModelRow.id)
        if for_update:
            statement = statement.with_for_update()
        return list((await session.scalars(statement)).all())

    async def _locked_model(
        self,
        session: AsyncSession,
        model_id: uuid.UUID,
    ) -> ModelProviderModelRow:
        """Lock Provider FOR UPDATE first, then the model row."""

        provider_id = await session.scalar(select(ModelProviderModelRow.provider_id).where(ModelProviderModelRow.id == model_id))
        if provider_id is None:
            raise _not_found()
        locked_provider = await session.scalar(select(ModelProviderRow.id).where(ModelProviderRow.id == provider_id).with_for_update())
        if locked_provider is None:  # pragma: no cover - RESTRICT FK keeps it alive
            raise _not_found()
        model = await session.scalar(select(ModelProviderModelRow).where(ModelProviderModelRow.id == model_id).with_for_update())
        if model is None:
            raise _not_found()
        return model

    async def _any_embedding_in_use(
        self,
        session: AsyncSession,
        models: list[ModelProviderModelRow],
    ) -> bool:
        for model in models:
            if model.model_type == "embedding" and await self._model_in_use(session, model.id):
                return True
        return False

    async def _model_view(
        self,
        session: AsyncSession,
        model: ModelProviderModelRow,
    ) -> ProviderModelView:
        return ProviderModelView(
            id=model.id,
            provider_id=model.provider_id,
            model_type=model.model_type,
            model_name=model.model_name,
            embedding_dimension=model.embedding_dimension,
            max_batch=model.max_batch,
            status=model.status,
            in_use=await self._model_in_use(session, model.id),
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def _probe_frozen_model(
        self,
        model: _FrozenModel,
        *,
        base_url: str,
        request_timeout_seconds: int,
        api_key: str,
    ) -> None:
        if model.model_type == "embedding":
            if model.embedding_dimension is None:  # pragma: no cover - CHECK enforces the pairing
                raise _invalid("Embedding 模型缺少维度")
            await self._client.verify_embedding(
                KnowledgeEmbeddingMaterial(
                    model_id=model.id,
                    base_url=base_url,
                    model_name=model.model_name,
                    dimension=model.embedding_dimension,
                    max_batch=model.max_batch,
                    request_timeout_seconds=request_timeout_seconds,
                    api_key=api_key,
                )
            )
            return
        await self._client.verify_rerank(
            KnowledgeRerankMaterial(
                model_id=model.id,
                base_url=base_url,
                model_name=model.model_name,
                max_batch=model.max_batch,
                request_timeout_seconds=request_timeout_seconds,
                api_key=api_key,
            )
        )

    def _protect(self, provider_id: uuid.UUID, base_url: str, api_key: str):
        try:
            return protect_provider_api_key(
                provider_id=provider_id,
                base_url=base_url,
                api_key=api_key,
                key=self._secret_key,
            )
        except (ModelProviderSecretInvalid, SecretKeyInvalid, SecretProtectionFailed):
            raise _storage_unavailable() from None

    def _materialize(self, provider_id: uuid.UUID, frozen: _FrozenProvider) -> str:
        try:
            return materialize_provider_api_key(
                provider_id=provider_id,
                base_url=frozen.base_url,
                nonce=frozen.api_key_nonce,
                ciphertext=frozen.api_key_ciphertext,
                key=self._secret_key,
            )
        except (
            ModelProviderSecretInvalid,
            SecretKeyInvalid,
            SecretMaterializationFailed,
            UnicodeError,
            ValueError,
        ):
            raise _storage_unavailable() from None


__all__ = [
    "MAX_BASE_URL_LENGTH",
    "MAX_EMBEDDING_BATCH",
    "MAX_EMBEDDING_DIMENSION",
    "MAX_MODEL_NAME_LENGTH",
    "MAX_PROVIDER_NAME_LENGTH",
    "MAX_RERANK_BATCH",
    "MAX_REQUEST_TIMEOUT_SECONDS",
    "MIN_REQUEST_TIMEOUT_SECONDS",
    "ModelProviderView",
    "ModelRegistryService",
    "ProviderModelTestResult",
    "ProviderModelView",
    "RetrievalModelOption",
    "list_active_retrieval_model_options",
]
