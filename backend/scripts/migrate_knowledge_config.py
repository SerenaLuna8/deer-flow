"""Explicit operator migration of legacy Knowledge YAML into installed Schema V1.

No schema mutation or runtime configuration loader is used. Stop Gateway and
Worker, run this while the YAML block still exists, remove that block, restart.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import yaml
from actweave_knowledge import KnowledgeSettings
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.final_schema import FinalSchemaProbe
from app.knowledge_settings.service import SessionFactory, default_knowledge_settings_row, knowledge_minio_secret_recipient
from deerflow.config.app_config import AppConfig
from deerflow.persistence.knowledge_settings import KnowledgeSystemSettingsRow
from deerflow.secrets import SecretEnvelope, SecretKey

_MIGRATION_LOCK_KEY = 0x0DEE_12F1_4B4E_4F57


def read_legacy_knowledge_settings(config_path: Path) -> KnowledgeSettings:
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("knowledge"), dict) or not raw["knowledge"]:
            raise ValueError
        # Deliberately bypass AppConfig.from_file/get_app_config: those reject
        # the removed block and would make the migration itself impossible.
        return KnowledgeSettings.model_validate(AppConfig.resolve_env_variables(raw["knowledge"]))
    except Exception:
        raise ValueError("KNOWLEDGE_CONFIG_MIGRATION_INVALID: legacy knowledge configuration is missing or invalid") from None


async def migrate_knowledge_config(session_factory: SessionFactory, *, config_path: Path, secret_key: SecretKey) -> KnowledgeSystemSettingsRow:
    settings = read_legacy_knowledge_settings(config_path)
    fields = settings.model_dump(exclude={"minio"})
    storage = settings.minio
    fields.update(
        minio_endpoint=storage.endpoint if storage else None,
        minio_bucket=storage.bucket if storage else None,
        minio_access_key=storage.access_key if storage else None,
        minio_secure=storage.secure if storage else False,
    )
    envelope = None
    if storage is not None:
        envelope = SecretEnvelope.protect(storage.secret_key.get_secret_value().encode("utf-8"), recipient=knowledge_minio_secret_recipient(storage.endpoint), key=secret_key)
    async with session_factory() as session, session.begin():
        await FinalSchemaProbe().require_ready(session)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
        row = await session.get(KnowledgeSystemSettingsRow, 1, with_for_update=True)
        if row is None:
            row = default_knowledge_settings_row()
            session.add(row)
        else:
            row.revision += 1
        for name, value in fields.items():
            setattr(row, name, value)
        row.minio_secret_nonce = envelope.nonce if envelope else None
        row.minio_secret_ciphertext = envelope.ciphertext if envelope else None
        row.updated_at = datetime.now(UTC)
        await session.flush()
        return row


def migration_report() -> str:
    # Report field names, never endpoint/bucket/access-key/model references or
    # secrets. The result is safe to paste into an operator ticket.
    fields = sorted(KnowledgeSettings.model_fields)
    return "Migrated Knowledge settings: " + ", ".join(fields) + "; minio.secret_key=[REDACTED]. Remove the YAML knowledge block before restart."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

    async def run() -> None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise ValueError
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        engine = create_async_engine(url)
        try:
            await migrate_knowledge_config(async_sessionmaker(engine, expire_on_commit=False), config_path=args.config or AppConfig.resolve_config_path(), secret_key=SecretKey.from_environment())
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    except Exception:
        print("KNOWLEDGE_CONFIG_MIGRATION_FAILED: verify installed Schema V1, database access, master key and legacy YAML; no schema was changed.")
        return 1
    print(migration_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
