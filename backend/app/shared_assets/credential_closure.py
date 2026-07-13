from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

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
class McpCredentialClosureTarget:
    version_id: uuid.UUID
    scope: AssetScope
    project_id: uuid.UUID | None


@dataclass(frozen=True)
class LockedMcpCredentialMaterial:
    slot: McpCredentialSlotRow
    grant: CredentialGrantRow
    credential: CredentialRow
    version: CredentialVersionRow
    envelope_id: uuid.UUID
    envelope: CredentialEnvelopeRow | None = field(default=None, repr=False)


@dataclass(frozen=True)
class LockedMcpCredentialClosure:
    slots: tuple[McpCredentialSlotRow, ...]
    materials: tuple[LockedMcpCredentialMaterial, ...] = field(repr=False)

    @property
    def grants(self) -> tuple[CredentialGrantRow, ...]:
        return tuple(material.grant for material in self.materials)

    @property
    def grant_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(uuid.UUID(str(material.grant.id)) for material in self.materials)


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


def _normalize_targets(
    targets: Sequence[McpCredentialClosureTarget],
) -> dict[uuid.UUID, McpCredentialClosureTarget]:
    normalized: dict[uuid.UUID, McpCredentialClosureTarget] = {}
    for target in targets:
        if (
            not isinstance(target, McpCredentialClosureTarget)
            or not isinstance(target.version_id, uuid.UUID)
            or not isinstance(target.scope, AssetScope)
            or (target.scope is AssetScope.SYSTEM and target.project_id is not None)
            or (target.scope is AssetScope.PROJECT and not isinstance(target.project_id, uuid.UUID))
        ):
            raise McpCredentialClosureInvalid
        version_id = uuid.UUID(str(target.version_id))
        canonical = McpCredentialClosureTarget(
            version_id,
            target.scope,
            (uuid.UUID(str(target.project_id)) if target.project_id is not None else None),
        )
        existing = normalized.get(version_id)
        if existing is not None and existing != canonical:
            raise McpCredentialClosureInvalid
        normalized[version_id] = canonical
    return normalized


