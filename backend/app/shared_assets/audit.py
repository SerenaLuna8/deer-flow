from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditAuthorityRejected,
    AuditOutcome,
    AuditTarget,
    AuditTargetKind,
    resolve_system_audit_context,
)
from app.audit.service import AuditService
from deerflow.persistence.projects.model import ProjectMembershipRow
from deerflow.persistence.user.model import UserRow

_ACTIONS: dict[str, AuditAction] = {
    "agent.create": AuditAction.ASSET_CREATED,
    "agent.version.create": AuditAction.ASSET_UPDATED,
    "agent.publish": AuditAction.ASSET_PUBLISHED,
    "agent.archive": AuditAction.ASSET_DEPRECATED,
    "agent.suspend": AuditAction.ASSET_DEPRECATED,
    "skill.create": AuditAction.ASSET_CREATED,
    "skill.version.create": AuditAction.ASSET_UPDATED,
    "skill.publish": AuditAction.ASSET_PUBLISHED,
    "skill.delete": AuditAction.ASSET_DELETED,
    "skill.activate": AuditAction.ASSET_UPDATED,
    "skill.suspend": AuditAction.ASSET_DEPRECATED,
    "mcp.create": AuditAction.ASSET_CREATED,
    "mcp.version.create": AuditAction.ASSET_UPDATED,
    "mcp.submit_approval": AuditAction.ASSET_UPDATED,
    "mcp.approve": AuditAction.ASSET_UPDATED,
    "mcp.credential_grants.configure": AuditAction.ASSET_UPDATED,
    "mcp.publish": AuditAction.ASSET_PUBLISHED,
    "mcp.archive": AuditAction.ASSET_DEPRECATED,
    "mcp.suspend": AuditAction.ASSET_DEPRECATED,
    "credential.create": AuditAction.ASSET_CREDENTIAL_CREATED,
    "credential.replace": AuditAction.ASSET_CREDENTIAL_REPLACED,
    "credential.revoke": AuditAction.ASSET_CREDENTIAL_REVOKED,
    "credential.grants.migrate": AuditAction.ASSET_CREDENTIAL_GRANTS_MIGRATED,
    "binding.enable": AuditAction.ASSET_BOUND,
    "binding.upgrade": AuditAction.ASSET_BOUND,
    "binding.rollback": AuditAction.ASSET_BOUND,
    "binding.disable": AuditAction.ASSET_UNBOUND,
}


class DurableSharedAssetGovernanceEventSink:
    """M3-compatible adapter into the formal append-only M6 audit ledger."""

    def __init__(self, service: AuditService) -> None:
        if type(service) is not AuditService:
            raise TypeError("AuditService is required")
        self._service = service

    async def append_override(
        self,
        session: AsyncSession,
        *,
        actor: uuid.UUID,
        project_id: uuid.UUID | None,
        asset_id: uuid.UUID,
        version_id: uuid.UUID | None,
        action: str,
        request_id: str,
        asset_kind: str | None = None,
    ) -> None:
        del version_id
        selected_action = _ACTIONS.get(action)
        selected_kind = asset_kind or action.partition(".")[0]
        if selected_action is None or selected_kind not in {"agent", "skill", "mcp"}:
            raise TypeError("shared asset audit event is invalid")
        system_role = await session.scalar(
            select(UserRow.system_role).where(
                UserRow.id == str(actor),
            )
        )
        if system_role != "system_admin":
            raise AuditAuthorityRejected()
        context = resolve_system_audit_context(
            SimpleNamespace(
                id=uuid.UUID(str(actor)),
                system_role="system_admin",
            ),
            request_id=request_id,
        )
        await self._append(
            session,
            actor=AuditActor.system_admin(context),
            project_id=project_id,
            asset_id=asset_id,
            action=selected_action,
            request_id=request_id,
            asset_kind=selected_kind,
        )

    async def append_project(
        self,
        session: AsyncSession,
        *,
        actor: uuid.UUID,
        project_id: uuid.UUID,
        asset_id: uuid.UUID,
        version_id: uuid.UUID | None,
        action: str,
        request_id: str,
        asset_kind: str | None = None,
    ) -> None:
        del version_id
        selected_action = _ACTIONS.get(action)
        selected_kind = asset_kind or action.partition(".")[0]
        if selected_action is None or selected_kind not in {"agent", "skill", "mcp"}:
            raise TypeError("shared asset audit event is invalid")
        membership_id = await session.scalar(
            select(ProjectMembershipRow.id).where(
                ProjectMembershipRow.project_id == project_id,
                ProjectMembershipRow.user_id == str(actor),
                ProjectMembershipRow.status == "active",
            )
        )
        if membership_id is None:
            raise AuditAuthorityRejected()
        await self._append(
            session,
            actor=AuditActor.user(uuid.UUID(str(actor))),
            project_id=project_id,
            asset_id=asset_id,
            action=selected_action,
            request_id=request_id,
            asset_kind=selected_kind,
        )

    async def _append(
        self,
        session: AsyncSession,
        *,
        actor: AuditActor,
        project_id: uuid.UUID | None,
        asset_id: uuid.UUID,
        action: AuditAction,
        request_id: str,
        asset_kind: str,
    ) -> None:
        await self._service.append(
            session,
            actor,
            action,
            AuditTarget(
                AuditTargetKind.ASSET,
                uuid.UUID(str(asset_id)),
                None if project_id is None else uuid.UUID(str(project_id)),
            ),
            AuditOutcome.SUCCESS,
            {"asset_kind": asset_kind},
            request_id=request_id,
        )


__all__ = ["DurableSharedAssetGovernanceEventSink"]
