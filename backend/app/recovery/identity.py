"""Authoritative PostgreSQL installation identity shared by recovery paths."""

from __future__ import annotations

import hashlib

from sqlalchemy import text

_SOURCE_ID_DOMAIN = b"deerflow-postgres-source-v1\x00"


async def source_installation_id(connection) -> str:
    row = (
        await connection.execute(
            text(
                """SELECT (SELECT system_identifier::text FROM pg_control_system()) AS system_identifier,
                          (SELECT oid::bigint FROM pg_database WHERE datname=current_database()) AS database_oid"""
            )
        )
    ).one()
    payload = _SOURCE_ID_DOMAIN + str(row.system_identifier).encode("ascii") + b"\x00" + str(int(row.database_oid)).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["source_installation_id"]
