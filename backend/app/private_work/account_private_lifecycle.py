"""Durable account-private admission guard after Project membership locks."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.user.model import UserRow
from deerflow.persistence.user.private_lifecycle import AccountPrivateGeneration


@dataclass(frozen=True, slots=True)
class LockedAccountPrivateScope:
    """Stable Project/Membership set plus the already locked User row."""

    owner_user_id: str
    project_ids: tuple[uuid.UUID, ...]
    membership_ids: tuple[uuid.UUID, ...]
    state: str
    generation: int
    _user_row: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class AccountPrivatePurgeFence:
    owner_user_id: str
    generation: int
    effective_at: datetime
    project_ids: tuple[uuid.UUID, ...]
    membership_ids: tuple[uuid.UUID, ...]


class AccountPrivateLifecycleClosed(RuntimeError):
    """The account cannot currently admit new owner-private scope."""


class AccountPrivateScopeChanged(RuntimeError):
    """The pre-read scope changed before the User serialization lock."""


class AccountPrivateLifecyclePort(Protocol):
    async def require_active_after_membership(
        self,
        session: AsyncSession,
        owner_user_id: uuid.UUID | str,
    ) -> AccountPrivateGeneration: ...

    async def reactivate_after_membership(
        self,
        session: AsyncSession,
        owner_user_id: uuid.UUID | str,
    ) -> AccountPrivateGeneration: ...

    async def begin_purge_after_memberships(
        self,
        session: AsyncSession,
        locked_scope: LockedAccountPrivateScope,
        *,
        effective_at: datetime,
    ) -> AccountPrivatePurgeFence: ...

    async def lock_stable_scope_for_purge(
        self,
        session: AsyncSession,
        owner_user_id: uuid.UUID | str,
    ) -> LockedAccountPrivateScope: ...


class AccountPrivateLifecycle(AccountPrivateLifecyclePort):
    """Own the User-row lifecycle check without owning caller transactions."""

    async def require_active_after_membership(
        self,
        session: AsyncSession,
        owner_user_id: uuid.UUID | str,
    ) -> AccountPrivateGeneration:
        """Lock and return the active generation after membership authority.

        The method name is an ordering contract: callers must already own their
        Project and Membership prefix.  This module never acquires those locks.
        """

        try:
            normalized_owner = str(uuid.UUID(str(owner_user_id)))
        except (TypeError, ValueError):
            raise AccountPrivateLifecycleClosed from None
        if not session.in_transaction():
            raise RuntimeError("account-private lifecycle guard requires a caller-owned transaction")
        row = await session.scalar(select(UserRow).where(UserRow.id == normalized_owner).with_for_update(read=True, of=UserRow))
        if row is None or row.private_retention_state != "active" or type(row.private_retention_generation) is not int or row.private_retention_generation < 1:
            raise AccountPrivateLifecycleClosed
        return AccountPrivateGeneration(
            owner_user_id=normalized_owner,
            generation=row.private_retention_generation,
        )

    async def reactivate_after_membership(
        self,
        session: AsyncSession,
        owner_user_id: uuid.UUID | str,
    ) -> AccountPrivateGeneration:
        """Explicitly reopen owner-private scope after governance locks.

        Invitation redemption, Membership rejoin, and existing guest reuse are
        the only ordinary writers allowed to reopen a closed account lifecycle.
        Advancing the generation makes every previously issued purge fence
        permanently stale.
        """

        try:
            normalized_owner = str(uuid.UUID(str(owner_user_id)))
        except (TypeError, ValueError):
            raise AccountPrivateLifecycleClosed from None
        if not session.in_transaction():
            raise RuntimeError(
                "account-private lifecycle reactivation requires a caller-owned transaction",
            )
        row = await session.scalar(
            select(UserRow).where(UserRow.id == normalized_owner).with_for_update(key_share=True, of=UserRow),
        )
        if row is None or row.private_retention_state not in {"active", "pending_deletion", "purged"} or type(row.private_retention_generation) is not int or row.private_retention_generation < 1:
            raise AccountPrivateLifecycleClosed
        if row.private_retention_state != "active":
            row.private_retention_state = "active"
            row.private_retention_generation += 1
            row.private_retention_effective_at = None
        return AccountPrivateGeneration(
            owner_user_id=normalized_owner,
            generation=row.private_retention_generation,
        )

    async def begin_purge_after_memberships(
        self,
        session: AsyncSession,
        locked_scope: LockedAccountPrivateScope,
        *,
        effective_at: datetime,
    ) -> AccountPrivatePurgeFence:
        """Close admission under a stable, already locked account scope."""

        if type(locked_scope) is not LockedAccountPrivateScope:
            raise TypeError("locked account-private scope is required")
        if not session.in_transaction():
            raise RuntimeError(
                "account-private purge transition requires a caller-owned transaction",
            )
        if not isinstance(effective_at, datetime) or effective_at.tzinfo is None or effective_at.utcoffset() is None:
            raise ValueError("account-private purge time must be timezone-aware")
        normalized_effective_at = effective_at.astimezone(UTC)
        row = locked_scope._user_row
        if (
            getattr(row, "id", None) != locked_scope.owner_user_id
            or getattr(row, "private_retention_state", None) != locked_scope.state
            or getattr(row, "private_retention_generation", None) != locked_scope.generation
            or type(locked_scope.generation) is not int
            or locked_scope.generation < 1
        ):
            raise AccountPrivateLifecycleClosed
        if locked_scope.state == "active":
            row.private_retention_state = "pending_deletion"
            row.private_retention_generation += 1
            row.private_retention_effective_at = normalized_effective_at
        elif not (locked_scope.state == "pending_deletion" and getattr(row, "private_retention_effective_at", None) == normalized_effective_at):
            raise AccountPrivateLifecycleClosed
        return AccountPrivatePurgeFence(
            owner_user_id=locked_scope.owner_user_id,
            generation=row.private_retention_generation,
            effective_at=normalized_effective_at,
            project_ids=locked_scope.project_ids,
            membership_ids=locked_scope.membership_ids,
        )

    @staticmethod
    async def _read_scope_coordinates(
        session: AsyncSession,
        owner_user_id: str,
    ) -> tuple[tuple[uuid.UUID, ...], tuple[uuid.UUID, ...]]:
        rows = (
            await session.execute(
                select(
                    ProjectMembershipRow.project_id,
                    ProjectMembershipRow.id,
                )
                .where(ProjectMembershipRow.user_id == owner_user_id)
                .order_by(
                    ProjectMembershipRow.project_id,
                    ProjectMembershipRow.user_id,
                    ProjectMembershipRow.id,
                ),
            )
        ).all()
        return (
            tuple(sorted({row.project_id for row in rows})),
            tuple(row.id for row in rows),
        )

    async def lock_stable_scope_for_purge(
        self,
        session: AsyncSession,
        owner_user_id: uuid.UUID | str,
    ) -> LockedAccountPrivateScope:
        """Lock a complete account scope without ever reversing User→Project."""

        try:
            normalized_owner = str(uuid.UUID(str(owner_user_id)))
        except (TypeError, ValueError):
            raise AccountPrivateLifecycleClosed from None
        if not session.in_transaction():
            raise RuntimeError(
                "account-private scope lock requires a caller-owned transaction",
            )
        project_ids, membership_ids = await self._read_scope_coordinates(
            session,
            normalized_owner,
        )
        locked_project_ids: tuple[uuid.UUID, ...] = ()
        if project_ids:
            locked_project_ids = tuple(
                (
                    await session.scalars(
                        select(ProjectRow.id).where(ProjectRow.id.in_(project_ids)).order_by(ProjectRow.id).with_for_update(of=ProjectRow),
                    )
                ).all(),
            )
        locked_membership_ids: tuple[uuid.UUID, ...] = ()
        if membership_ids:
            locked_membership_ids = tuple(
                (
                    await session.scalars(
                        select(ProjectMembershipRow.id)
                        .where(
                            ProjectMembershipRow.user_id == normalized_owner,
                            ProjectMembershipRow.id.in_(membership_ids),
                        )
                        .order_by(
                            ProjectMembershipRow.project_id,
                            ProjectMembershipRow.user_id,
                            ProjectMembershipRow.id,
                        )
                        .with_for_update(of=ProjectMembershipRow),
                    )
                ).all(),
            )
        if locked_project_ids != project_ids or locked_membership_ids != membership_ids:
            raise AccountPrivateScopeChanged
        user = await session.scalar(
            select(UserRow).where(UserRow.id == normalized_owner).with_for_update(key_share=True, of=UserRow),
        )
        if user is None or user.private_retention_state not in {"active", "pending_deletion", "purged"} or type(user.private_retention_generation) is not int or user.private_retention_generation < 1:
            raise AccountPrivateLifecycleClosed
        observed_project_ids, observed_membership_ids = await self._read_scope_coordinates(session, normalized_owner)
        if observed_project_ids != project_ids or observed_membership_ids != membership_ids:
            # The caller must roll back and retry from the pre-read.  It is
            # forbidden to acquire a newly observed Project/Membership here.
            raise AccountPrivateScopeChanged
        return LockedAccountPrivateScope(
            owner_user_id=normalized_owner,
            project_ids=project_ids,
            membership_ids=membership_ids,
            state=user.private_retention_state,
            generation=user.private_retention_generation,
            _user_row=user,
        )


__all__ = [
    "AccountPrivateGeneration",
    "AccountPrivateLifecycle",
    "AccountPrivateLifecycleClosed",
    "AccountPrivateLifecyclePort",
    "AccountPrivatePurgeFence",
    "AccountPrivateScopeChanged",
    "LockedAccountPrivateScope",
]
