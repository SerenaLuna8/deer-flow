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
from deerflow.persistence.shared_assets.agent_model import AgentVersionRow
from deerflow.persistence.shared_assets.skill_model import SkillVersionRow
from deerflow.persistence.user.model import UserRow

_ACTIONS: dict[str, AuditAction] = {
    "agent.create": AuditAction.ASSET_CREATED,
    "agent.version.create": AuditAction.ASSET_UPDATED,
    "agent.instructions.update": AuditAction.ASSET_UPDATED,
    "agent.capability_bindings.update": AuditAction.ASSET_UPDATED,
    "agent.version.activate": AuditAction.ASSET_UPDATED,
    "agent.delete": AuditAction.ASSET_DELETED,
    "agent.enable": AuditAction.ASSET_UPDATED,
    "agent.suspend": AuditAction.ASSET_DEPRECATED,
    "agent.default.set": AuditAction.ASSET_BOUND,
    "agent.default.clear": AuditAction.ASSET_UNBOUND,
    "skill.create": AuditAction.ASSET_CREATED,
    "skill.version.create": AuditAction.ASSET_UPDATED,
    "skill.version.activate": AuditAction.ASSET_UPDATED,
    "skill.export": AuditAction.ASSET_EXPORTED,
    "skill.version.revoke": AuditAction.ASSET_DEPRECATED,
    "skill.delete": AuditAction.ASSET_DELETED,
    "skill.enable": AuditAction.ASSET_UPDATED,
    "skill.credential_bindings.configure": AuditAction.ASSET_UPDATED,
    "skill.suspend": AuditAction.ASSET_DEPRECATED,
    "mcp.create": AuditAction.ASSET_CREATED,
    "mcp.version.create": AuditAction.ASSET_UPDATED,
    "mcp.submit_approval": AuditAction.ASSET_UPDATED,
    "mcp.approve": AuditAction.ASSET_UPDATED,
    "mcp.credential_grants.configure": AuditAction.ASSET_UPDATED,
    "mcp.publish": AuditAction.ASSET_PUBLISHED,
    "mcp.archive": AuditAction.ASSET_DEPRECATED,
    "mcp.suspend": AuditAction.ASSET_DEPRECATED,
    "mcp.activate": AuditAction.ASSET_UPDATED,
    "mcp.delete": AuditAction.ASSET_DELETED,
    "credential.create": AuditAction.ASSET_CREDENTIAL_CREATED,
    "credential.replace": AuditAction.ASSET_CREDENTIAL_REPLACED,
    "credential.revoke": AuditAction.ASSET_CREDENTIAL_REVOKED,
    "credential.delete": AuditAction.ASSET_CREDENTIAL_DELETED,
    "credential.grants.migrate": AuditAction.ASSET_CREDENTIAL_GRANTS_MIGRATED,
    "binding.enable": AuditAction.ASSET_BOUND,
    "binding.upgrade": AuditAction.ASSET_BOUND,
    "binding.rollback": AuditAction.ASSET_BOUND,
    "binding.sync_current": AuditAction.ASSET_BOUND,
    "binding.disable": AuditAction.ASSET_UNBOUND,
}

_VERSIONED_AGENT_OPERATIONS = frozenset(
    {
        "agent.version.create",
        "agent.instructions.update",
        "agent.capability_bindings.update",
        "agent.version.activate",
    }
)
_VERSIONED_SKILL_OPERATIONS = frozenset(
    {
        "skill.version.create",
        "skill.version.activate",
        "skill.export",
        "skill.version.revoke",
    }
)


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
        selected_action, selected_kind = self._select_event(action, asset_kind)
        system_role = await session.scalar(
            select(UserRow.system_role).where(
                UserRow.id == str(actor),
            )
        )
        if system_role != "system_admin":
            raise AuditAuthorityRejected()
        version_number = await self._safe_version_number(
            session,
            asset_id=asset_id,
            version_id=version_id,
            operation=action,
            asset_kind=selected_kind,
        )
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
            operation=action,
            version_number=version_number,
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
        selected_action, selected_kind = self._select_event(action, asset_kind)
        membership_id = await session.scalar(
            select(ProjectMembershipRow.id).where(
                ProjectMembershipRow.project_id == project_id,
                ProjectMembershipRow.user_id == str(actor),
                ProjectMembershipRow.status == "active",
            )
        )
        if membership_id is None:
            raise AuditAuthorityRejected()
        version_number = await self._safe_version_number(
            session,
            asset_id=asset_id,
            version_id=version_id,
            operation=action,
            asset_kind=selected_kind,
        )
        await self._append(
            session,
            actor=AuditActor.user(uuid.UUID(str(actor))),
            project_id=project_id,
            asset_id=asset_id,
            action=selected_action,
            request_id=request_id,
            asset_kind=selected_kind,
            operation=action,
            version_number=version_number,
        )

    @staticmethod
    def _select_event(
        operation: str,
        asset_kind: str | None,
    ) -> tuple[AuditAction, str]:
        selected_action = _ACTIONS.get(operation)
        operation_domain = operation.partition(".")[0]
        selected_kind = asset_kind or operation_domain
        kind_mismatch = operation_domain in {"agent", "skill", "mcp"} and selected_kind != operation_domain
        if selected_action is None or selected_kind not in {"agent", "skill", "mcp"} or kind_mismatch:
            raise TypeError("shared asset audit event is invalid")
        return selected_action, selected_kind

    @staticmethod
    async def _safe_version_number(
        session: AsyncSession,
        *,
        asset_id: uuid.UUID,
        version_id: uuid.UUID | None,
        operation: str,
        asset_kind: str,
    ) -> int | None:
        if asset_kind == "agent" and operation.startswith("agent."):
            expects_version = operation in _VERSIONED_AGENT_OPERATIONS
            if expects_version != (version_id is not None):
                raise AuditAuthorityRejected()
            if version_id is None:
                return None
            version_number = await session.scalar(
                select(AgentVersionRow.version_number).where(
                    AgentVersionRow.agent_id == asset_id,
                    AgentVersionRow.id == version_id,
                )
            )
        elif asset_kind == "skill" and operation in _VERSIONED_SKILL_OPERATIONS:
            if version_id is None:
                raise AuditAuthorityRejected()
            version_number = await session.scalar(
                select(SkillVersionRow.version_number).where(
                    SkillVersionRow.skill_id == asset_id,
                    SkillVersionRow.id == version_id,
                )
            )
        else:
            return None
        if type(version_number) is not int or version_number < 1:
            raise AuditAuthorityRejected()
        return version_number

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
        operation: str,
        version_number: int | None,
    ) -> None:
        metadata: dict[str, object] = {
            "asset_kind": asset_kind,
            "operation": operation,
        }
        if version_number is not None:
            metadata["version_number"] = version_number
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
            metadata,
            request_id=request_id,
        )


__all__ = ["DurableSharedAssetGovernanceEventSink"]
