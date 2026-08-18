"""SQL repository for user-owned IM channel connections."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.channel_connections.identity_lock import (
    lock_channel_identities,
)
from deerflow.persistence.channel_connections.model import (
    ChannelConnectionRow,
    ChannelConversationRow,
    ChannelCredentialRow,
    ChannelOAuthStateRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.utils.time import coerce_iso

logger = logging.getLogger(__name__)

# Bounded retries for upsert_connection when a concurrent writer commits a
# conflicting row first (same owner identity, or the same active external
# identity guarded by the partial unique index). Each retry re-reads the
# now-visible state, so a small bound converges under realistic contention.
_UPSERT_MAX_ATTEMPTS = 3

ExternalIdentityCandidateFactory = Callable[
    [str, uuid.UUID, str, str],
    tuple[tuple[str, str], ...],
]
TransactionGuard = Callable[[AsyncSession], Awaitable[None]]


class ChannelCredentialCipher:
    """Encrypts provider credentials before they are persisted."""

    def __init__(self, fernet: Fernet) -> None:
        self._fernet = fernet

    @classmethod
    def from_key(cls, key: str) -> ChannelCredentialCipher:
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return cls(Fernet(base64.urlsafe_b64encode(digest)))

    def encrypt_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        return "fernet:v1:" + self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        token = value.removeprefix("fernet:v1:")
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")


class ChannelConnectionRepository:
    """Persistence facade for channel connections, credentials, and conversations."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cipher: ChannelCredentialCipher | None = None,
        external_identity_candidates: ExternalIdentityCandidateFactory | None = None,
    ) -> None:
        self.session_factory = session_factory
        self._cipher = cipher
        self._external_identity_candidates = external_identity_candidates

    async def close(self) -> None:
        from deerflow.persistence.engine import close_engine

        await close_engine()

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _normalize_optional_identity(value: str | None) -> str:
        return value or ""

    @staticmethod
    def _channel_instance_uuid(
        value: uuid.UUID | str | None,
    ) -> uuid.UUID | None:
        if value is None:
            return None
        try:
            return value if isinstance(value, uuid.UUID) else uuid.UUID(value)
        except (TypeError, ValueError):
            raise RuntimeError("invalid channel instance id") from None

    @staticmethod
    def _connection_instance_condition(value: uuid.UUID | None):
        if value is None:
            return ChannelConnectionRow.channel_instance_id.is_(None)
        return ChannelConnectionRow.channel_instance_id == value

    def _lookup_identity_candidates(
        self,
        provider: str,
        channel_instance_id: uuid.UUID | None,
        external_account_id: str,
        workspace_id: str | None,
    ) -> tuple[tuple[str, str], ...]:
        """Return raw personal and optional pseudonymous group coordinates."""

        raw = (
            self._normalize_optional_identity(external_account_id),
            self._normalize_optional_identity(workspace_id),
        )
        if channel_instance_id is None or self._external_identity_candidates is None:
            return (raw,)
        derived = self._external_identity_candidates(
            provider,
            channel_instance_id,
            raw[0],
            raw[1],
        )
        candidates = [raw]
        for candidate in derived:
            if type(candidate) is not tuple or len(candidate) != 2 or any(type(value) is not str or not value for value in candidate):
                raise RuntimeError("invalid channel identity candidate")
            if candidate not in candidates:
                candidates.append(candidate)
        return tuple(candidates)

    @staticmethod
    def _oauth_instance_condition(value: uuid.UUID | None):
        if value is None:
            return ChannelOAuthStateRow.channel_instance_id.is_(None)
        return ChannelOAuthStateRow.channel_instance_id == value

    @staticmethod
    def _coordinates(scope: PrivateResourceScope) -> tuple[uuid.UUID, str]:
        if type(scope) is not PrivateResourceScope:
            raise RuntimeError("channel connection operation requires private scope")
        try:
            return uuid.UUID(scope.project_id), str(uuid.UUID(scope.owner_user_id))
        except (TypeError, ValueError):
            raise RuntimeError("invalid channel connection private scope") from None

    @staticmethod
    def _coerce_datetime(value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)

    def _encrypt_optional_secret(self, value: str | None) -> str | None:
        if value is None:
            return None
        if self._cipher is None:
            raise RuntimeError("channel connection encryption key is required")
        return self._cipher.encrypt_text(value)

    @staticmethod
    def _connection_to_dict(row: ChannelConnectionRow) -> dict[str, Any]:
        data = row.to_dict()
        data["project_id"] = str(data["project_id"])
        if data.get("channel_instance_id") is not None:
            data["channel_instance_id"] = str(data["channel_instance_id"])
        data["account_id"] = data["owner_user_id"]
        data["external_account_id"] = data["external_account_id"] or None
        data["workspace_id"] = data["workspace_id"] or None
        data["scopes"] = data.pop("scopes_json") or []
        data["capabilities"] = data.pop("capabilities_json") or {}
        data["metadata"] = data.pop("metadata_json") or {}
        for key in (
            "created_at",
            "updated_at",
            "last_seen_at",
            "last_error_at",
            "frozen_at",
        ):
            value = data.get(key)
            if isinstance(value, datetime):
                data[key] = coerce_iso(value)
        return data

    async def upsert_connection(
        self,
        *,
        scope: PrivateResourceScope,
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
        channel_instance_id: uuid.UUID | str | None = None,
        transaction_guard: TransactionGuard | None = None,
    ) -> dict[str, Any]:
        project_id, owner_user_id = self._coordinates(scope)
        instance_id = self._channel_instance_uuid(channel_instance_id)
        external_account_id_value = self._normalize_optional_identity(external_account_id)
        workspace_id_value = self._normalize_optional_identity(workspace_id)

        def _apply(row: ChannelConnectionRow) -> None:
            row.status = status
            row.external_account_name = external_account_name
            row.workspace_name = workspace_name
            row.bot_user_id = bot_user_id
            row.scopes_json = list(scopes or [])
            row.capabilities_json = dict(capabilities or {})
            row.metadata_json = dict(metadata or {})

        async def _revoke_other_active_scopes(session: AsyncSession) -> None:
            if status != "connected":
                return
            with session.no_autoflush:
                result = await session.execute(
                    select(ChannelConnectionRow.id).where(
                        ChannelConnectionRow.provider == provider,
                        self._connection_instance_condition(instance_id),
                        ChannelConnectionRow.external_account_id == external_account_id_value,
                        ChannelConnectionRow.workspace_id == workspace_id_value,
                        ChannelConnectionRow.status == "connected",
                        or_(
                            ChannelConnectionRow.project_id != project_id,
                            ChannelConnectionRow.owner_user_id != owner_user_id,
                        ),
                    )
                )
            transferred_ids = [row_id for row_id in result.scalars()]
            if not transferred_ids:
                return
            await session.execute(update(ChannelConnectionRow).where(ChannelConnectionRow.id.in_(transferred_ids)).values(status="revoked"))
            await session.execute(delete(ChannelCredentialRow).where(ChannelCredentialRow.connection_id.in_(transferred_ids)))

        stmt = select(ChannelConnectionRow).where(
            ChannelConnectionRow.project_id == project_id,
            ChannelConnectionRow.owner_user_id == owner_user_id,
            ChannelConnectionRow.provider == provider,
            self._connection_instance_condition(instance_id),
            ChannelConnectionRow.external_account_id == external_account_id_value,
            ChannelConnectionRow.workspace_id == workspace_id_value,
        )

        async with self.session_factory() as session:
            last_error: IntegrityError | None = None
            for _ in range(_UPSERT_MAX_ATTEMPTS):
                try:
                    if transaction_guard is not None:
                        await transaction_guard(session)
                    await lock_channel_identities(
                        session,
                        ((provider, external_account_id_value, workspace_id_value),),
                    )
                    row = (await session.execute(stmt)).scalar_one_or_none()
                    # Revoke any other owner's active row for this external identity
                    # *before* our connected row is flushed, so the partial unique
                    # index on active identities is satisfied at commit time.
                    await _revoke_other_active_scopes(session)
                    if row is None:
                        row = ChannelConnectionRow(
                            id=self._new_id(),
                            project_id=project_id,
                            owner_user_id=owner_user_id,
                            provider=provider,
                            channel_instance_id=instance_id,
                            external_account_id=external_account_id_value,
                            workspace_id=workspace_id_value,
                        )
                        session.add(row)
                    _apply(row)
                    await session.commit()
                    await session.refresh(row)
                    return self._connection_to_dict(row)
                except IntegrityError as exc:
                    # A concurrent writer committed a conflicting row first (this
                    # owner's identity, or the same active external identity). Roll
                    # back and retry: the next pass re-reads the now-visible state,
                    # revokes the newly-committed owner, and writes our row.
                    last_error = exc
                    await session.rollback()
            raise last_error  # type: ignore[misc]  # loop runs at least once

    async def list_connections(
        self,
        scope: PrivateResourceScope,
    ) -> list[dict[str, Any]]:
        project_id, owner_user_id = self._coordinates(scope)
        async with self.session_factory() as session:
            result = await session.execute(
                select(ChannelConnectionRow)
                .where(
                    ChannelConnectionRow.project_id == project_id,
                    ChannelConnectionRow.owner_user_id == owner_user_id,
                )
                .order_by(
                    ChannelConnectionRow.updated_at.desc(),
                    ChannelConnectionRow.id.desc(),
                )
            )
            return [self._connection_to_dict(row) for row in result.scalars()]

    async def disconnect_connection(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
    ) -> bool:
        project_id, owner_user_id = self._coordinates(scope)
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(ChannelConnectionRow).where(
                        ChannelConnectionRow.id == connection_id,
                        ChannelConnectionRow.project_id == project_id,
                        ChannelConnectionRow.owner_user_id == owner_user_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return False

            row.status = "revoked"
            credential = await session.get(ChannelCredentialRow, connection_id)
            if credential is not None:
                await session.delete(credential)
            await session.commit()
            return True

    async def trusted_disconnect_provider_connections(self, *, provider: str) -> int:
        """Revoke all active user connections for an instance-wide provider removal."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(ChannelConnectionRow.id).where(
                    ChannelConnectionRow.provider == provider,
                    ChannelConnectionRow.status != "revoked",
                )
            )
            connection_ids = [row_id for row_id in result.scalars()]
            if not connection_ids:
                return 0

            await session.execute(update(ChannelConnectionRow).where(ChannelConnectionRow.id.in_(connection_ids)).values(status="revoked"))
            await session.execute(delete(ChannelCredentialRow).where(ChannelCredentialRow.connection_id.in_(connection_ids)))
            await session.commit()
            return len(connection_ids)

    async def disconnect_provider_connections(self, *, provider: str) -> int:
        """Compatibility alias for the trusted instance-wide admin operation."""
        return await self.trusted_disconnect_provider_connections(provider=provider)

    async def store_credentials(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        access_token: str | None,
        refresh_token: str | None = None,
        token_type: str | None = None,
        expires_at: datetime | None = None,
        refresh_expires_at: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        project_id, owner_user_id = self._coordinates(scope)
        if self._cipher is None:
            raise RuntimeError("channel connection encryption key is required")
        async with self.session_factory() as session:
            connection = (
                await session.execute(
                    select(ChannelConnectionRow.id).where(
                        ChannelConnectionRow.id == connection_id,
                        ChannelConnectionRow.project_id == project_id,
                        ChannelConnectionRow.owner_user_id == owner_user_id,
                        ChannelConnectionRow.status == "connected",
                    )
                )
            ).scalar_one_or_none()
            if connection is None:
                return False
            row = await session.get(ChannelCredentialRow, connection_id)
            if row is None:
                row = ChannelCredentialRow(connection_id=connection_id)
                session.add(row)
            row.encrypted_access_token = self._cipher.encrypt_text(access_token)
            row.encrypted_refresh_token = self._cipher.encrypt_text(refresh_token)
            row.token_type = token_type
            row.expires_at = expires_at
            row.refresh_expires_at = refresh_expires_at
            row.encrypted_extra_json = self._cipher.encrypt_text(json.dumps(extra or {}, ensure_ascii=False))
            row.version = (row.version or 0) + 1
            await session.commit()
            return True

    async def get_credentials(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
    ) -> dict[str, Any] | None:
        project_id, owner_user_id = self._coordinates(scope)
        if self._cipher is None:
            return None
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(ChannelCredentialRow)
                    .join(
                        ChannelConnectionRow,
                        ChannelConnectionRow.id == ChannelCredentialRow.connection_id,
                    )
                    .where(
                        ChannelConnectionRow.id == connection_id,
                        ChannelConnectionRow.project_id == project_id,
                        ChannelConnectionRow.owner_user_id == owner_user_id,
                        ChannelConnectionRow.status == "connected",
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            try:
                extra_raw = self._cipher.decrypt_text(row.encrypted_extra_json)
                return {
                    "connection_id": row.connection_id,
                    "access_token": self._cipher.decrypt_text(row.encrypted_access_token),
                    "refresh_token": self._cipher.decrypt_text(row.encrypted_refresh_token),
                    "token_type": row.token_type,
                    "expires_at": self._coerce_datetime(row.expires_at),
                    "refresh_expires_at": self._coerce_datetime(row.refresh_expires_at),
                    "extra": json.loads(extra_raw) if extra_raw else {},
                }
            except (InvalidToken, UnicodeError, json.JSONDecodeError):
                logger.warning(
                    "Unable to decrypt channel connection credentials; treating credentials as unavailable",
                    exc_info=True,
                )
                return None

    @staticmethod
    def hash_state(state: str) -> str:
        return hashlib.sha256(state.encode("utf-8")).hexdigest()

    async def create_oauth_state(
        self,
        *,
        scope: PrivateResourceScope,
        provider: str,
        state: str,
        expires_at: datetime,
        code_verifier: str | None = None,
        nonce_hash: str | None = None,
        redirect_after: str | None = None,
        requested_scopes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        channel_instance_id: uuid.UUID | str | None = None,
    ) -> None:
        project_id, owner_user_id = self._coordinates(scope)
        instance_id = self._channel_instance_uuid(channel_instance_id)
        row = ChannelOAuthStateRow(
            state_hash=self.hash_state(state),
            project_id=project_id,
            owner_user_id=owner_user_id,
            provider=provider,
            channel_instance_id=instance_id,
            code_verifier_encrypted=self._encrypt_optional_secret(code_verifier),
            nonce_hash=nonce_hash,
            redirect_after=redirect_after,
            requested_scopes_json=list(requested_scopes or []),
            metadata_json=dict(metadata or {}),
            expires_at=expires_at,
        )
        async with self.session_factory() as session:
            session.add(row)
            await session.commit()

    async def create_oauth_state_within_cap(
        self,
        *,
        scope: PrivateResourceScope,
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
        channel_instance_id: uuid.UUID | str | None = None,
        transaction_guard: TransactionGuard | None = None,
    ) -> bool:
        """Atomically enforce the per-private-scope/provider pending cap."""
        project_id, owner_user_id = self._coordinates(scope)
        instance_id = self._channel_instance_uuid(channel_instance_id)

        current_time = now or datetime.now(UTC)
        async with self.session_factory() as session:
            if transaction_guard is not None:
                await transaction_guard(session)
            await self._serialize_oauth_scope(
                session,
                project_id,
                owner_user_id,
                provider,
                instance_id,
            )
            await session.execute(
                delete(ChannelOAuthStateRow).where(
                    ChannelOAuthStateRow.project_id == project_id,
                    ChannelOAuthStateRow.owner_user_id == owner_user_id,
                    ChannelOAuthStateRow.provider == provider,
                    self._oauth_instance_condition(instance_id),
                    ChannelOAuthStateRow.expires_at < current_time,
                )
            )
            pending = await session.execute(
                select(func.count())
                .select_from(ChannelOAuthStateRow)
                .where(
                    ChannelOAuthStateRow.project_id == project_id,
                    ChannelOAuthStateRow.owner_user_id == owner_user_id,
                    ChannelOAuthStateRow.provider == provider,
                    self._oauth_instance_condition(instance_id),
                    ChannelOAuthStateRow.consumed_at.is_(None),
                    ChannelOAuthStateRow.expires_at >= current_time,
                )
            )
            if int(pending.scalar_one()) >= max_pending:
                await session.rollback()
                return False
            session.add(
                ChannelOAuthStateRow(
                    state_hash=self.hash_state(state),
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    provider=provider,
                    channel_instance_id=instance_id,
                    code_verifier_encrypted=self._encrypt_optional_secret(code_verifier),
                    nonce_hash=nonce_hash,
                    redirect_after=redirect_after,
                    requested_scopes_json=list(requested_scopes or []),
                    metadata_json=dict(metadata or {}),
                    expires_at=expires_at,
                )
            )
            await session.commit()
            return True

    async def _serialize_oauth_scope(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        owner_user_id: str,
        provider: str,
        channel_instance_id: uuid.UUID | None,
    ) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {
                "lock_key": self._oauth_scope_lock_key(
                    project_id,
                    owner_user_id,
                    provider,
                    channel_instance_id,
                )
            },
        )

    @staticmethod
    def _oauth_scope_lock_key(
        project_id: uuid.UUID,
        owner_user_id: str,
        provider: str,
        channel_instance_id: uuid.UUID | None,
    ) -> int:
        digest = hashlib.sha256(f"{project_id}\x00{owner_user_id}\x00{provider}\x00{channel_instance_id or 'legacy'}".encode()).digest()
        # 63-bit non-negative key for pg_advisory_xact_lock(bigint).
        return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF

    async def delete_expired_oauth_states(self, *, now: datetime | None = None) -> int:
        current_time = now or datetime.now(UTC)
        async with self.session_factory() as session:
            result = await session.execute(delete(ChannelOAuthStateRow).where(ChannelOAuthStateRow.expires_at < current_time))
            await session.commit()
            return int(result.rowcount or 0)

    async def count_oauth_states(
        self,
        *,
        scope: PrivateResourceScope,
        provider: str,
        active_only: bool = False,
        now: datetime | None = None,
        channel_instance_id: uuid.UUID | str | None = None,
    ) -> int:
        project_id, owner_user_id = self._coordinates(scope)
        instance_id = self._channel_instance_uuid(channel_instance_id)
        current_time = now or datetime.now(UTC)
        conditions = [
            ChannelOAuthStateRow.project_id == project_id,
            ChannelOAuthStateRow.owner_user_id == owner_user_id,
            ChannelOAuthStateRow.provider == provider,
            self._oauth_instance_condition(instance_id),
        ]
        if active_only:
            conditions.extend(
                [
                    ChannelOAuthStateRow.consumed_at.is_(None),
                    ChannelOAuthStateRow.expires_at >= current_time,
                ]
            )

        async with self.session_factory() as session:
            result = await session.execute(select(func.count()).select_from(ChannelOAuthStateRow).where(*conditions))
            return int(result.scalar_one())

    async def consume_oauth_state(
        self,
        *,
        provider: str,
        state: str,
        now: datetime | None = None,
        channel_instance_id: uuid.UUID | str | None = None,
    ) -> dict[str, Any] | None:
        current_time = now or datetime.now(UTC)
        instance_id = self._channel_instance_uuid(channel_instance_id)
        state_hash = self.hash_state(state)
        async with self.session_factory() as session:
            await session.execute(delete(ChannelOAuthStateRow).where(ChannelOAuthStateRow.expires_at < current_time))
            row = await session.get(ChannelOAuthStateRow, state_hash)
            if row is None or row.provider != provider or row.channel_instance_id != instance_id or row.consumed_at is not None:
                await session.commit()
                return None
            expires_at = self._coerce_datetime(row.expires_at)
            if expires_at is not None and expires_at < current_time:
                await session.commit()
                return None

            # Conditional UPDATE so two concurrent workers cannot both consume
            # the same binding code: only the writer that flips consumed_at
            # from NULL wins.
            result = await session.execute(
                update(ChannelOAuthStateRow)
                .where(
                    ChannelOAuthStateRow.state_hash == state_hash,
                    ChannelOAuthStateRow.consumed_at.is_(None),
                )
                .values(consumed_at=current_time)
            )
            await session.commit()
            if result.rowcount != 1:
                return None
            return {
                "project_id": str(row.project_id),
                "owner_user_id": row.owner_user_id,
                "provider": row.provider,
                "channel_instance_id": (str(row.channel_instance_id) if row.channel_instance_id is not None else None),
                "requested_scopes": row.requested_scopes_json or [],
                "metadata": row.metadata_json or {},
                "redirect_after": row.redirect_after,
            }

    async def find_connection_by_external_identity(
        self,
        *,
        provider: str,
        external_account_id: str,
        workspace_id: str | None = None,
        channel_instance_id: uuid.UUID | str | None = None,
        expected_connection_id: str | None = None,
        expected_scope: PrivateResourceScope | None = None,
    ) -> dict[str, Any] | None:
        instance_id = self._channel_instance_uuid(channel_instance_id)
        expected_project_id: uuid.UUID | None = None
        expected_owner_user_id: str | None = None
        if expected_connection_id is not None:
            if type(expected_connection_id) is not str or not expected_connection_id or len(expected_connection_id) > 64:
                return None
        if expected_scope is not None:
            expected_project_id, expected_owner_user_id = self._coordinates(
                expected_scope,
            )
        identity_candidates = self._lookup_identity_candidates(
            provider,
            instance_id,
            external_account_id,
            workspace_id,
        )
        conditions = [
            ChannelConnectionRow.provider == provider,
            self._connection_instance_condition(instance_id),
            ChannelConnectionRow.status == "connected",
            ProjectMembershipRow.status == "active",
        ]
        if expected_connection_id is not None:
            conditions.append(ChannelConnectionRow.id == expected_connection_id)
        else:
            conditions.append(
                or_(
                    *(
                        and_(
                            ChannelConnectionRow.external_account_id == account_id,
                            ChannelConnectionRow.workspace_id == candidate_workspace_id,
                        )
                        for account_id, candidate_workspace_id in identity_candidates
                    )
                )
            )
        if expected_project_id is not None and expected_owner_user_id is not None:
            conditions.extend(
                [
                    ChannelConnectionRow.project_id == expected_project_id,
                    ChannelConnectionRow.owner_user_id == expected_owner_user_id,
                ]
            )
        async with self.session_factory() as session:
            result = await session.execute(
                select(
                    ChannelConnectionRow,
                    ProjectMembershipRow.version.label("membership_version"),
                )
                .join(
                    ProjectMembershipRow,
                    (ProjectMembershipRow.project_id == ChannelConnectionRow.project_id) & (ProjectMembershipRow.user_id == ChannelConnectionRow.owner_user_id),
                )
                .where(*conditions)
                .order_by(ChannelConnectionRow.updated_at.desc(), ChannelConnectionRow.id.desc())
                .limit(1)
            )
            resolved = result.one_or_none()
            if resolved is None:
                return None
            row, membership_version = resolved
            if expected_connection_id is not None and (row.external_account_id, row.workspace_id) not in identity_candidates:
                return None
            data = self._connection_to_dict(row)
            data["membership_version"] = int(membership_version)
            return data

    async def set_thread_id(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        provider: str,
        external_conversation_id: str,
        thread_id: str,
        external_topic_id: str | None = None,
    ) -> bool:
        project_id, owner_user_id = self._coordinates(scope)
        topic_id = external_topic_id or ""
        async with self.session_factory() as session, session.begin():
            project = (
                await session.execute(
                    select(ProjectRow.id)
                    .where(
                        ProjectRow.id == project_id,
                        ProjectRow.status == "active",
                        ProjectRow.is_suspended.is_(False),
                    )
                    .with_for_update(read=True, of=ProjectRow)
                )
            ).scalar_one_or_none()
            if project is None:
                return False
            connection = (
                await session.execute(
                    select(ChannelConnectionRow)
                    .where(
                        ChannelConnectionRow.id == connection_id,
                        ChannelConnectionRow.project_id == project_id,
                        ChannelConnectionRow.owner_user_id == owner_user_id,
                        ChannelConnectionRow.provider == provider,
                        ChannelConnectionRow.status == "connected",
                    )
                    .with_for_update(of=ChannelConnectionRow)
                )
            ).scalar_one_or_none()
            if connection is None:
                return False

            connection_metadata = connection.metadata_json or {}
            if connection_metadata.get("group_binding_id") is not None:
                try:
                    connection_agent_asset_id = uuid.UUID(
                        connection_metadata["agent_asset_id"],
                    )
                except (KeyError, TypeError, ValueError):
                    return False
                connection_agent_scope = connection_metadata.get("agent_scope")
                if connection_agent_scope not in {"project", "system"}:
                    return False
                thread = (
                    await session.execute(
                        select(ThreadMetaRow.thread_id).where(
                            ThreadMetaRow.thread_id == thread_id,
                            ThreadMetaRow.project_id == project_id,
                            ThreadMetaRow.owner_user_id == owner_user_id,
                            ThreadMetaRow.agent_asset_id == connection_agent_asset_id,
                            ThreadMetaRow.agent_scope == connection_agent_scope,
                            ThreadMetaRow.deleted_at.is_(None),
                            ThreadMetaRow.frozen_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if thread is None:
                    return False
            insert_result = await session.execute(
                pg_insert(ChannelConversationRow)
                .values(
                    id=self._new_id(),
                    project_id=project_id,
                    connection_id=connection_id,
                    owner_user_id=owner_user_id,
                    provider=provider,
                    external_conversation_id=external_conversation_id,
                    external_topic_id=topic_id,
                    thread_id=thread_id,
                )
                .on_conflict_do_nothing(
                    constraint="uq_channel_conversation_connection_external",
                )
                .returning(ChannelConversationRow.id)
            )
            if insert_result.scalar_one_or_none() is not None:
                return True

            # Another process already owns this exact provider conversation.
            # Identical retries are idempotent; a different Thread never
            # silently remaps established private work.
            existing = (
                await session.execute(
                    select(
                        ChannelConversationRow.provider,
                        ChannelConversationRow.thread_id,
                    ).where(
                        ChannelConversationRow.project_id == project_id,
                        ChannelConversationRow.owner_user_id == owner_user_id,
                        ChannelConversationRow.connection_id == connection_id,
                        ChannelConversationRow.external_conversation_id == external_conversation_id,
                        ChannelConversationRow.external_topic_id == topic_id,
                    )
                )
            ).one_or_none()
            return existing is not None and existing[0] == provider and existing[1] == thread_id

    async def get_thread_id(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        external_conversation_id: str,
        external_topic_id: str | None = None,
        provider: str | None = None,
    ) -> str | None:
        project_id, owner_user_id = self._coordinates(scope)
        async with self.session_factory() as session:
            conditions = [
                ChannelConversationRow.project_id == project_id,
                ChannelConversationRow.owner_user_id == owner_user_id,
                ChannelConversationRow.connection_id == connection_id,
                ChannelConversationRow.external_conversation_id == external_conversation_id,
                ChannelConversationRow.external_topic_id == (external_topic_id or ""),
            ]
            if provider is not None:
                conditions.append(ChannelConversationRow.provider == provider)
            stmt = select(ChannelConversationRow.thread_id).where(*conditions)
            return (await session.execute(stmt)).scalar_one_or_none()

    async def remove_thread_ids(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        provider: str,
        external_conversation_id: str,
        external_topic_id: str | None = None,
    ) -> bool:
        """Delete one topic mapping, or every topic for one conversation."""
        project_id, owner_user_id = self._coordinates(scope)
        conditions = [
            ChannelConversationRow.project_id == project_id,
            ChannelConversationRow.owner_user_id == owner_user_id,
            ChannelConversationRow.connection_id == connection_id,
            ChannelConversationRow.provider == provider,
            ChannelConversationRow.external_conversation_id == external_conversation_id,
        ]
        if external_topic_id is not None:
            conditions.append(ChannelConversationRow.external_topic_id == external_topic_id)
        async with self.session_factory() as session:
            result = await session.execute(delete(ChannelConversationRow).where(*conditions))
            await session.commit()
            return bool(result.rowcount)

    async def list_thread_ids(
        self,
        *,
        scope: PrivateResourceScope,
        connection_id: str,
        provider: str | None = None,
    ) -> list[dict[str, Any]]:
        """List mappings for one exact private connection."""
        project_id, owner_user_id = self._coordinates(scope)
        conditions = [
            ChannelConversationRow.project_id == project_id,
            ChannelConversationRow.owner_user_id == owner_user_id,
            ChannelConversationRow.connection_id == connection_id,
        ]
        if provider is not None:
            conditions.append(ChannelConversationRow.provider == provider)
        async with self.session_factory() as session:
            result = await session.execute(
                select(ChannelConversationRow)
                .where(*conditions)
                .order_by(
                    ChannelConversationRow.created_at.asc(),
                    ChannelConversationRow.id.asc(),
                )
            )
            entries: list[dict[str, Any]] = []
            for row in result.scalars():
                entry: dict[str, Any] = {
                    "channel_name": row.provider,
                    "chat_id": row.external_conversation_id,
                    "thread_id": row.thread_id,
                    "created_at": self._coerce_datetime(row.created_at),
                    "updated_at": self._coerce_datetime(row.updated_at),
                }
                if row.external_topic_id:
                    entry["topic_id"] = row.external_topic_id
                entries.append(entry)
            return entries
