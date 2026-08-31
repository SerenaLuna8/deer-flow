"""Revisioned Knowledge settings with transaction-free storage validation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from actweave_knowledge import KNOWLEDGE_MODEL_UNAVAILABLE, KnowledgeError, KnowledgeSettings
from actweave_knowledge.contracts import KnowledgeMinioSettings
from actweave_knowledge.storage.minio_store import MinioObjectStore
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from urllib3 import PoolManager, Timeout

from app.audit.models import AuditAction, AuditActor, AuditError, AuditOutcome, AuditTarget, AuditTargetKind, SystemAuditContext, is_issued_system_audit_context
from app.audit.service import AuditService
from app.knowledge_settings.models import AdminKnowledgeSettingsResponse, AdminKnowledgeSettingsUpdateRequest, KnowledgeSettingsFields, SummaryModelInfo
from deerflow.persistence.knowledge_settings import KnowledgeSystemSettingsRow
from deerflow.persistence.system_settings import SystemModelConfigRow
from deerflow.persistence.user import UserRow
from deerflow.secrets import SecretEnvelope, SecretKey, SecretMaterializationFailed, SecretProtectionFailed

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], AsyncSession]
StorageProbe = Callable[[KnowledgeMinioSettings], Awaitable[None]]
_AUDIT_TARGET = UUID("79c1c60a-1aed-5a13-bd49-49d0707f5a2a")


class KnowledgeSettingsError(Exception):
    def __init__(self, code: str = "KNOWLEDGE_SETTINGS_INVALID", status_code: int = 422) -> None:
        self.code = code
        self.status_code = status_code
        self.public_message = {
            404: "设置不存在",
            409: "配置已更新，请刷新后重试",
            422: "知识库配置无效，请检查存储连接、密钥和模型状态",
            503: "知识库配置暂不可用",
        }[status_code]
        super().__init__(self.public_message)


def knowledge_minio_secret_recipient(endpoint: str) -> str:
    return f"knowledge-system-settings:minio-secret-key:{endpoint}"


def default_knowledge_settings_row() -> KnowledgeSystemSettingsRow:
    return KnowledgeSystemSettingsRow(
        id=1,
        revision=1,
        **KnowledgeSettings().model_dump(exclude={"minio"}),
        minio_endpoint=None,
        minio_bucket=None,
        minio_access_key=None,
        minio_secure=False,
        minio_secret_nonce=None,
        minio_secret_ciphertext=None,
        summary_model_name=None,
        updated_at=datetime.now(UTC),
    )


async def read_knowledge_system_settings(session: AsyncSession) -> KnowledgeSystemSettingsRow:
    row = await session.get(KnowledgeSystemSettingsRow, 1)
    if row is None:
        # Runtime never seeds or repairs state; setup owns the singleton.
        logger.warning("knowledge settings row absent; feature disabled")
        return default_knowledge_settings_row()
    return row


def settings_from_row(row: KnowledgeSystemSettingsRow, *, secret_key: SecretKey) -> KnowledgeSettings:
    fields = KnowledgeSettingsFields.model_validate(row).model_dump()
    stored_secret = None
    if row.minio_secret_nonce is not None or row.minio_secret_ciphertext is not None:
        if not row.minio_endpoint:
            raise SecretMaterializationFailed
        stored_secret = (
            SecretEnvelope(row.minio_secret_nonce, row.minio_secret_ciphertext)
            .materialize(
                recipient=knowledge_minio_secret_recipient(row.minio_endpoint),
                key=secret_key,
            )
            .decode("utf-8")
        )
    storage_values = {"endpoint": fields.pop("minio_endpoint"), "bucket": fields.pop("minio_bucket"), "access_key": fields.pop("minio_access_key"), "secure": fields.pop("minio_secure")}
    fields.pop("summary_model_name")
    storage = None
    if all(storage_values[name] is not None for name in ("endpoint", "bucket", "access_key")) and stored_secret is not None:
        storage = KnowledgeMinioSettings(**storage_values, secret_key=stored_secret)
    return KnowledgeSettings(**fields, minio=storage)


async def load_knowledge_settings_from_db(session_factory: SessionFactory, *, secret_key: SecretKey) -> KnowledgeSettings:
    try:
        async with session_factory() as session:
            return settings_from_row(await read_knowledge_system_settings(session), secret_key=secret_key)
    except (ValidationError, SecretMaterializationFailed, UnicodeError):
        raise KnowledgeSettingsError("KNOWLEDGE_SETTINGS_UNAVAILABLE", 503) from None


async def summary_model_info(session: AsyncSession, reference: str | None) -> SummaryModelInfo | None:
    if reference is None:
        return None
    try:
        model_id = UUID(reference)
    except (ValueError, TypeError):
        raise KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE, "摘要模型不可用，请联系管理员") from None
    model = (
        await session.execute(select(SystemModelConfigRow.id, SystemModelConfigRow.display_name).where(SystemModelConfigRow.id == model_id, SystemModelConfigRow.status == "active").with_for_update(read=True, of=SystemModelConfigRow))
    ).one_or_none()
    if model is None:
        raise KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE, "摘要模型不可用，请联系管理员")
    return SummaryModelInfo(model_name=str(model.id), display_name=model.display_name)


async def read_active_summary_model(session: AsyncSession) -> SummaryModelInfo | None:
    row = await session.get(KnowledgeSystemSettingsRow, 1)
    return await summary_model_info(session, row.summary_model_name if row is not None else None)


async def knowledge_settings_response(session: AsyncSession, row: KnowledgeSystemSettingsRow, *, request_id: str) -> AdminKnowledgeSettingsResponse:
    try:
        summary = await summary_model_info(session, row.summary_model_name)
    except KnowledgeError:
        # Keep the reference visible so administrators can replace a model
        # suspended since the configuration was saved.
        summary = None
    return AdminKnowledgeSettingsResponse(
        **KnowledgeSettingsFields.model_validate(row).model_dump(),
        revision=row.revision,
        updated_at=row.updated_at,
        secret_key_configured=row.minio_secret_ciphertext is not None,
        summary_model=summary,
        request_id=request_id,
    )


async def require_settings_admin(session: AsyncSession, actor: SystemAuditContext) -> None:
    if not is_issued_system_audit_context(actor):
        raise KnowledgeSettingsError("NOT_FOUND", 404)
    role = await session.scalar(select(UserRow.system_role).where(UserRow.id == str(actor.user_id), UserRow.system_role == "system_admin").with_for_update(read=True, of=UserRow))
    if role != "system_admin":
        raise KnowledgeSettingsError("NOT_FOUND", 404)


async def probe_knowledge_storage(settings: KnowledgeMinioSettings) -> None:
    # MinIO defaults to five-minute sockets plus retries. Its synchronous calls
    # are joined on cancellation, so wait_for alone does not bound this probe.
    # Use short sockets with no retries here; ordinary object mutations retain
    # their existing client and cancellation/cleanup semantics.
    client = PoolManager(timeout=Timeout(total=2, connect=2, read=2), retries=False)
    try:
        await asyncio.wait_for(MinioObjectStore(settings, http_client=client).require_unversioned_bucket(), timeout=10)
    finally:
        client.clear()


def _candidate(row: KnowledgeSystemSettingsRow, request: AdminKnowledgeSettingsUpdateRequest, key: SecretKey) -> KnowledgeSystemSettingsRow:
    if request.minio_secret_key is None and row.minio_secret_ciphertext is not None and request.minio_endpoint != row.minio_endpoint:
        raise KnowledgeSettingsError()
    candidate = KnowledgeSystemSettingsRow(**request.model_dump(exclude={"expected_revision"}))
    candidate.minio_secret_nonce = row.minio_secret_nonce
    candidate.minio_secret_ciphertext = row.minio_secret_ciphertext
    if request.minio_secret_key is not None:
        if not candidate.minio_endpoint:
            raise KnowledgeSettingsError()
        envelope = SecretEnvelope.protect(request.minio_secret_key.get_secret_value().encode("utf-8"), recipient=knowledge_minio_secret_recipient(candidate.minio_endpoint), key=key)
        candidate.minio_secret_nonce, candidate.minio_secret_ciphertext = envelope.nonce, envelope.ciphertext
    return candidate


async def update_knowledge_system_settings(
    session_factory: SessionFactory,
    *,
    actor: SystemAuditContext,
    request: AdminKnowledgeSettingsUpdateRequest,
    secret_key: SecretKey,
    audit_service: AuditService,
    storage_probe: StorageProbe = probe_knowledge_storage,
) -> KnowledgeSystemSettingsRow:
    try:
        # Snapshot and probe do not hold locks while external storage responds.
        # The write fence below rechecks both authority and expected_revision.
        async with session_factory() as session, session.begin():
            await require_settings_admin(session, actor)
            current = await read_knowledge_system_settings(session)
            if current.revision != request.expected_revision:
                raise KnowledgeSettingsError("KNOWLEDGE_SETTINGS_CONFLICT", 409)
            candidate = _candidate(current, request, secret_key)
            settings = settings_from_row(candidate, secret_key=secret_key)
            await summary_model_info(session, request.summary_model_name)
        if settings.enabled:
            try:
                await asyncio.wait_for(storage_probe(settings.minio), timeout=10)
            except Exception:
                raise KnowledgeSettingsError() from None
        async with session_factory() as session, session.begin():
            await require_settings_admin(session, actor)
            current = await session.get(KnowledgeSystemSettingsRow, 1, with_for_update=True)
            if current is None:
                # Bootstrap is an operator action, never a runtime repair.
                raise KnowledgeSettingsError("KNOWLEDGE_SETTINGS_UNAVAILABLE", 503)
            if current.revision != request.expected_revision:
                raise KnowledgeSettingsError("KNOWLEDGE_SETTINGS_CONFLICT", 409)
            await summary_model_info(session, request.summary_model_name)
            for name in KnowledgeSettingsFields.model_fields:
                setattr(current, name, getattr(candidate, name))
            current.minio_secret_nonce = candidate.minio_secret_nonce
            current.minio_secret_ciphertext = candidate.minio_secret_ciphertext
            current.revision += 1
            current.updated_at = datetime.now(UTC)
            await audit_service.append(
                session,
                AuditActor.system_admin(actor),
                AuditAction.KNOWLEDGE_SETTINGS_UPDATED,
                AuditTarget(kind=AuditTargetKind.SYSTEM_SETTING, authority_id=_AUDIT_TARGET, project_id=None),
                AuditOutcome.SUCCESS,
                {},
                request_id=actor.request_id,
            )
            await session.flush()
            return current
    except KnowledgeSettingsError:
        raise
    except (ValidationError, KnowledgeError, SecretProtectionFailed, SecretMaterializationFailed, UnicodeError):
        raise KnowledgeSettingsError() from None
    except (AuditError, DBAPIError):
        raise KnowledgeSettingsError("KNOWLEDGE_SETTINGS_UNAVAILABLE", 503) from None
