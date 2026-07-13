from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared_assets.models import AssetScope
from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialGrantRow,
    CredentialRow,
    CredentialVersionRow,
    McpCredentialSlotRow,
)


class McpCredentialClosureInvalid(Exception):
    pass


@dataclass(frozen=True)
class LockedMcpCredentialClosure:
    slots: tuple[McpCredentialSlotRow, ...]
    grants: tuple[CredentialGrantRow, ...]

    @property
    def grant_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(uuid.UUID(str(grant.id)) for grant in self.grants)


def _normalized_schema(value: object) -> dict[str, list[object]] | None:
    if not isinstance(value, dict):
        return None
    try:
        normalized: dict[str, list[object]] = {}
        for key, items in value.items():
            if not isinstance(key, str) or not isinstance(items, list):
                return None
            normalized[key] = sorted(items)
        return normalized
    except TypeError:
        return None


async def lock_mcp_credential_closure(
    session: AsyncSession,
    mcp_version_id: uuid.UUID,
    *,
    scope: AssetScope,
    project_id: uuid.UUID | None,
) -> LockedMcpCredentialClosure:
    """Lock and validate one published MCP credential closure in global order."""

    version_id = uuid.UUID(str(mcp_version_id))
    slots = tuple(
        (await session.execute(select(McpCredentialSlotRow).where(McpCredentialSlotRow.mcp_server_version_id == version_id).order_by(McpCredentialSlotRow.name, McpCredentialSlotRow.id).with_for_update(read=True, of=McpCredentialSlotRow)))
        .scalars()
        .all()
    )
    references = tuple(
        (
            await session.execute(
                select(
                    CredentialGrantRow.id,
                    CredentialGrantRow.credential_slot_id,
                    CredentialGrantRow.credential_version_id,
                    CredentialVersionRow.credential_id,
                )
                .join(
                    CredentialVersionRow,
                    CredentialVersionRow.id == CredentialGrantRow.credential_version_id,
                )
                .where(
                    CredentialGrantRow.mcp_server_version_id == version_id,
                    CredentialGrantRow.status == "active",
                )
                .order_by(CredentialGrantRow.id)
            )
        ).all()
    )
    credential_ids = sorted(
        {uuid.UUID(str(row.credential_id)) for row in references},
        key=lambda value: value.int,
    )
    credentials: dict[uuid.UUID, CredentialRow] = {}
    for credential_id in credential_ids:
        credential = (await session.execute(select(CredentialRow).where(CredentialRow.id == credential_id).with_for_update(read=True, of=CredentialRow))).scalar_one_or_none()
        if credential is None:
            raise McpCredentialClosureInvalid
        credentials[credential_id] = credential

    ordered_versions = sorted(
        {
            (
                uuid.UUID(str(row.credential_id)),
                uuid.UUID(str(row.credential_version_id)),
            )
            for row in references
        },
        key=lambda pair: (pair[0].int, pair[1].int),
    )
    versions: dict[uuid.UUID, CredentialVersionRow] = {}
    for credential_id, credential_version_id in ordered_versions:
        credential_version = (
            await session.execute(
                select(CredentialVersionRow)
                .where(
                    CredentialVersionRow.id == credential_version_id,
                    CredentialVersionRow.credential_id == credential_id,
                )
                .with_for_update(read=True, of=CredentialVersionRow)
            )
        ).scalar_one_or_none()
        if credential_version is None:
            raise McpCredentialClosureInvalid
        versions[credential_version_id] = credential_version

    envelope_versions: set[uuid.UUID] = set()
    for credential_version_id in sorted(versions, key=lambda value: value.int):
        envelope_id = (
            await session.execute(
                select(CredentialEnvelopeRow.id)
                .where(
                    CredentialEnvelopeRow.credential_version_id == credential_version_id,
                    CredentialEnvelopeRow.is_active.is_(True),
                )
                .with_for_update(read=True, of=CredentialEnvelopeRow)
            )
        ).scalar_one_or_none()
        if envelope_id is None:
            raise McpCredentialClosureInvalid
        envelope_versions.add(credential_version_id)

    grants: dict[uuid.UUID, CredentialGrantRow] = {}
    for grant_id in sorted(
        {uuid.UUID(str(row.id)) for row in references},
        key=lambda value: value.int,
    ):
        grant = (
            await session.execute(
                select(CredentialGrantRow)
                .where(
                    CredentialGrantRow.id == grant_id,
                    CredentialGrantRow.mcp_server_version_id == version_id,
                )
                .with_for_update(read=True, of=CredentialGrantRow)
            )
        ).scalar_one_or_none()
        if grant is None:
            raise McpCredentialClosureInvalid
        grants[grant_id] = grant

    reference_by_slot: dict[uuid.UUID, object] = {}
    for reference in references:
        slot_id = uuid.UUID(str(reference.credential_slot_id))
        if slot_id in reference_by_slot:
            raise McpCredentialClosureInvalid
        reference_by_slot[slot_id] = reference

    ordered_grants: list[CredentialGrantRow] = []
    for slot in slots:
        slot_id = uuid.UUID(str(slot.id))
        reference = reference_by_slot.get(slot_id)
        if reference is None:
            if slot.required:
                raise McpCredentialClosureInvalid
            continue
        grant = grants.get(uuid.UUID(str(reference.id)))
        credential_id = uuid.UUID(str(reference.credential_id))
        credential_version_id = uuid.UUID(str(reference.credential_version_id))
        credential = credentials.get(credential_id)
        credential_version = versions.get(credential_version_id)
        if credential is None or credential_version is None or grant is None:
            raise McpCredentialClosureInvalid
        stored_project_id = uuid.UUID(str(credential.project_id)) if credential.project_id is not None else None
        scope_matches = credential.scope == scope.value and ((scope is AssetScope.SYSTEM and stored_project_id is None) or (scope is AssetScope.PROJECT and project_id is not None and stored_project_id == project_id))
        if (
            grant.status != "active"
            or grant.credential_slot_id != slot.id
            or grant.credential_version_id != credential_version.id
            or credential.status != "active"
            or credential_version.status not in {"active", "retired"}
            or not scope_matches
            or credential_version.id not in envelope_versions
            or _normalized_schema(slot.payload_schema) != _normalized_schema(credential_version.payload_schema)
        ):
            raise McpCredentialClosureInvalid
        ordered_grants.append(grant)

    if len(ordered_grants) != len(references):
        raise McpCredentialClosureInvalid
    return LockedMcpCredentialClosure(slots, tuple(ordered_grants))
