"""Explicit-install-only singleton bootstrap; never called by runtime startup."""

from __future__ import annotations

from sqlalchemy import text

from app.knowledge_settings.models import KnowledgeSettingsFields
from app.knowledge_settings.service import SessionFactory, default_knowledge_settings_row
from deerflow.persistence.knowledge_settings import KnowledgeSystemSettingsRow

_BOOTSTRAP_LOCK_KEY = 0x0DEE_12F1_4B4E_4F57


async def bootstrap_knowledge_system_settings(session_factory: SessionFactory) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BOOTSTRAP_LOCK_KEY})
        row = await session.get(KnowledgeSystemSettingsRow, 1, with_for_update=True)
        if row is None:
            session.add(default_knowledge_settings_row())
            await session.flush()
        else:
            KnowledgeSettingsFields.model_validate(row)
            if row.revision < 1 or (row.minio_secret_nonce is None) != (row.minio_secret_ciphertext is None):
                raise RuntimeError("KNOWLEDGE_SETTINGS_BOOTSTRAP_INVALID")
