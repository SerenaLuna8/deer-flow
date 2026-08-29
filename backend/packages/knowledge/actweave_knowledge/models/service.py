"""Knowledge model configuration CRUD, connection testing, and options list.

Every mutation that changes what the provider is asked to do (a new API key or
any retrieval-semantic field) must pass a live ``/embeddings`` + ``/rerank``
probe before it is persisted. Provider probes run outside database
transactions: a mutation freezes its inputs, probes, then re-locks and rejects
stale state before persisting. A configuration referenced by a Knowledge Base
keeps its retrieval semantics frozen: no disabling, no base URL, model,
or dimension changes, and no deletion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..contracts import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_NAME_CONFLICT,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeError,
    KnowledgeModelConfigurationCreate,
    KnowledgeModelConfigurationUpdate,
    KnowledgeModelConfigurationView,
    KnowledgeModelConnectionResult,
    KnowledgeModelOption,
    KnowledgeSecretPort,
)
from ..persistence.models import KnowledgeBaseRow, KnowledgeModelConfigurationRow
from .client import KnowledgeModelClient, KnowledgeModelMaterial

MAX_PAGE_SIZE = 100

_MAX_DISPLAY_NAME_LENGTH = 120
_MAX_BASE_URL_LENGTH = 1024
_MAX_MODEL_NAME_LENGTH = 255
_EMBEDDING_DIMENSION_RANGE = (1, 16000)
_EMBEDDING_BATCH_RANGE = (1, 2048)
_RERANKER_BATCH_RANGE = (1, 256)
_TIMEOUT_RANGE = (1, 300)

# Fields a Knowledge-Base-referenced configuration must keep frozen, and whose
# change always requires a fresh two-endpoint connection test.
_SEMANTIC_FIELDS = ("base_url", "embedding_model", "embedding_dimension", "reranker_model")

# Every field a provider call materializes; the connection probe proves exactly
# one snapshot of these plus the API key.
_MATERIAL_FIELDS = (
    "base_url",
    "embedding_model",
    "embedding_dimension",
    "embedding_max_batch",
    "reranker_model",
    "reranker_max_batch",
    "request_timeout_seconds",
)


def _invalid(message: str) -> KnowledgeError:
    return KnowledgeError(KNOWLEDGE_INVALID_REQUEST, message)


def _validated_display_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _MAX_DISPLAY_NAME_LENGTH:
        raise _invalid(f"display_name 必须是 1-{_MAX_DISPLAY_NAME_LENGTH} 个字符的非空文本")
    return cleaned


def _validated_base_url(value: str) -> str:
    cleaned = value.strip().rstrip("/")
    parts = urlsplit(cleaned)
    if parts.scheme not in {"http", "https"} or not parts.netloc or len(cleaned) > _MAX_BASE_URL_LENGTH:
        raise _invalid("base_url 必须是可解析的 HTTP/HTTPS URL")
    # Endpoint paths are appended verbatim, so a query, fragment, or userinfo
    # part would silently produce a nonsense URL; reject it up front instead
    # of letting the connection probe fail with a confusing provider error.
    if parts.query or parts.fragment or parts.username is not None:
        raise _invalid("base_url 不能包含查询参数、fragment 或用户信息")
    return cleaned


def _validated_model_name(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _MAX_MODEL_NAME_LENGTH:
        raise _invalid(f"{field_name} 必须是 1-{_MAX_MODEL_NAME_LENGTH} 个字符的非空文本")
    return cleaned


def _validated_int(value: int, bounds: tuple[int, int], field_name: str) -> int:
    low, high = bounds
    if type(value) is not int or not low <= value <= high:
        raise _invalid(f"{field_name} 必须是 {low}-{high} 之间的整数")
    return value


def _validated_api_key(value: str) -> str:
    if not value or not value.strip():
        raise _invalid("api_key 不能为空")
    return value


def _validated_page(page: int, page_size: int) -> tuple[int, int]:
    if type(page) is not int or page < 1:
        raise _invalid("page 必须是不小于 1 的整数")
    if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise _invalid(f"page_size 必须是 1-{MAX_PAGE_SIZE} 之间的整数")
    return page, page_size


@dataclass(frozen=True, slots=True)
class _ValidatedCreate:
    display_name: str
    base_url: str
    embedding_model: str
    embedding_dimension: int
    embedding_max_batch: int
    reranker_model: str
    reranker_max_batch: int
    request_timeout_seconds: int


def _validated_create(create: KnowledgeModelConfigurationCreate) -> _ValidatedCreate:
    return _ValidatedCreate(
        display_name=_validated_display_name(create.display_name),
        base_url=_validated_base_url(create.base_url),
        embedding_model=_validated_model_name(create.embedding_model, "embedding_model"),
        embedding_dimension=_validated_int(create.embedding_dimension, _EMBEDDING_DIMENSION_RANGE, "embedding_dimension"),
        embedding_max_batch=_validated_int(create.embedding_max_batch, _EMBEDDING_BATCH_RANGE, "embedding_max_batch"),
        reranker_model=_validated_model_name(create.reranker_model, "reranker_model"),
        reranker_max_batch=_validated_int(create.reranker_max_batch, _RERANKER_BATCH_RANGE, "reranker_max_batch"),
        request_timeout_seconds=_validated_int(create.request_timeout_seconds, _TIMEOUT_RANGE, "request_timeout_seconds"),
    )


def _validated_update_changes(update: KnowledgeModelConfigurationUpdate) -> dict[str, object]:
    """Validate provided fields only; absent (``None``) fields keep stored values."""

    changes: dict[str, object] = {}
    if update.display_name is not None:
        changes["display_name"] = _validated_display_name(update.display_name)
    if update.status is not None:
        if update.status not in ("active", "disabled"):
            raise _invalid("status 只能是 active 或 disabled")
        changes["status"] = update.status
    if update.base_url is not None:
        changes["base_url"] = _validated_base_url(update.base_url)
    if update.embedding_model is not None:
        changes["embedding_model"] = _validated_model_name(update.embedding_model, "embedding_model")
    if update.embedding_dimension is not None:
        changes["embedding_dimension"] = _validated_int(update.embedding_dimension, _EMBEDDING_DIMENSION_RANGE, "embedding_dimension")
    if update.embedding_max_batch is not None:
        changes["embedding_max_batch"] = _validated_int(update.embedding_max_batch, _EMBEDDING_BATCH_RANGE, "embedding_max_batch")
    if update.reranker_model is not None:
        changes["reranker_model"] = _validated_model_name(update.reranker_model, "reranker_model")
    if update.reranker_max_batch is not None:
        changes["reranker_max_batch"] = _validated_int(update.reranker_max_batch, _RERANKER_BATCH_RANGE, "reranker_max_batch")
    if update.request_timeout_seconds is not None:
        changes["request_timeout_seconds"] = _validated_int(update.request_timeout_seconds, _TIMEOUT_RANGE, "request_timeout_seconds")
    return changes


def _view(row: KnowledgeModelConfigurationRow, *, in_use: bool) -> KnowledgeModelConfigurationView:
    return KnowledgeModelConfigurationView(
        id=row.id,
        display_name=row.display_name,
        status=row.status,  # type: ignore[arg-type]
        base_url=row.base_url,
        embedding_model=row.embedding_model,
        embedding_dimension=row.embedding_dimension,
        embedding_max_batch=row.embedding_max_batch,
        reranker_model=row.reranker_model,
        reranker_max_batch=row.reranker_max_batch,
        request_timeout_seconds=row.request_timeout_seconds,
        in_use=in_use,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _in_use_expression(configuration_id):  # noqa: ANN001, ANN202 - SQLAlchemy column expression
    return exists(select(KnowledgeBaseRow.id).where(KnowledgeBaseRow.model_configuration_id == configuration_id))


def _reject_frozen_field_changes(effective: dict[str, object], *, in_use: bool) -> None:
    """A configuration referenced by a Knowledge Base keeps retrieval semantics frozen."""

    if not in_use:
        return
    if effective.get("status") == "disabled":
        raise _invalid("配置正被 Knowledge Base 引用，不能停用")
    if any(name in effective for name in _SEMANTIC_FIELDS):
        raise _invalid("配置正被 Knowledge Base 引用，不能修改 base_url、模型或向量维度")


class KnowledgeModelConfigurationService:
    """CRUD and connection testing over ``knowledge_model_configurations``."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        secret_port: KnowledgeSecretPort,
        client: KnowledgeModelClient,
    ) -> None:
        self._session_factory = session_factory
        self._secret_port = secret_port
        self._client = client

    async def create_model_configuration(
        self,
        create: KnowledgeModelConfigurationCreate,
    ) -> KnowledgeModelConfigurationView:
        validated = _validated_create(create)
        api_key = _validated_api_key(create.api_key)
        configuration_id = uuid4()
        await self._client.verify_connection(
            _material(configuration_id, asdict(validated), api_key),
        )
        secret = self._protect_api_key(configuration_id, api_key)
        try:
            async with self._session_factory() as session, session.begin():
                row = KnowledgeModelConfigurationRow(
                    id=configuration_id,
                    display_name=validated.display_name,
                    status="active",
                    base_url=validated.base_url,
                    embedding_model=validated.embedding_model,
                    embedding_dimension=validated.embedding_dimension,
                    embedding_max_batch=validated.embedding_max_batch,
                    reranker_model=validated.reranker_model,
                    reranker_max_batch=validated.reranker_max_batch,
                    request_timeout_seconds=validated.request_timeout_seconds,
                    api_key_nonce=secret.nonce,
                    api_key_ciphertext=secret.ciphertext,
                )
                session.add(row)
                await session.flush()
                await session.refresh(row)
                return _view(row, in_use=False)
        except IntegrityError:
            # Every range/length constraint is pre-validated to the exact SQL
            # bounds above, so the case-insensitive name index is the only
            # realistic integrity failure left on this insert.
            raise KnowledgeError(KNOWLEDGE_NAME_CONFLICT, "已存在同名模型配置") from None
        except SQLAlchemyError:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "模型配置存储暂时不可用") from None

    async def list_model_configurations(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[KnowledgeModelConfigurationView], int]:
        page, page_size = _validated_page(page, page_size)
        try:
            async with self._session_factory() as session:
                total = await session.scalar(select(func.count()).select_from(KnowledgeModelConfigurationRow))
                rows = await session.execute(
                    select(
                        KnowledgeModelConfigurationRow,
                        _in_use_expression(KnowledgeModelConfigurationRow.id).label("in_use"),
                    )
                    .order_by(KnowledgeModelConfigurationRow.created_at, KnowledgeModelConfigurationRow.id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                views = [_view(row, in_use=in_use) for row, in_use in rows.all()]
                return views, int(total or 0)
        except SQLAlchemyError:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "模型配置存储暂时不可用") from None

    async def update_model_configuration(
        self,
        configuration_id: UUID,
        update: KnowledgeModelConfigurationUpdate,
    ) -> KnowledgeModelConfigurationView:
        """Update a configuration; provider probes run outside transactions.

        Freeze phase reads the row, applies an advisory frozen-field check,
        and probes the merged material with no database lock held. Settle
        phase re-locks the row, repeats the authoritative checks, and rejects
        the write when the probed material no longer matches the stored state
        (``KNOWLEDGE_CONFLICT``), so a stale probe can never certify a
        different configuration.
        """

        changes = _validated_update_changes(update)
        api_key = _validated_api_key(update.api_key) if update.api_key is not None else None

        # -- freeze: read current state, probe outside any transaction -------
        try:
            async with self._session_factory() as session:
                row = await session.get(KnowledgeModelConfigurationRow, configuration_id)
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "模型配置不存在")
                in_use_hint = bool(await session.scalar(select(_in_use_expression(configuration_id))))
                effective = {name: value for name, value in changes.items() if value != getattr(row, name)}
                needs_retest = api_key is not None or any(name in effective for name in _SEMANTIC_FIELDS)
                frozen_merged = {name: changes.get(name, getattr(row, name)) for name in _MATERIAL_FIELDS}
                frozen_secret = (row.api_key_nonce, row.api_key_ciphertext)
                stored_plaintext = self._materialize_api_key(row) if needs_retest and api_key is None else None
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "模型配置存储暂时不可用") from None
        # Advisory early reject: do not probe a provider for a doomed update.
        # The settle transaction repeats this check authoritatively.
        _reject_frozen_field_changes(effective, in_use=in_use_hint)
        if needs_retest:
            await self._client.verify_connection(
                _material(configuration_id, frozen_merged, api_key if api_key is not None else stored_plaintext or ""),
            )
        secret = self._protect_api_key(configuration_id, api_key) if api_key is not None else None

        # -- settle: re-lock, reject stale state, persist ---------------------
        try:
            async with self._session_factory() as session, session.begin():
                row = await session.get(
                    KnowledgeModelConfigurationRow,
                    configuration_id,
                    with_for_update=True,
                )
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "模型配置不存在")
                in_use = bool(await session.scalar(select(_in_use_expression(configuration_id))))
                effective = {name: value for name, value in changes.items() if value != getattr(row, name)}
                _reject_frozen_field_changes(effective, in_use=in_use)
                if needs_retest:
                    merged_now = {name: changes.get(name, getattr(row, name)) for name in _MATERIAL_FIELDS}
                    secret_unchanged = api_key is not None or (row.api_key_nonce, row.api_key_ciphertext) == frozen_secret
                    if merged_now != frozen_merged or not secret_unchanged:
                        raise KnowledgeError(KNOWLEDGE_CONFLICT, "模型配置已被并发修改，请重试")
                if secret is not None:
                    row.api_key_nonce = secret.nonce
                    row.api_key_ciphertext = secret.ciphertext
                if not effective and api_key is None:
                    return _view(row, in_use=in_use)
                for name, value in effective.items():
                    setattr(row, name, value)
                row.updated_at = func.now()  # type: ignore[assignment]
                await session.flush()
                await session.refresh(row)
                return _view(row, in_use=in_use)
        except IntegrityError:
            raise KnowledgeError(KNOWLEDGE_NAME_CONFLICT, "已存在同名模型配置") from None
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "模型配置存储暂时不可用") from None

    async def delete_model_configuration(self, configuration_id: UUID) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                row = await session.get(
                    KnowledgeModelConfigurationRow,
                    configuration_id,
                    with_for_update=True,
                )
                if row is None:
                    raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "模型配置不存在")
                if await session.scalar(select(_in_use_expression(configuration_id))):
                    raise _invalid("配置正被 Knowledge Base 引用，不能删除")
                await session.delete(row)
        except IntegrityError:
            # A Knowledge Base grabbed the configuration between the check and
            # the delete; the RESTRICT foreign key keeps the row alive.
            raise _invalid("配置正被 Knowledge Base 引用，不能删除") from None
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "模型配置存储暂时不可用") from None

    async def test_model_configuration(self, configuration_id: UUID) -> KnowledgeModelConnectionResult:
        try:
            async with self._session_factory() as session:
                row = await session.get(KnowledgeModelConfigurationRow, configuration_id)
        except SQLAlchemyError:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "模型配置存储暂时不可用") from None
        if row is None:
            raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "模型配置不存在")
        material = materialize_model_material(row, self._secret_port)
        try:
            await self._client.verify_connection(material)
        except KnowledgeError as error:
            return KnowledgeModelConnectionResult(ok=False, message=error.message)
        return KnowledgeModelConnectionResult(ok=True, message="Embedding 与 Reranker 连接测试通过")

    async def list_active_model_options(self) -> list[KnowledgeModelOption]:
        try:
            async with self._session_factory() as session:
                rows = await session.scalars(select(KnowledgeModelConfigurationRow).where(KnowledgeModelConfigurationRow.status == "active").order_by(KnowledgeModelConfigurationRow.display_name, KnowledgeModelConfigurationRow.id))
                return [
                    KnowledgeModelOption(
                        id=row.id,
                        display_name=row.display_name,
                        embedding_model=row.embedding_model,
                        embedding_dimension=row.embedding_dimension,
                        reranker_model=row.reranker_model,
                    )
                    for row in rows.all()
                ]
        except SQLAlchemyError:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "模型配置存储暂时不可用") from None

    def _protect_api_key(self, configuration_id: UUID, api_key: str):  # noqa: ANN202 - KnowledgeProtectedSecret
        try:
            return self._secret_port.protect_api_key(configuration_id, api_key)
        except Exception:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "API Key 加密失败，配置未保存") from None

    def _materialize_api_key(self, row: KnowledgeModelConfigurationRow) -> str:
        from ..contracts import KnowledgeProtectedSecret

        try:
            return self._secret_port.materialize_api_key(
                row.id,
                KnowledgeProtectedSecret(nonce=row.api_key_nonce, ciphertext=row.api_key_ciphertext),
            )
        except Exception:
            raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "无法读取已保存的 API Key") from None


