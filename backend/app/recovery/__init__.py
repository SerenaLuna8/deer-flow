"""Operator-only encrypted PostgreSQL recovery archive primitives."""

from .archive import (
    CHUNK_SIZE,
    BackupArchiveReader,
    BackupArchiveWriter,
    BackupAuthenticationFailed,
    BackupCommandFailed,
    BackupConfig,
    BackupKeyInvalid,
    BackupKeyMissing,
    BackupManifest,
    create_backup,
    load_backup_key,
    pg_dump_argv,
)

__all__ = [
    "CHUNK_SIZE",
    "BackupArchiveReader",
    "BackupArchiveWriter",
    "BackupAuthenticationFailed",
    "BackupCommandFailed",
    "BackupConfig",
    "BackupKeyInvalid",
    "BackupKeyMissing",
    "BackupManifest",
    "create_backup",
    "load_backup_key",
    "pg_dump_argv",
]
