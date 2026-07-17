from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import (
    AUDIT_ACTION_CONTRACTS,
    AUDIT_METADATA_MODELS,
    AuditAction,
    AuditActor,
    AuditAuthorityRejected,
    AuditCursorRejected,
    AuditMetadataRejected,
    AuditOutcome,
    AuditPage,
    AuditPlatformRole,
    AuditProcess,
    AuditProcessContext,
    AuditRecord,
    AuditScope,
    AuditTarget,
    AuditTargetKind,
    AuditUnavailable,
    SystemAuditContext,
    _AuditProcessRegistry,
    is_issued_elevated_audit_actor,
    is_issued_system_audit_context,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.audit.sql import AuditRepository

_PUBLIC_ERROR = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class AuditService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        keyring: AuditHmacKeyring,
    ) -> None:
        if session_factory is not None and not callable(session_factory):
            raise TypeError("audit session factory is invalid")
        if type(keyring) is not AuditHmacKeyring:
            raise TypeError("audit HMAC keyring is invalid")
        self._sessions = session_factory
        self._keyring = keyring
        self.__process_registry = _AuditProcessRegistry()

    def require_process_context(
        self,
        context: object,
    ) -> AuditProcessContext:
        if not self.__process_registry.owns(context):
            raise AuditAuthorityRejected()
        return context

    async def append(
        self,
        session: AsyncSession,
        actor: AuditActor,
        action: AuditAction,
        target: AuditTarget,
        outcome: AuditOutcome,
        metadata: object,
        *,
        public_error_code: str | None = None,
        request_id: str | None = None,
        job_id: uuid.UUID | None = None,
        attempt_id: uuid.UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditRecord:
        if not self._valid_actor(actor) or not self._valid_target(target):
            raise AuditAuthorityRejected()
        if type(action) is not AuditAction or type(outcome) is not AuditOutcome:
            raise AuditMetadataRejected()
        try:
            if public_error_code is not None and (type(public_error_code) is not str or _PUBLIC_ERROR.fullmatch(public_error_code) is None):
                raise ValueError
            if job_id is not None and type(job_id) is not uuid.UUID:
                raise ValueError
            if attempt_id is not None and type(attempt_id) is not uuid.UUID:
                raise ValueError
            selected_time = occurred_at or datetime.now(UTC)
            if type(selected_time) is not datetime or selected_time.tzinfo is None or selected_time.utcoffset() is None:
                raise ValueError
            sanitized = (
                AUDIT_METADATA_MODELS[action]
                .model_validate(metadata)
                .model_dump(
                    mode="json",
                    exclude_none=True,
                )
            )
            request_ref = self._keyring.audit_request_ref(request_id).hmac_hex if request_id is not None else None
        except (KeyError, TypeError, ValueError, ValidationError):
            raise AuditMetadataRejected() from None
        if not self._action_authorized(actor, action, target, sanitized):
            raise AuditAuthorityRejected()
        target_ref = self._keyring.audit_target_ref(
            target.kind.value,
            target.authority_id,
        )

        row = await AuditRepository(session).append(
            occurred_at=selected_time.astimezone(UTC),
            actor_user_id=str(actor.user_id) if actor.user_id is not None else None,
            actor_process=actor.process.value if actor.process is not None else None,
            actor_platform_role=actor.platform_role.value if actor.platform_role is not None else None,
            project_id=target.project_id,
            action=action.value,
            target_kind=target.kind.value,
            target_ref_key_id=target_ref.key_id,
            target_ref_hmac=target_ref.hmac_hex,
            outcome=outcome.value,
            public_error_code=public_error_code,
            request_id=request_ref,
            job_id=job_id,
            attempt_id=attempt_id,
            metadata_json=sanitized,
        )
        return self._record(row)

    def _valid_actor(self, value: object) -> bool:
        try:
            if type(value) is not AuditActor:
                return False
            valid = (
                (value.user_id is None or type(value.user_id) is uuid.UUID)
                and (value.process is None or type(value.process) is AuditProcess)
                and (value.platform_role is None or type(value.platform_role) is AuditPlatformRole)
                and ((value.user_id is None) != (value.process is None))
                and not (value.process is not None and value.platform_role is not None)
            )
            if not valid:
                return False
            if value.process is not None:
                return is_issued_elevated_audit_actor(
                    value,
                    process_issuer_id=self.__process_registry.issuer_id,
                )
            if value.platform_role is not None:
                return is_issued_elevated_audit_actor(value)
            return True
        except AttributeError:
            return False

    @staticmethod
    def _valid_target(value: object) -> bool:
        try:
            return type(value) is AuditTarget and type(value.kind) is AuditTargetKind and type(value.authority_id) is uuid.UUID and (value.project_id is None or type(value.project_id) is uuid.UUID)
        except AttributeError:
            return False

    @staticmethod
    def _action_authorized(
        actor: AuditActor,
        action: AuditAction,
        target: AuditTarget,
        metadata: dict[str, object],
    ) -> bool:
        contract = AUDIT_ACTION_CONTRACTS[action]
        if target.kind is not contract.target_kind:
            return False
        if contract.authority_matches_project and target.authority_id != target.project_id:
            return False
        if actor.platform_role is not None:
            actor_kind = "system"
        elif actor.process is not None:
            actor_kind = "process"
        else:
            actor_kind = "user"
        for variant in contract.variants:
            if variant.actor != actor_kind:
                continue
            if variant.scope is AuditScope.PROJECT and target.project_id is None:
                continue
            if variant.scope is AuditScope.PLATFORM and target.project_id is not None:
                continue
            if actor.process is not None and actor.process not in variant.processes:
                continue
            if any(metadata.get(key) != expected for key, expected in variant.metadata_equals):
                continue
            return True
        return False

    async def append_new_session(self, *args, **kwargs) -> AuditRecord:
        if self._sessions is None:
            raise AuditUnavailable()
        try:
            async with self._sessions.begin() as session:
                return await self.append(session, *args, **kwargs)
        except DBAPIError:
            raise AuditUnavailable() from None

    async def list_project_new_session(
        self,
        context: ProjectContext,
        *,
        limit: int = 50,
        cursor: str | None = None,
        action: AuditAction | None = None,
        outcome: AuditOutcome | None = None,
        target: AuditTarget | None = None,
    ) -> AuditPage:
        if type(context) is not ProjectContext:
            raise AuditAuthorityRejected()
        context.require(Capability.PROJECT_AUDIT_READ)
        if target is not None and (type(target) is not AuditTarget or target.project_id != context.project_id):
            raise AuditAuthorityRejected()
        return await self._list_new_session(
            project_id=context.project_id,
            limit=limit,
            cursor=cursor,
            action=action,
            outcome=outcome,
            target=target,
        )

    async def list_platform_new_session(
        self,
        context: SystemAuditContext,
        *,
        limit: int = 50,
        cursor: str | None = None,
        action: AuditAction | None = None,
        outcome: AuditOutcome | None = None,
        target: AuditTarget | None = None,
    ) -> AuditPage:
        if not is_issued_system_audit_context(context):
            raise TypeError("issued system audit context is required")
        return await self._list_new_session(
            project_id=None,
            limit=limit,
            cursor=cursor,
            action=action,
            outcome=outcome,
            target=target,
        )

    async def _list_new_session(
        self,
        *,
        project_id: uuid.UUID | None,
        limit: int,
        cursor: str | None,
        action: AuditAction | None,
        outcome: AuditOutcome | None,
        target: AuditTarget | None,
    ) -> AuditPage:
        if self._sessions is None:
            raise AuditUnavailable()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AuditCursorRejected()
        if action is not None and type(action) is not AuditAction:
            raise AuditMetadataRejected()
        if outcome is not None and type(outcome) is not AuditOutcome:
            raise AuditMetadataRejected()
        if target is not None and type(target) is not AuditTarget:
            raise AuditAuthorityRejected()
        selected_cursor = None if cursor is None else self._decode_cursor(cursor)
        target_refs = None
        if target is not None:
            target_refs = tuple(
                (ref.key_id, ref.hmac_hex)
                for ref in self._keyring.audit_target_refs(
                    target.kind.value,
                    target.authority_id,
                )
            )
        try:
            async with self._sessions() as session:
                repository = AuditRepository(session)
                if project_id is None:
                    rows = await repository.list_platform(
                        limit=limit + 1,
                        cursor=selected_cursor,
                        action=action.value if action is not None else None,
                        outcome=outcome.value if outcome is not None else None,
                        target_refs=target_refs,
                    )
                else:
                    rows = await repository.list_project(
                        project_id,
                        limit=limit + 1,
                        cursor=selected_cursor,
                        action=action.value if action is not None else None,
                        outcome=outcome.value if outcome is not None else None,
                        target_refs=target_refs,
                    )
        except DBAPIError:
            raise AuditUnavailable() from None
        page_rows = rows[:limit]
        next_cursor = self._encode_cursor(page_rows[-1]) if len(rows) > limit else None
        return AuditPage(
            items=tuple(self._record(row) for row in page_rows),
            next_cursor=next_cursor,
        )

    @staticmethod
    def _record(row: AuditLogRow) -> AuditRecord:
        try:
            return AuditRecord(
                id=row.id,
                occurred_at=row.occurred_at,
                actor_user_id=uuid.UUID(row.actor_user_id) if row.actor_user_id is not None else None,
                actor_process=AuditProcess(row.actor_process) if row.actor_process is not None else None,
                actor_platform_role=AuditPlatformRole(row.actor_platform_role) if row.actor_platform_role is not None else None,
                project_id=row.project_id,
                action=AuditAction(row.action),
                target_kind=AuditTargetKind(row.target_kind),
                outcome=AuditOutcome(row.outcome),
                public_error_code=row.public_error_code,
                request_id=row.request_id,
                job_id=row.job_id,
                attempt_id=row.attempt_id,
                metadata=dict(row.metadata_json),
            )
        except (TypeError, ValueError):
            raise AuditUnavailable() from None

    @staticmethod
    def _encode_cursor(row: AuditLogRow) -> str:
        payload = {
            "v": 1,
            "t": row.occurred_at.isoformat(),
            "i": str(row.id),
        }
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii")).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(value: str) -> tuple[datetime, uuid.UUID]:
        try:
            if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
                raise ValueError
            raw = base64.b64decode(
                value + "=" * (-len(value) % 4),
                altchars=b"-_",
                validate=True,
            )
            if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value:
                raise ValueError
            payload = json.loads(raw)
            if type(payload) is not dict or set(payload) != {"v", "t", "i"} or payload["v"] != 1:
                raise ValueError
            occurred_at = datetime.fromisoformat(payload["t"])
            if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                raise ValueError
            return occurred_at.astimezone(UTC), uuid.UUID(payload["i"])
        except (
            binascii.Error,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            raise AuditCursorRejected() from None


def _bind_gateway_audit_process(service: AuditService) -> AuditProcessContext:
    return service._AuditService__process_registry.bind(AuditProcess.GATEWAY)


def _bind_worker_audit_process(service: AuditService) -> AuditProcessContext:
    return service._AuditService__process_registry.bind(AuditProcess.WORKER)


def _bind_scheduler_audit_process(service: AuditService) -> AuditProcessContext:
    return service._AuditService__process_registry.bind(AuditProcess.SCHEDULER)


def _bind_operator_audit_process(service: AuditService) -> AuditProcessContext:
    return service._AuditService__process_registry.bind(AuditProcess.OPERATOR)


def _bind_recovery_audit_process(service: AuditService) -> AuditProcessContext:
    return service._AuditService__process_registry.bind(AuditProcess.RECOVERY)


__all__ = ["AuditService"]