async def lock_mcp_credential_closures(
    session: AsyncSession,
    targets: Sequence[McpCredentialClosureTarget],
    *,
    load_envelopes: bool = False,
) -> dict[uuid.UUID, LockedMcpCredentialClosure]:
    """Lock complete MCP credential closures using one transaction-global order.

    Callers lock the project, binding/MCP asset and exact MCP versions first. This
    primitive then locks every slot before taking any credential lock, followed by
    the union of logical credentials, semantic versions, active envelopes and
    grants. Reading envelope bytes is opt-in for the plaintext materializer only.
    """

    normalized_targets = _normalize_targets(targets)
    ordered_version_ids = sorted(normalized_targets, key=lambda value: value.int)
    slots_by_version: dict[uuid.UUID, tuple[McpCredentialSlotRow, ...]] = {}
    references_by_version: dict[uuid.UUID, tuple[object, ...]] = {}

    # Lock every target's slots before reading any grant reference. FOR UPDATE
    # conflicts with the FK key-share lock required to insert or re-pin a grant,
    # so the collected reference set stays stable for the transaction.
    for version_id in ordered_version_ids:
        slots_by_version[version_id] = tuple(
            (await session.execute(select(McpCredentialSlotRow).where(McpCredentialSlotRow.mcp_server_version_id == version_id).order_by(McpCredentialSlotRow.name, McpCredentialSlotRow.id).with_for_update(of=McpCredentialSlotRow)))
            .scalars()
            .all()
        )

    # Collect the entire transaction's grant-reference closure only after all
    # slot rows are protected, and before the first logical credential lock.
    for version_id in ordered_version_ids:
        references_by_version[version_id] = tuple(
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

    all_references = tuple(reference for version_id in ordered_version_ids for reference in references_by_version[version_id])
    credential_ids = sorted(
        {uuid.UUID(str(row.credential_id)) for row in all_references},
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
            for row in all_references
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

    envelopes: dict[
        uuid.UUID,
        tuple[uuid.UUID, CredentialEnvelopeRow | None],
    ] = {}
    for _credential_id, credential_version_id in ordered_versions:
        if load_envelopes:
            envelope = (
                await session.execute(
                    select(CredentialEnvelopeRow)
                    .where(
                        CredentialEnvelopeRow.credential_version_id == credential_version_id,
                        CredentialEnvelopeRow.is_active.is_(True),
                    )
                    .with_for_update(read=True, of=CredentialEnvelopeRow)
                )
            ).scalar_one_or_none()
            if envelope is None:
                raise McpCredentialClosureInvalid
            envelopes[credential_version_id] = (
                uuid.UUID(str(envelope.id)),
                envelope,
            )
        else:
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
            envelopes[credential_version_id] = (
                uuid.UUID(str(envelope_id)),
                None,
            )

    reference_versions: dict[uuid.UUID, uuid.UUID] = {}
    grants: dict[uuid.UUID, CredentialGrantRow] = {}
    grant_versions: dict[uuid.UUID, uuid.UUID] = {}
    for version_id in ordered_version_ids:
        for reference in references_by_version[version_id]:
            grant_id = uuid.UUID(str(reference.id))
            if grant_id in reference_versions:
                raise McpCredentialClosureInvalid
            reference_versions[grant_id] = version_id
    for grant_id in sorted(reference_versions, key=lambda value: value.int):
        version_id = reference_versions[grant_id]
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
        grant_versions[grant_id] = version_id

    closures: dict[uuid.UUID, LockedMcpCredentialClosure] = {}
    for version_id in ordered_version_ids:
        target = normalized_targets[version_id]
        slots = slots_by_version[version_id]
        references = references_by_version[version_id]
        reference_by_slot: dict[uuid.UUID, object] = {}
        for reference in references:
            slot_id = uuid.UUID(str(reference.credential_slot_id))
            if slot_id in reference_by_slot:
                raise McpCredentialClosureInvalid
            reference_by_slot[slot_id] = reference

        materials: list[LockedMcpCredentialMaterial] = []
        for slot in slots:
            slot_id = uuid.UUID(str(slot.id))
            reference = reference_by_slot.get(slot_id)
            if reference is None:
                if slot.required:
                    raise McpCredentialClosureInvalid
                continue
            grant_id = uuid.UUID(str(reference.id))
            credential_id = uuid.UUID(str(reference.credential_id))
            credential_version_id = uuid.UUID(str(reference.credential_version_id))
            credential = credentials.get(credential_id)
            credential_version = versions.get(credential_version_id)
            grant = grants.get(grant_id)
            envelope_material = envelopes.get(credential_version_id)
            if credential is None or credential_version is None or grant is None or envelope_material is None or grant_versions.get(grant_id) != version_id:
                raise McpCredentialClosureInvalid
            stored_project_id = uuid.UUID(str(credential.project_id)) if credential.project_id is not None else None
            scope_matches = credential.scope == target.scope.value and ((target.scope is AssetScope.SYSTEM and stored_project_id is None) or (target.scope is AssetScope.PROJECT and stored_project_id == target.project_id))
            if (
                grant.status != "active"
                or uuid.UUID(str(grant.credential_slot_id)) != slot_id
                or uuid.UUID(str(grant.credential_version_id)) != credential_version_id
                or credential.status != "active"
                or credential_version.status not in {"active", "retired"}
                or not scope_matches
                or _normalized_schema(slot.payload_schema) != _normalized_schema(credential_version.payload_schema)
            ):
                raise McpCredentialClosureInvalid
            envelope_id, envelope = envelope_material
            materials.append(
                LockedMcpCredentialMaterial(
                    slot,
                    grant,
                    credential,
                    credential_version,
                    envelope_id,
                    envelope,
                )
            )

        if len(materials) != len(references):
            raise McpCredentialClosureInvalid
        closures[version_id] = LockedMcpCredentialClosure(
            slots,
            tuple(materials),
        )
    return closures


async def lock_mcp_credential_closure(
    session: AsyncSession,
    mcp_version_id: uuid.UUID,
    *,
    scope: AssetScope,
    project_id: uuid.UUID | None,
    load_envelopes: bool = False,
) -> LockedMcpCredentialClosure:
    """Single-MCP adapter over the transaction-global batch primitive."""

    version_id = uuid.UUID(str(mcp_version_id))
    closures = await lock_mcp_credential_closures(
        session,
        (McpCredentialClosureTarget(version_id, scope, project_id),),
        load_envelopes=load_envelopes,
    )
    return closures[version_id]
