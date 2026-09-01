"""Explicit-install-only singleton bootstrap; never called by runtime startup."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from actweave_knowledge.contracts import KnowledgeMinioSettings
from pydantic import ValidationError
from sqlalchemy import text

from app.knowledge_settings.models import KnowledgeSettingsFields
from app.knowledge_settings.service import (
    SessionFactory,
    default_knowledge_settings_row,
    knowledge_minio_secret_recipient,
    probe_knowledge_storage,
)
from deerflow.persistence.knowledge_settings import KnowledgeSystemSettingsRow
from deerflow.secrets import (
    SecretEnvelope,
    SecretKey,
    SecretKeyInvalid,
    SecretProtectionFailed,
)

_BOOTSTRAP_LOCK_KEY = 0x0DEE_12F1_4B4E_4F57
_MINIO_BOOTSTRAP_ENV = (
    "ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT",
    "ACT_WEAVE_KNOWLEDGE_MINIO_BUCKET",
    "ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY",
    "ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY",
)


class KnowledgeSettingsBootstrapConfigurationInvalid(RuntimeError):
    """Secret-free failure raised before setup/reset performs any DDL."""

    def __init__(self) -> None:
        super().__init__(
            "知识库初始化配置必须同时提供 ACT_WEAVE_KNOWLEDGE_MINIO_ENDPOINT、ACT_WEAVE_KNOWLEDGE_MINIO_BUCKET、ACT_WEAVE_KNOWLEDGE_MINIO_ACCESS_KEY 和 ACT_WEAVE_KNOWLEDGE_MINIO_SECRET_KEY",
        )


class KnowledgeSettingsBootstrapStorageUnavailable(RuntimeError):
    """Secret-free failure raised when the configured bucket cannot be used."""

    def __init__(self) -> None:
        super().__init__(
            "知识库初始化无法访问配置的未版本化存储桶；请确认 bucket 已存在、未启用 versioning/Object Lock，且凭据权限完整",
        )


@dataclass(frozen=True, slots=True)
class KnowledgeSettingsBootstrapMaterial:
    """Pre-encrypted Knowledge storage input crossing the schema boundary."""

    minio_endpoint: str = field(repr=False)
    minio_bucket: str = field(repr=False)
    minio_access_key: str = field(repr=False)
    minio_secure: bool
    minio_secret_envelope: SecretEnvelope = field(repr=False)


BootstrapStorageProbe = Callable[[KnowledgeMinioSettings], Awaitable[None]]


async def prepare_knowledge_settings_bootstrap(
    *,
    storage_probe: BootstrapStorageProbe = probe_knowledge_storage,
) -> KnowledgeSettingsBootstrapMaterial | None:
    """Validate, probe, and encrypt optional install-only Knowledge storage."""

    values = tuple(os.environ.get(name) for name in _MINIO_BOOTSTRAP_ENV)
    if all(value is None for value in values):
        return None
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise KnowledgeSettingsBootstrapConfigurationInvalid
    endpoint, bucket, access_key, secret_key = values
    try:
        storage = KnowledgeMinioSettings(
            endpoint=endpoint,
            bucket=bucket.strip(),
            access_key=access_key.strip(),
            secret_key=secret_key,
            secure=False,
        )
        envelope = SecretEnvelope.protect(
            secret_key.encode("utf-8"),
            recipient=knowledge_minio_secret_recipient(storage.endpoint),
            key=SecretKey.from_environment(),
        )
    except (
        SecretKeyInvalid,
        SecretProtectionFailed,
        TypeError,
        UnicodeError,
        ValidationError,
        ValueError,
    ):
        raise KnowledgeSettingsBootstrapConfigurationInvalid from None
    try:
        await storage_probe(storage)
    except Exception:
        raise KnowledgeSettingsBootstrapStorageUnavailable from None
    return KnowledgeSettingsBootstrapMaterial(
        minio_endpoint=storage.endpoint,
        minio_bucket=storage.bucket,
        minio_access_key=storage.access_key,
        minio_secure=storage.secure,
        minio_secret_envelope=envelope,
    )


def _bootstrap_row(
    material: KnowledgeSettingsBootstrapMaterial | None,
) -> KnowledgeSystemSettingsRow:
    row = default_knowledge_settings_row()
    if material is None:
        return row
    row.enabled = True
    row.minio_endpoint = material.minio_endpoint
    row.minio_bucket = material.minio_bucket
    row.minio_access_key = material.minio_access_key
    row.minio_secure = material.minio_secure
    row.minio_secret_nonce = material.minio_secret_envelope.nonce
    row.minio_secret_ciphertext = material.minio_secret_envelope.ciphertext
    return row


async def bootstrap_knowledge_system_settings(
    session_factory: SessionFactory,
    *,
    material: KnowledgeSettingsBootstrapMaterial | None = None,
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BOOTSTRAP_LOCK_KEY})
        row = await session.get(KnowledgeSystemSettingsRow, 1, with_for_update=True)
        if row is None:
            session.add(_bootstrap_row(material))
            await session.flush()
        else:
            KnowledgeSettingsFields.model_validate(row)
            if row.revision < 1 or (row.minio_secret_nonce is None) != (row.minio_secret_ciphertext is None):
                raise RuntimeError("KNOWLEDGE_SETTINGS_BOOTSTRAP_INVALID")
