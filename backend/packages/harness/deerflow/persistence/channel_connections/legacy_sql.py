"""Owner-only channel adapter for the bounded pre-M4 compatibility window.

This repository intentionally uses the frozen 0007 column contract instead of
the final project-scoped ORM models.  Only the legacy ``/api/channels`` router
may use it, and that router is guarded by ``require_legacy_private_open``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.utils.time import coerce_iso


class LegacyChannelConnectionRepository:
    """Explicit raw-SQL adapter for channel tables at revisions 0007/0008."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def close(self) -> None:
        from deerflow.persistence.engine import close_engine

        await close_engine()

    @staticmethod
    def _identity(value: str | None) -> str:
        return value or ""

    @staticmethod
    def _lock_key(*parts: str) -> int:
        digest = hashlib.sha256("\x00".join(parts).encode()).digest()
        return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF

    @staticmethod
    def hash_state(state: str) -> str:
        return hashlib.sha256(state.encode("utf-8")).hexdigest()

    @staticmethod
    def _connection_dict(row: Any) -> dict[str, Any]:
        data = dict(row._mapping if hasattr(row, "_mapping") else row)
        data["external_account_id"] = data.get("external_account_id") or None
        data["workspace_id"] = data.get("workspace_id") or None
        data["scopes"] = data.pop("scopes_json", None) or []
        data["capabilities"] = data.pop("capabilities_json", None) or {}
        data["metadata"] = data.pop("metadata_json", None) or {}
        for key in ("created_at", "updated_at", "last_seen_at", "last_error_at"):
            value = data.get(key)
            if isinstance(value, datetime):
                data[key] = coerce_iso(value)
        return data

    async def _get_connection(self, session: AsyncSession, connection_id: str) -> dict[str, Any]:
        row = (
            (
                await session.execute(
                    text(
                        """SELECT id,owner_user_id,provider,status,external_account_id,
                              external_account_name,workspace_id,workspace_name,bot_user_id,
                              scopes_json,capabilities_json,metadata_json,created_at,updated_at,
                              last_seen_at,last_error_at
                       FROM channel_connections WHERE id=:id"""
                    ),
                    {"id": connection_id},
                )
            )
            .mappings()
            .one()
        )
        return self._connection_dict(row)

    async def upsert_connection(
        self,
        *,
        owner_user_id: str,
        provider: str,
        external_account_id: str | None = None,
        external_account_name: str | None = None,
        workspace_id: str | None = None,
        workspace_name: str | None = None,
        bot_user_id: str | None = None,
        scopes: list[str] | None = None,
        capabilities: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = "connected",
    ) -> dict[str, Any]:
        external_id = self._identity(external_account_id)
        workspace = self._identity(workspace_id)
        now = datetime.now(UTC)
        values = {
            "owner": owner_user_id,
            "provider": provider,
            "external": external_id,
            "external_name": external_account_name,
            "workspace": workspace,
            "workspace_name": workspace_name,
            "bot_user": bot_user_id,
            "scopes": json.dumps(scopes or [], ensure_ascii=False),
            "capabilities": json.dumps(capabilities or {}, ensure_ascii=False),
            "metadata": json.dumps(metadata or {}, ensure_ascii=False),
            "status": status,
            "now": now,
        }
        async with self.session_factory() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": self._lock_key(provider, external_id, workspace)},
            )
            if status == "connected":
                transferred = (
                    (
                        await session.execute(
                            text(
                                """UPDATE channel_connections SET status='revoked',updated_at=:now
                               WHERE provider=:provider AND external_account_id=:external
                                 AND workspace_id=:workspace AND owner_user_id<>:owner
                                 AND status='connected' RETURNING id"""
                            ),
                            values,
                        )
                    )
                    .scalars()
                    .all()
                )
                if transferred:
                    await session.execute(
                        text("DELETE FROM channel_credentials WHERE connection_id = ANY(:ids)"),
                        {"ids": list(transferred)},
                    )
            connection_id = (
                await session.execute(
                    text(
                        """SELECT id FROM channel_connections
                           WHERE owner_user_id=:owner AND provider=:provider
                             AND external_account_id=:external AND workspace_id=:workspace"""
                    ),
                    values,
                )
            ).scalar_one_or_none()
            if connection_id is None:
                connection_id = uuid.uuid4().hex
                await session.execute(
                    text(
                        """INSERT INTO channel_connections
                           (id,owner_user_id,provider,status,external_account_id,
                            external_account_name,workspace_id,workspace_name,bot_user_id,
                            scopes_json,capabilities_json,metadata_json,created_at,updated_at,
                            last_seen_at,last_error_at)
                           VALUES (:id,:owner,:provider,:status,:external,:external_name,
                                   :workspace,:workspace_name,:bot_user,CAST(:scopes AS jsonb),
                                   CAST(:capabilities AS jsonb),CAST(:metadata AS jsonb),
                                   :now,:now,NULL,NULL)"""
                    ),
                    {**values, "id": connection_id},
                )
            else:
                await session.execute(
                    text(
                        """UPDATE channel_connections
                           SET status=:status,external_account_name=:external_name,
                               workspace_name=:workspace_name,bot_user_id=:bot_user,
                               scopes_json=CAST(:scopes AS jsonb),
                               capabilities_json=CAST(:capabilities AS jsonb),
                               metadata_json=CAST(:metadata AS jsonb),updated_at=:now
                           WHERE id=:id"""
                    ),
                    {**values, "id": connection_id},
                )
            await session.commit()
            return await self._get_connection(session, connection_id)

    async def list_connections(self, owner_user_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """SELECT id,owner_user_id,provider,status,external_account_id,
                                  external_account_name,workspace_id,workspace_name,bot_user_id,
                                  scopes_json,capabilities_json,metadata_json,created_at,updated_at,
                                  last_seen_at,last_error_at
                           FROM channel_connections WHERE owner_user_id=:owner
                           ORDER BY updated_at DESC,id DESC"""
                        ),
                        {"owner": owner_user_id},
                    )
                )
                .mappings()
                .all()
            )
            return [self._connection_dict(row) for row in rows]

    async def disconnect_connection(self, *, connection_id: str, owner_user_id: str) -> bool:
        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    """UPDATE channel_connections SET status='revoked',updated_at=:now
                       WHERE id=:id AND owner_user_id=:owner RETURNING id"""
                ),
                {"id": connection_id, "owner": owner_user_id, "now": datetime.now(UTC)},
            )
            found = result.scalar_one_or_none()
            if found is None:
                await session.rollback()
                return False
            await session.execute(
                text("DELETE FROM channel_credentials WHERE connection_id=:id"),
                {"id": connection_id},
            )
            await session.commit()
            return True

    async def disconnect_provider_connections(self, *, provider: str) -> int:
        async with self.session_factory() as session:
            ids = (
                (
                    await session.execute(
                        text(
                            """UPDATE channel_connections SET status='revoked',updated_at=:now
                           WHERE provider=:provider AND status<>'revoked' RETURNING id"""
                        ),
                        {"provider": provider, "now": datetime.now(UTC)},
                    )
                )
                .scalars()
                .all()
            )
            if ids:
                await session.execute(
                    text("DELETE FROM channel_credentials WHERE connection_id = ANY(:ids)"),
                    {"ids": list(ids)},
                )
            await session.commit()
            return len(ids)

    async def create_oauth_state_within_cap(
        self,
        *,
        owner_user_id: str,
        provider: str,
        state: str,
        expires_at: datetime,
        max_pending: int,
        now: datetime | None = None,
        code_verifier: str | None = None,
        nonce_hash: str | None = None,
        redirect_after: str | None = None,
        requested_scopes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        current = now or datetime.now(UTC)
        async with self.session_factory() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": self._lock_key(owner_user_id, provider)},
            )
            await session.execute(
                text(
                    """DELETE FROM channel_oauth_states
                       WHERE owner_user_id=:owner AND provider=:provider AND expires_at<:now"""
                ),
                {"owner": owner_user_id, "provider": provider, "now": current},
            )
            pending = await session.scalar(
                text(
                    """SELECT count(*) FROM channel_oauth_states
                       WHERE owner_user_id=:owner AND provider=:provider
                         AND consumed_at IS NULL AND expires_at>=:now"""
                ),
                {"owner": owner_user_id, "provider": provider, "now": current},
            )
            if int(pending or 0) >= max_pending:
                await session.rollback()
                return False
            await session.execute(
                text(
                    """INSERT INTO channel_oauth_states
                       (state_hash,owner_user_id,provider,code_verifier_encrypted,
                        nonce_hash,redirect_after,requested_scopes_json,metadata_json,
                        expires_at,consumed_at,created_at)
                       VALUES (:state_hash,:owner,:provider,:verifier,:nonce,:redirect,
                               CAST(:scopes AS jsonb),CAST(:metadata AS jsonb),
                               :expires,NULL,:now)"""
                ),
                {
                    "state_hash": self.hash_state(state),
                    "owner": owner_user_id,
                    "provider": provider,
                    "verifier": code_verifier,
                    "nonce": nonce_hash,
                    "redirect": redirect_after,
                    "scopes": json.dumps(requested_scopes or [], ensure_ascii=False),
                    "metadata": json.dumps(metadata or {}, ensure_ascii=False),
                    "expires": expires_at,
                    "now": current,
                },
            )
            await session.commit()
            return True

    async def count_oauth_states(
        self,
        *,
        owner_user_id: str,
        provider: str,
        active_only: bool = False,
        now: datetime | None = None,
    ) -> int:
        where = "owner_user_id=:owner AND provider=:provider"
        values: dict[str, Any] = {"owner": owner_user_id, "provider": provider}
        if active_only:
            where += " AND consumed_at IS NULL AND expires_at>=:now"
            values["now"] = now or datetime.now(UTC)
        async with self.session_factory() as session:
            count = await session.scalar(
                text(f"SELECT count(*) FROM channel_oauth_states WHERE {where}"),  # noqa: S608 - fixed internal clause
                values,
            )
            return int(count or 0)