def materialize_model_material(
    row: KnowledgeModelConfigurationRow,
    secret_port: KnowledgeSecretPort,
) -> KnowledgeModelMaterial:
    """Materialize a configuration row for provider calls (ingestion, search, test)."""

    from ..contracts import KnowledgeProtectedSecret

    try:
        plaintext = secret_port.materialize_api_key(
            row.id,
            KnowledgeProtectedSecret(nonce=row.api_key_nonce, ciphertext=row.api_key_ciphertext),
        )
    except Exception:
        raise KnowledgeError(KNOWLEDGE_STORAGE_UNAVAILABLE, "无法读取已保存的 API Key") from None
    fields = {name: getattr(row, name) for name in _MATERIAL_FIELDS}
    return _material(row.id, fields, plaintext)


def _material(configuration_id: UUID, fields: dict[str, object], api_key: str) -> KnowledgeModelMaterial:
    return KnowledgeModelMaterial(
        configuration_id=configuration_id,
        base_url=str(fields["base_url"]),
        embedding_model=str(fields["embedding_model"]),
        embedding_dimension=int(fields["embedding_dimension"]),  # type: ignore[call-overload]
        embedding_max_batch=int(fields["embedding_max_batch"]),  # type: ignore[call-overload]
        reranker_model=str(fields["reranker_model"]),
        reranker_max_batch=int(fields["reranker_max_batch"]),  # type: ignore[call-overload]
        request_timeout_seconds=int(fields["request_timeout_seconds"]),  # type: ignore[call-overload]
        api_key=api_key,
    )
