"""SQLAlchemy-backed thread metadata repository."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.json_compat import json_match
from deerflow.persistence.thread_meta.base import InvalidMetadataFilterError, ThreadMetaStore
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.user_context import AUTO, _AutoSentinel, resolve_user_id
from deerflow.utils.time import coerce_iso

logger = logging.getLogger(__name__)


class ThreadMetaRepository(ThreadMetaStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _thread_predicate(
        thread_id: str,
        resolved_user_id: str | None,
        scope: PrivateResourceScope | None,
    ):
        predicate = [
            ThreadMetaRow.thread_id == thread_id,
            ThreadMetaRow.deleted_at.is_(None),
        ]
        if scope is not None:
            predicate.extend(
                (
                    ThreadMetaRow.project_id == uuid.UUID(scope.project_id),
                    ThreadMetaRow.owner_user_id == scope.owner_user_id,
                    ThreadMetaRow.frozen_at.is_(None),
                )
            )
        elif resolved_user_id is not None:
            predicate.append(ThreadMetaRow.owner_user_id == resolved_user_id)
        return tuple(predicate)

    @staticmethod
    def _row_to_dict(row: ThreadMetaRow) -> dict[str, Any]:
        d = row.to_dict()
        d["user_id"] = d.get("owner_user_id")
        d["metadata"] = d.pop("metadata_json", None) or {}
        for key in ("created_at", "updated_at"):
            val = d.get(key)
            if isinstance(val, datetime):
                # Normalize legacy naive values as UTC so the wire format always carries tz.
                d[key] = coerce_iso(val)
        return d

    async def create(
        self,
        thread_id: str,
        *,
        assistant_id: str | None = None,
        user_id: str | None | _AutoSentinel = AUTO,
        display_name: str | None = None,
        metadata: dict | None = None,
        scope: PrivateResourceScope | None = None,
        agent_asset_id: uuid.UUID | None = None,
        agent_scope: str | None = None,
    ) -> dict:
        # Project creates take owner identity from the trusted scope. Ownerless
        # legacy rows cannot be created after the final M4 schema constraint.
        if scope is None or agent_asset_id is None or agent_scope not in {"system", "project"}:
            raise RuntimeError("ThreadMetaRepository.create requires scoped final-schema authority")
        resolved_user_id = scope.owner_user_id
        try:
            project_id = uuid.UUID(scope.project_id)
        except (TypeError, ValueError):
            raise RuntimeError("invalid final-schema thread project scope") from None
        now = datetime.now(UTC)
        row = ThreadMetaRow(
            thread_id=thread_id,
            assistant_id=assistant_id,
            user_id=resolved_user_id,
            display_name=display_name,
            metadata_json=metadata or {},
            created_at=now,
            updated_at=now,
            project_id=project_id,
            agent_asset_id=agent_asset_id,
            agent_scope=agent_scope,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def get(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> dict | None:
        resolved_user_id = None if scope is not None else resolve_user_id(user_id, method_name="ThreadMetaRepository.get")
        async with self._sf() as session:
            statement = select(ThreadMetaRow).where(*self._thread_predicate(thread_id, resolved_user_id, scope))
            row = (await session.execute(statement)).scalar_one_or_none()
            return None if row is None else self._row_to_dict(row)

    async def check_access(
        self,
        thread_id: str,
        user_id: str,
        *,
        require_existing: bool = False,
        scope: PrivateResourceScope | None = None,
    ) -> bool:
        """Check if ``user_id`` has access to ``thread_id``.

        Two modes — one row, two distinct semantics depending on what
        the caller is about to do:

        - ``require_existing=False`` (default, permissive):
          Returns True for: row missing (untracked legacy thread),
          ``row.user_id`` is None (shared / pre-auth data),
          or ``row.user_id == user_id``. Use for **read-style**
          decorators where treating an untracked thread as accessible
          preserves backward-compat.

        - ``require_existing=True`` (strict):
          Returns True **only** when the row exists AND
          (``row.user_id == user_id`` OR ``row.user_id is None``).
          Use for **destructive / mutating** decorators (DELETE, PATCH,
          state-update) so a thread that has *already been deleted*
          cannot be re-targeted by any caller — closing the
          delete-idempotence cross-user gap where the row vanishing
          made every other user appear to "own" it.
        """
        async with self._sf() as session:
            if scope is not None:
                statement = select(ThreadMetaRow.owner_user_id).where(*self._thread_predicate(thread_id, user_id, scope))
                row_owner = (await session.execute(statement)).scalar_one_or_none()
                return row_owner is not None and user_id == scope.owner_user_id

            # Query by identity before applying active-state filters. A durable
            # tombstone must never collapse into the permissive "untracked
            # legacy thread" case used by read-only compatibility routes.
            statement = select(
                ThreadMetaRow.owner_user_id,
                ThreadMetaRow.deleted_at,
                ThreadMetaRow.frozen_at,
            ).where(ThreadMetaRow.thread_id == thread_id)
            row = (await session.execute(statement)).one_or_none()
            if row is None:
                return not require_existing
            if row.deleted_at is not None or row.frozen_at is not None:
                return False
            return row.owner_user_id == user_id

    async def search(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> list[dict[str, Any]]:
        """Search threads with optional metadata and status filters.

        Owner filter is enforced by default: caller must be in a user
        context. Pass ``user_id=None`` to bypass (migration/CLI).
        """
        resolved_user_id = None if scope is not None else resolve_user_id(user_id, method_name="ThreadMetaRepository.search")
        stmt = select(ThreadMetaRow).where(ThreadMetaRow.deleted_at.is_(None)).order_by(ThreadMetaRow.updated_at.desc(), ThreadMetaRow.thread_id.desc())
        if scope is not None:
            stmt = stmt.where(
                ThreadMetaRow.project_id == uuid.UUID(scope.project_id),
                ThreadMetaRow.owner_user_id == scope.owner_user_id,
                ThreadMetaRow.frozen_at.is_(None),
            )
        if resolved_user_id is not None:
            stmt = stmt.where(ThreadMetaRow.user_id == resolved_user_id)
        if status:
            stmt = stmt.where(ThreadMetaRow.status == status)

        if metadata:
            applied = 0
            for key, value in metadata.items():
                try:
                    stmt = stmt.where(json_match(ThreadMetaRow.metadata_json, key, value))
                    applied += 1
                except (ValueError, TypeError) as exc:
                    logger.warning("Skipping metadata filter key %s: %s", ascii(key), exc)
            if applied == 0:
                # Comma-separated plain string (no list repr / nested
                # quoting) so the 400 detail surfaced by the Gateway is
                # easy for clients to read. Sorted for determinism.
                rejected_keys = ", ".join(sorted(str(k) for k in metadata))
                raise InvalidMetadataFilterError(f"All metadata filter keys were rejected as unsafe: {rejected_keys}")

        stmt = stmt.limit(limit).offset(offset)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def update_display_name(
        self,
        thread_id: str,
        display_name: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        """Update the display_name (title) for a thread."""
        resolved_user_id = None if scope is not None else resolve_user_id(user_id, method_name="ThreadMetaRepository.update_display_name")
        async with self._sf() as session:
            await session.execute(update(ThreadMetaRow).where(*self._thread_predicate(thread_id, resolved_user_id, scope)).values(display_name=display_name, updated_at=datetime.now(UTC)))
            await session.commit()

    async def update_status(
        self,
        thread_id: str,
        status: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        resolved_user_id = None if scope is not None else resolve_user_id(user_id, method_name="ThreadMetaRepository.update_status")
        async with self._sf() as session:
            await session.execute(update(ThreadMetaRow).where(*self._thread_predicate(thread_id, resolved_user_id, scope)).values(status=status, updated_at=datetime.now(UTC)))
            await session.commit()

    async def update_metadata(
        self,
        thread_id: str,
        metadata: dict,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        """Merge ``metadata`` into ``metadata_json``.

        Read-modify-write inside a single session/transaction so concurrent
        callers see consistent state. No-op if the row does not exist or
        the user_id check fails.
        """
        resolved_user_id = None if scope is not None else resolve_user_id(user_id, method_name="ThreadMetaRepository.update_metadata")
        async with self._sf() as session:
            statement = select(ThreadMetaRow).where(*self._thread_predicate(thread_id, resolved_user_id, scope)).with_for_update(of=ThreadMetaRow)
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                return
            merged = dict(row.metadata_json or {})
            merged.update(metadata)
            row.metadata_json = merged
            row.updated_at = datetime.now(UTC)
            await session.commit()

    async def update_owner(
        self,
        thread_id: str,
        owner_user_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        """Move a thread metadata row to ``owner_user_id``."""
        if scope is not None:
            raise RuntimeError("scoped project thread ownership is immutable")
        resolved_user_id = resolve_user_id(user_id, method_name="ThreadMetaRepository.update_owner")
        async with self._sf() as session:
            await session.execute(update(ThreadMetaRow).where(*self._thread_predicate(thread_id, resolved_user_id, None)).values(user_id=owner_user_id, updated_at=datetime.now(UTC)))
            await session.commit()

    async def delete(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        resolved_user_id = None if scope is not None else resolve_user_id(user_id, method_name="ThreadMetaRepository.delete")
        async with self._sf() as session:
            await session.execute(sql_delete(ThreadMetaRow).where(*self._thread_predicate(thread_id, resolved_user_id, scope)))
            await session.commit()

    async def mark_deleted(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> bool:
        resolved_user_id = (
            None
            if scope is not None
            else resolve_user_id(
                user_id,
                method_name="ThreadMetaRepository.mark_deleted",
            )
        )
        now = datetime.now(UTC)
        async with self._sf() as session:
            result = await session.execute(
                update(ThreadMetaRow)
                .where(*self._thread_predicate(thread_id, resolved_user_id, scope))
                .values(
                    deleted_at=now,
                    checkpoint_delete_status="pending",
                    updated_at=now,
                    version=ThreadMetaRow.version + 1,
                )
                .returning(ThreadMetaRow.thread_id)
            )
            await session.commit()
            return result.scalar_one_or_none() is not None

    async def set_checkpoint_delete_status(
        self,
        thread_id: str,
        status: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> bool:
        if status not in {"pending", "complete", "retry_required"}:
            raise ValueError("invalid checkpoint delete status")
        resolved_user_id = (
            None
            if scope is not None
            else resolve_user_id(
                user_id,
                method_name="ThreadMetaRepository.set_checkpoint_delete_status",
            )
        )
        predicate = [
            ThreadMetaRow.thread_id == thread_id,
            ThreadMetaRow.deleted_at.is_not(None),
        ]
        if scope is not None:
            predicate.extend(
                (
                    ThreadMetaRow.project_id == uuid.UUID(scope.project_id),
                    ThreadMetaRow.owner_user_id == scope.owner_user_id,
                )
            )
        elif resolved_user_id is not None:
            predicate.append(ThreadMetaRow.owner_user_id == resolved_user_id)
        async with self._sf() as session:
            result = await session.execute(
                update(ThreadMetaRow)
                .where(*predicate)
                .values(
                    checkpoint_delete_status=status,
                    updated_at=datetime.now(UTC),
                )
                .returning(ThreadMetaRow.thread_id)
            )
            await session.commit()
            return result.scalar_one_or_none() is not None
