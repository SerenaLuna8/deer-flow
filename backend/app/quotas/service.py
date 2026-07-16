from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import (
    PrivateWorkContext,
    is_issued_private_work_context,
)
from app.projects.models import ProjectRole
from app.quotas.models import (
    QUOTA_DIMENSIONS,
    EffectiveQuotaLimits,
    ProjectQuotaLimits,
    ProjectQuotaPolicy,
    QuotaCompensationAuthority,
    QuotaConflict,
    QuotaDimension,
    QuotaExceeded,
    QuotaForbidden,
    QuotaMutation,
    QuotaPolicyInvalid,
    QuotaSourceRef,
    _is_issued_quota_compensation_authority,
)
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.quotas.model import ProjectQuotaRow, ProjectUsageCounterRow
from deerflow.persistence.quotas.sql import QuotaRepository
from deerflow.runtime.private_scope import PrivateResourceScope

QuotaOperation = Literal["reserve", "release", "consume"]
QuotaSourceRefHasher = Callable[[bytes], QuotaSourceRef]
_IDEMPOTENCY_DOMAIN = b"deerflow.m6.quota-idempotency.v1\x00"
_SOURCE_DOMAIN = b"deerflow.m6.quota-source-ref.v1\x00"


class QuotaService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: QuotaConfig,
        *,
        source_ref_hasher: QuotaSourceRefHasher,
    ) -> None:
        if type(config) is not QuotaConfig or not (callable(source_ref_hasher) or callable(getattr(source_ref_hasher, "quota_source_ref", None))):
            raise TypeError("quota service configuration is invalid")
        self._sessions = session_factory
        self._config = config
        self._source_ref_hasher = source_ref_hasher

    @staticmethod
    def _dimension(value: str) -> QuotaDimension:
        if value not in QUOTA_DIMENSIONS:
            raise QuotaPolicyInvalid("quota dimension is invalid")
        return value  # type: ignore[return-value]

    @staticmethod
    def _project_id(value: object) -> uuid.UUID:
        try:
            return uuid.UUID(str(value))
        except (AttributeError, TypeError, ValueError):
            raise QuotaPolicyInvalid("project quota scope is invalid") from None

    @classmethod
    def _scope_coordinates(
        cls,
        scope: PrivateResourceScope,
    ) -> tuple[uuid.UUID, str]:
        if type(scope) is not PrivateResourceScope:
            raise QuotaForbidden("issued quota authority is required")
        try:
            if type(scope.membership_version) is not int or scope.membership_version < 1:
                raise ValueError
            return cls._project_id(scope.project_id), str(uuid.UUID(scope.owner_user_id))
        except (AttributeError, TypeError, ValueError):
            raise QuotaForbidden("issued quota authority is required") from None

    @staticmethod
    async def _lock_active_authority(
        session: AsyncSession,
        context: PrivateWorkContext,
    ) -> tuple[ProjectRow, ProjectMembershipRow]:
        if not is_issued_private_work_context(context):
            raise QuotaForbidden("issued quota authority is required") from None
        project = (await session.execute(select(ProjectRow).where(ProjectRow.id == context.project_id).with_for_update(of=ProjectRow))).scalar_one_or_none()
        membership = (
            await session.execute(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.id == context.membership_id,
                    ProjectMembershipRow.project_id == context.project_id,
                    ProjectMembershipRow.user_id == str(context.user_id),
                )
                .with_for_update(of=ProjectMembershipRow)
            )
        ).scalar_one_or_none()
        if project is None or project.status != "active" or project.is_suspended or membership is None or membership.status != "active" or membership.version != context.membership_version:
            raise QuotaForbidden("current project quota authority is required")
        return project, membership

    @classmethod
    async def _lock_compensation_authority(
        cls,
        session: AsyncSession,
        authority: QuotaCompensationAuthority,
    ) -> tuple[uuid.UUID, str]:
        if not _is_issued_quota_compensation_authority(authority):
            raise QuotaForbidden("trusted quota compensation authority is required")
        project_id, owner_user_id = cls._scope_coordinates(authority.scope)
        project = (await session.execute(select(ProjectRow).where(ProjectRow.id == project_id).with_for_update(of=ProjectRow))).scalar_one_or_none()
        membership = (
            await session.execute(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == project_id,
                    ProjectMembershipRow.user_id == owner_user_id,
                )
                .with_for_update(of=ProjectMembershipRow)
            )
        ).scalar_one_or_none()
        if project is None or membership is None or membership.version < authority.scope.membership_version:
            raise QuotaForbidden("trusted quota compensation authority is required")
        return project_id, owner_user_id

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        selected = value or datetime.now(UTC)
        if selected.tzinfo is None or selected.utcoffset() is None:
            raise QuotaPolicyInvalid("quota time must be timezone aware")
        return selected.astimezone(UTC)

    @classmethod
    def bucket_for(
        cls,
        dimension: str,
        *,
        now: datetime | None = None,
    ) -> str:
        selected = cls._dimension(dimension)
        if selected == "mcp_calls_daily":
            return cls._now(now).date().isoformat()
        return "lifetime"

    def _default_limit(self, dimension: QuotaDimension) -> int:
        return {
            "members": self._config.default_member_limit,
            "storage_bytes": self._config.default_storage_bytes_limit,
            "concurrent_runs": self._config.default_concurrent_run_limit,
            "mcp_calls_daily": self._config.default_mcp_calls_daily_limit,
        }[dimension]

    @staticmethod
    def _configured_limit(
        policy: ProjectQuotaRow | None,
        dimension: QuotaDimension,
    ) -> int | None:
        if policy is None:
            return None
        return {
            "members": policy.member_limit,
            "storage_bytes": policy.storage_bytes_limit,
            "concurrent_runs": policy.concurrent_run_limit,
            "mcp_calls_daily": policy.mcp_calls_daily_limit,
        }[dimension]

    async def effective_limit(
        self,
        session: AsyncSession,
        project_id: object,
        dimension: str,
    ) -> int:
        selected = self._dimension(dimension)
        policy = await QuotaRepository(session).policy(self._project_id(project_id))
        default = self._default_limit(selected)
        configured = self._configured_limit(policy, selected)
        return default if configured is None else min(default, configured)

    def _effective_limits(self, configured: ProjectQuotaLimits) -> EffectiveQuotaLimits:
        return EffectiveQuotaLimits(
            member_limit=min(
                self._config.default_member_limit,
                configured.member_limit if configured.member_limit is not None else self._config.default_member_limit,
            ),
            storage_bytes_limit=min(
                self._config.default_storage_bytes_limit,
                configured.storage_bytes_limit if configured.storage_bytes_limit is not None else self._config.default_storage_bytes_limit,
            ),
            concurrent_run_limit=min(
                self._config.default_concurrent_run_limit,
                configured.concurrent_run_limit if configured.concurrent_run_limit is not None else self._config.default_concurrent_run_limit,
            ),
            mcp_calls_daily_limit=min(
                self._config.default_mcp_calls_daily_limit,
                configured.mcp_calls_daily_limit if configured.mcp_calls_daily_limit is not None else self._config.default_mcp_calls_daily_limit,
            ),
        )

    def _validate_limits(self, limits: ProjectQuotaLimits) -> None:
        values = (
            (limits.member_limit, 1, self._config.default_member_limit),
            (limits.storage_bytes_limit, 0, self._config.default_storage_bytes_limit),
            (limits.concurrent_run_limit, 1, self._config.default_concurrent_run_limit),
            (limits.mcp_calls_daily_limit, 0, self._config.default_mcp_calls_daily_limit),
        )
        if any(value is not None and (type(value) is not int or value < minimum or value > maximum) for value, minimum, maximum in values):
            raise QuotaPolicyInvalid("project quota must tighten the platform default")

    async def set_limits(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        limits: ProjectQuotaLimits,
        *,
        expected_version: int,
    ) -> ProjectQuotaPolicy:
        if not is_issued_private_work_context(context):
            raise QuotaForbidden("issued quota authority is required")
        if type(limits) is not ProjectQuotaLimits or type(expected_version) is not int or expected_version < 0:
            raise QuotaPolicyInvalid("project quota policy is invalid")
        self._validate_limits(limits)
        _project, membership = await self._lock_active_authority(session, context)
        if membership.role != ProjectRole.ADMIN.value:
            raise QuotaForbidden("project Admin quota authority is required")

        row = await QuotaRepository(session).policy(context.project_id)
        if row is None:
            if expected_version != 0:
                raise QuotaConflict("project quota version conflict")
            row = ProjectQuotaRow(project_id=context.project_id, version=1)
            session.add(row)
        else:
            if row.version != expected_version:
                raise QuotaConflict("project quota version conflict")
            row.version += 1
        row.member_limit = limits.member_limit
        row.storage_bytes_limit = limits.storage_bytes_limit
        row.concurrent_run_limit = limits.concurrent_run_limit
        row.mcp_calls_daily_limit = limits.mcp_calls_daily_limit
        row.updated_by_user_id = str(context.user_id)
        changed_at = datetime.now(UTC)
        row.updated_at = changed_at
        await session.flush()
        repository = QuotaRepository(session)
        effective = self._effective_limits(limits)
        configured_limits = {
            "members": effective.member_limit,
            "storage_bytes": effective.storage_bytes_limit,
            "concurrent_runs": effective.concurrent_run_limit,
            "mcp_calls_daily": effective.mcp_calls_daily_limit,
        }
        for dimension in QUOTA_DIMENSIONS:
            bucket = self.bucket_for(dimension, now=changed_at)
            counter = await repository.lock_counter(
                context.project_id,
                dimension,
                bucket,
            )
            await self._append_zero_net_threshold(
                session,
                counter,
                owner_user_id=str(context.user_id),
                limit=configured_limits[dimension],
                source_kind="policy_threshold",
                source_key=f"policy:{row.version}",
                occurred_at=changed_at,
            )
        return ProjectQuotaPolicy(
            configured=limits,
            effective=effective,
            version=row.version,
        )

    @staticmethod
    def _idempotency_digest(
        *,
        source_ref: QuotaSourceRef,
    ) -> str:
        return hashlib.sha256(
            _IDEMPOTENCY_DOMAIN + source_ref.key_id.encode("ascii") + b"\x00" + bytes.fromhex(source_ref.hmac_hex),
        ).hexdigest()

    @staticmethod
    def _source_payload(
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        dimension: QuotaDimension,
        bucket: str,
        operation: str,
        key: str,
    ) -> bytes:
        fields = (
            project_id.bytes,
            owner_user_id.encode("ascii"),
            dimension.encode("ascii"),
            bucket.encode("ascii"),
            operation.encode("ascii"),
            key.encode("utf-8"),
        )
        return _SOURCE_DOMAIN + b"".join(len(field).to_bytes(4, "big") + field for field in fields)

    def _source_ref(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        dimension: QuotaDimension,
        bucket: str,
        operation: str,
        key: str,
    ) -> QuotaSourceRef:
        return self._source_refs(
            project_id=project_id,
            owner_user_id=owner_user_id,
            dimension=dimension,
            bucket=bucket,
            operation=operation,
            key=key,
        )[0]

    def _source_refs(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        dimension: QuotaDimension,
        bucket: str,
        operation: str,
        key: str,
    ) -> tuple[QuotaSourceRef, ...]:
        payload = self._source_payload(
            project_id=project_id,
            owner_user_id=owner_user_id,
            dimension=dimension,
            bucket=bucket,
            operation=operation,
            key=key,
        )
        all_refs = getattr(self._source_ref_hasher, "quota_source_refs", None)
        if callable(all_refs):
            values = all_refs(payload)
        else:
            active = self._source_ref_hasher(payload) if callable(self._source_ref_hasher) else self._source_ref_hasher.quota_source_ref(payload)
            values = (active,)
        if type(values) is not tuple or not values or any(type(value) is not QuotaSourceRef for value in values) or len({(value.key_id, value.hmac_hex) for value in values}) != len(values):
            raise TypeError("quota source_ref_hasher returned an invalid value")
        return values

    @staticmethod
    def _threshold_reached(*, usage: int, limit: int, threshold: float) -> bool:
        warning_at = max(1, math.ceil(limit * threshold))
        return usage >= warning_at

    async def _append_zero_net_threshold(
        self,
        session: AsyncSession,
        counter: ProjectUsageCounterRow,
        *,
        owner_user_id: str,
        limit: int,
        source_kind: str,
        source_key: str,
        occurred_at: datetime,
    ) -> bool:
        selected = self._dimension(counter.dimension)
        repository = QuotaRepository(session)
        usage = counter.used + counter.reserved
        if not self._threshold_reached(
            usage=usage,
            limit=limit,
            threshold=self._config.warning_threshold,
        ) or await repository.threshold_recorded(
            counter.project_id,
            selected,
            counter.bucket,
        ):
            return False
        rows = (
            (f"{source_kind}_offset", -usage, f"{source_key}:offset"),
            (source_kind, usage, source_key),
        )
        for kind, delta, key in rows:
            source_ref = self._source_ref(
                project_id=counter.project_id,
                owner_user_id=owner_user_id,
                dimension=selected,
                bucket=counter.bucket,
                operation=kind,
                key=key,
            )
            await repository.append_ledger(
                project_id=counter.project_id,
                dimension=selected,
                delta=delta,
                bucket=counter.bucket,
                source_kind=kind,
                source_ref_key_id=source_ref.key_id,
                source_ref_hmac=source_ref.hmac_hex,
                idempotency_key=self._idempotency_digest(source_ref=source_ref),
                request_id=None,
                occurred_at=occurred_at,
            )
        counter.version += 1
        counter.updated_at = occurred_at
        await session.flush()
        return True

    async def _mutate(
        self,
        session: AsyncSession,
        authority: PrivateWorkContext | QuotaCompensationAuthority,
        dimension: str,
        amount: int,
        key: str,
        *,
        operation: QuotaOperation,
        now: datetime | None,
    ) -> QuotaMutation:
        selected = self._dimension(dimension)
        if type(amount) is not int or amount < 1 or not isinstance(key, str) or not 1 <= len(key) <= 512:
            raise QuotaPolicyInvalid("quota mutation is invalid")
        if (selected == "mcp_calls_daily" and operation != "consume") or (selected != "mcp_calls_daily" and operation == "consume"):
            raise QuotaPolicyInvalid("quota operation is invalid for dimension")
        if operation == "release":
            if type(authority) is not QuotaCompensationAuthority or authority.dimension != selected:
                raise QuotaForbidden("quota compensation dimension is invalid")
            project_id, owner_user_id = await self._lock_compensation_authority(
                session,
                authority,
            )
        else:
            if not is_issued_private_work_context(authority):
                raise QuotaForbidden("issued quota authority is required")
            await self._lock_active_authority(session, authority)
            project_id = authority.project_id
            owner_user_id = str(authority.user_id)
        occurred_at = self._now(now)
        bucket = self.bucket_for(selected, now=occurred_at)
        repository = QuotaRepository(session)
        counter = await repository.lock_counter(project_id, selected, bucket)
        limit = await self.effective_limit(session, project_id, selected)
        source_refs = self._source_refs(
            project_id=project_id,
            owner_user_id=owner_user_id,
            dimension=selected,
            bucket=bucket,
            operation=operation,
            key=key,
        )
        source_ref = source_refs[0]
        existing_matches = []
        for candidate in source_refs:
            existing = await repository.ledger_entry(
                project_id,
                selected,
                self._idempotency_digest(source_ref=candidate),
            )
            if existing is not None:
                existing_matches.append((candidate, existing))
        if len(existing_matches) > 1:
            raise QuotaConflict("quota idempotency authority conflict")
        expected_delta = -amount if operation == "release" else amount
        if existing_matches:
            existing_ref, existing = existing_matches[0]
            valid_kinds = {operation, f"{operation}_threshold"}
            if existing.bucket != bucket or existing.delta != expected_delta or existing.source_kind not in valid_kinds or existing.source_ref_key_id != existing_ref.key_id or existing.source_ref_hmac != existing_ref.hmac_hex:
                raise QuotaConflict("quota idempotency authority conflict")
            return QuotaMutation(
                dimension=selected,
                bucket=bucket,
                used=counter.used,
                reserved=counter.reserved,
                limit=limit,
                threshold_crossed=existing.source_kind.endswith("_threshold"),
                created=False,
            )

        before = counter.used + counter.reserved
        if operation == "release":
            reserve_refs = self._source_refs(
                project_id=project_id,
                owner_user_id=owner_user_id,
                dimension=selected,
                bucket=bucket,
                operation="reserve",
                key=key,
            )
            reservation_matches = []
            for candidate in reserve_refs:
                reservation = await repository.ledger_entry(
                    project_id,
                    selected,
                    self._idempotency_digest(source_ref=candidate),
                )
                if reservation is not None:
                    reservation_matches.append((candidate, reservation))
            if len(reservation_matches) != 1:
                raise QuotaConflict("quota release requires its exact reservation")
            reserve_ref, reservation = reservation_matches[0]
            if (
                reservation is None
                or reservation.bucket != bucket
                or reservation.delta != amount
                or reservation.source_kind not in {"reserve", "reserve_threshold"}
                or reservation.source_ref_key_id != reserve_ref.key_id
                or reservation.source_ref_hmac != reserve_ref.hmac_hex
            ):
                raise QuotaConflict("quota release requires its exact reservation")
            if counter.reserved < amount:
                raise QuotaConflict("quota release exceeds reservation")
            counter.reserved -= amount
        else:
            if before + amount > limit:
                raise QuotaExceeded(selected, limit)
            if operation == "reserve":
                counter.reserved += amount
            else:
                counter.used += amount
        after = counter.used + counter.reserved
        warning_at = math.ceil(limit * self._config.warning_threshold)
        threshold_crossed = operation != "release" and warning_at > 0 and before < warning_at <= after and not await repository.threshold_recorded(project_id, selected, bucket)
        await repository.append_ledger(
            project_id=project_id,
            dimension=selected,
            delta=expected_delta,
            bucket=bucket,
            source_kind=(f"{operation}_threshold" if threshold_crossed else operation),
            source_ref_key_id=source_ref.key_id,
            source_ref_hmac=source_ref.hmac_hex,
            idempotency_key=self._idempotency_digest(source_ref=source_ref),
            request_id=None,
            occurred_at=occurred_at,
        )
        counter.version += 1
        counter.updated_at = occurred_at
        await session.flush()
        return QuotaMutation(
            dimension=selected,
            bucket=bucket,
            used=counter.used,
            reserved=counter.reserved,
            limit=limit,
            threshold_crossed=threshold_crossed,
            created=True,
        )

    async def reserve(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        dimension: str,
        amount: int,
        idempotency_key: str,
        *,
        now: datetime | None = None,
    ) -> QuotaMutation:
        return await self._mutate(
            session,
            context,
            dimension,
            amount,
            idempotency_key,
            operation="reserve",
            now=now,
        )

    async def release(
        self,
        session: AsyncSession,
        authority: QuotaCompensationAuthority,
        dimension: str,
        amount: int,
        idempotency_key: str,
        *,
        now: datetime | None = None,
    ) -> QuotaMutation:
        return await self._mutate(
            session,
            authority,
            dimension,
            amount,
            idempotency_key,
            operation="release",
            now=now,
        )

    async def consume(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        dimension: str,
        amount: int,
        idempotency_key: str,
        *,
        now: datetime | None = None,
    ) -> QuotaMutation:
        return await self._mutate(
            session,
            context,
            dimension,
            amount,
            idempotency_key,
            operation="consume",
            now=now,
        )

    async def reserve_new_session(
        self,
        context: PrivateWorkContext,
        dimension: str,
        amount: int,
        idempotency_key: str,
        *,
        now: datetime | None = None,
    ) -> QuotaMutation:
        async with self._sessions() as session, session.begin():
            return await self.reserve(
                session,
                context,
                dimension,
                amount,
                idempotency_key,
                now=now,
            )

    async def release_new_session(
        self,
        authority: QuotaCompensationAuthority,
        dimension: str,
        amount: int,
        idempotency_key: str,
        *,
        now: datetime | None = None,
    ) -> QuotaMutation:
        async with self._sessions() as session, session.begin():
            return await self.release(
                session,
                authority,
                dimension,
                amount,
                idempotency_key,
                now=now,
            )

    async def consume_new_session(
        self,
        context: PrivateWorkContext,
        dimension: str,
        amount: int,
        idempotency_key: str,
        *,
        now: datetime | None = None,
    ) -> QuotaMutation:
        async with self._sessions() as session, session.begin():
            return await self.consume(
                session,
                context,
                dimension,
                amount,
                idempotency_key,
                now=now,
            )

    async def _reconcile_locked(
        self,
        session: AsyncSession,
        counter: ProjectUsageCounterRow,
        *,
        expected: int,
        now: datetime,
    ) -> tuple[int, int] | None:
        selected = self._dimension(counter.dimension)
        if type(expected) is not int or expected < 0:
            raise QuotaPolicyInvalid("quota reconciliation value is invalid")
        current = counter.used + counter.reserved
        if current == expected and ((selected == "mcp_calls_daily" and counter.reserved == 0) or (selected != "mcp_calls_daily" and counter.used == 0)):
            return None
        delta = expected - current
        source_base = f"reconcile:{selected}:{counter.bucket}:{counter.version}:{current}:{expected}"
        repository = QuotaRepository(session)
        limit = await self.effective_limit(
            session,
            counter.project_id,
            selected,
        )
        threshold_missing = self._threshold_reached(
            usage=expected,
            limit=limit,
            threshold=self._config.warning_threshold,
        ) and not await repository.threshold_recorded(
            counter.project_id,
            selected,
            counter.bucket,
        )
        adjustments = (
            (
                (
                    "reconcile_threshold" if threshold_missing else "reconcile_adjustment",
                    delta,
                    source_base,
                ),
            )
            if delta != 0
            else (
                ("reconcile_axis_debit", -expected, f"{source_base}:debit"),
                (
                    "reconcile_threshold" if threshold_missing else "reconcile_axis_credit",
                    expected,
                    f"{source_base}:credit",
                ),
            )
        )
        for source_kind, adjustment, source_key in adjustments:
            source_ref = self._source_ref(
                project_id=counter.project_id,
                owner_user_id="trusted:quota_reconciliation",
                dimension=selected,
                bucket=counter.bucket,
                operation=source_kind,
                key=source_key,
            )
            await repository.append_ledger(
                project_id=counter.project_id,
                dimension=selected,
                delta=adjustment,
                bucket=counter.bucket,
                source_kind=source_kind,
                source_ref_key_id=source_ref.key_id,
                source_ref_hmac=source_ref.hmac_hex,
                idempotency_key=self._idempotency_digest(source_ref=source_ref),
                request_id=None,
                occurred_at=now,
            )
        if selected == "mcp_calls_daily":
            counter.used = expected
            counter.reserved = 0
        else:
            counter.used = 0
            counter.reserved = expected
        counter.version += 1
        counter.updated_at = now
        await session.flush()
        return current, expected


__all__ = ["QuotaService", "QuotaSourceRefHasher"]
