from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared_assets.errors import AssetStorageUnavailable, AssetValidationFailed
from deerflow.persistence.shared_assets.mcp_model import (
    McpSecretSlotRow,
    ProjectMcpSecretGenerationRow,
    ProjectMcpSecretStateRow,
    ProjectMcpSecretTombstoneRow,
)
from deerflow.secrets import (
    SecretEnvelope,
    SecretKey,
    SecretKeyInvalid,
    SecretMaterializationFailed,
    SecretProtectionFailed,
    secret_envelope_digest,
)


def mcp_secret_recipient(
    project_id: uuid.UUID,
    mcp_server_id: uuid.UUID,
    mcp_server_version_id: uuid.UUID,
    slot_id: uuid.UUID,
) -> str:
    return ":".join(
        (
            "mcp",
            str(project_id),
            str(mcp_server_id),
            str(mcp_server_version_id),
            str(slot_id),
        )
    )


def mcp_secret_closure_digest(
    materials: Sequence[object],
) -> str:
    digest = hashlib.sha256(b"actweave:mcp-secret-closure:v1\0")
    ordered = sorted(
        materials,
        key=lambda item: uuid.UUID(str(getattr(item, "slot_id"))).int,
    )
    for item in ordered:
        digest.update(uuid.UUID(str(getattr(item, "slot_id"))).bytes)
        digest.update(uuid.UUID(str(getattr(item, "generation_id"))).bytes)
        generation_digest = getattr(item, "generation_digest")
        if not isinstance(generation_digest, str) or len(generation_digest) != 64:
            raise ValueError("invalid MCP secret closure")
        digest.update(generation_digest.encode("ascii"))
    return digest.hexdigest()


def _canonical_payload(
    slot: McpSecretSlotRow,
    payload: object,
    *,
    request_id: str,
) -> bytes:
    schema = slot.payload_schema
    if not isinstance(schema, Mapping) or not isinstance(payload, Mapping):
        raise AssetValidationFailed(request_id)
    if set(payload) != set(schema):
        raise AssetValidationFailed(request_id)
    normalized: dict[str, dict[str, str]] = {}
    try:
        for section, raw_names in schema.items():
            values = payload.get(section)
            if not isinstance(section, str) or not isinstance(raw_names, list) or not isinstance(values, Mapping) or set(values) != set(raw_names):
                raise ValueError
            section_values: dict[str, str] = {}
            for name in raw_names:
                value = values.get(name)
                if not isinstance(name, str) or not isinstance(value, str) or not value or "\x00" in value:
                    raise ValueError
                if section == "env" and "${" in value:
                    raise ValueError
                section_values[name] = value
            normalized[section] = section_values
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > 64 * 1024:
            raise ValueError
        return encoded
    except (RecursionError, TypeError, UnicodeError, ValueError):
        raise AssetValidationFailed(request_id) from None


@dataclass(frozen=True, slots=True)
class McpSecretMaterial:
    project_id: uuid.UUID
    mcp_server_id: uuid.UUID
    mcp_server_version_id: uuid.UUID
    slot_id: uuid.UUID
    slot_name: str
    required: bool
    revision: int
    generation_id: uuid.UUID
    generation_digest: str
    envelope: SecretEnvelope = field(repr=False)


class McpSecretStore:
    def __init__(
        self,
        session: AsyncSession,
        *,
        secret_key: SecretKey | None = None,
    ) -> None:
        self.session = session
        self._secret_key = secret_key

    def _key(self, request_id: str) -> SecretKey:
        try:
            return self._secret_key or SecretKey.from_environment()
        except SecretKeyInvalid:
            raise AssetStorageUnavailable(request_id) from None

    async def list_states(
        self,
        *,
        project_id: uuid.UUID,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
        for_update: bool = False,
    ) -> tuple[ProjectMcpSecretStateRow, ...]:
        statement = (
            select(ProjectMcpSecretStateRow)
            .where(
                ProjectMcpSecretStateRow.project_id == project_id,
                ProjectMcpSecretStateRow.mcp_server_id == mcp_server_id,
                ProjectMcpSecretStateRow.mcp_server_version_id == mcp_server_version_id,
            )
            .order_by(ProjectMcpSecretStateRow.slot_id)
        )
        if for_update:
            statement = statement.with_for_update()
        return tuple((await self.session.execute(statement)).scalars().all())

    async def ensure_states(
        self,
        *,
        project_id: uuid.UUID,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
        slots: Sequence[McpSecretSlotRow],
        actor_user_id: str,
        request_id: str,
    ) -> tuple[ProjectMcpSecretStateRow, ...]:
        states = await self.list_states(
            project_id=project_id,
            mcp_server_id=mcp_server_id,
            mcp_server_version_id=mcp_server_version_id,
            for_update=True,
        )
        by_slot = {row.slot_id: row for row in states}
        declared = {slot.id for slot in slots}
        if set(by_slot) - declared:
            raise AssetValidationFailed(request_id)
        for slot in slots:
            if slot.id not in by_slot:
                state = ProjectMcpSecretStateRow(
                    project_id=project_id,
                    mcp_server_id=mcp_server_id,
                    mcp_server_version_id=mcp_server_version_id,
                    slot_id=slot.id,
                    revision=0,
                    current_generation_id=None,
                    updated_by_user_id=actor_user_id,
                )
                self.session.add(state)
                by_slot[slot.id] = state
        await self.session.flush()
        return tuple(by_slot[slot.id] for slot in slots)

    async def replace(
        self,
        *,
        project_id: uuid.UUID,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
        slots: Sequence[McpSecretSlotRow],
        slot_name: str,
        payload: object,
        actor_user_id: str,
        request_id: str,
    ) -> ProjectMcpSecretStateRow:
        by_name = {slot.name: slot for slot in slots}
        slot = by_name.get(slot_name)
        if slot is None:
            raise AssetValidationFailed(request_id)
        plaintext = _canonical_payload(slot, payload, request_id=request_id)
        states = await self.ensure_states(
            project_id=project_id,
            mcp_server_id=mcp_server_id,
            mcp_server_version_id=mcp_server_version_id,
            slots=slots,
            actor_user_id=actor_user_id,
            request_id=request_id,
        )
        state = next(row for row in states if row.slot_id == slot.id)
        recipient = mcp_secret_recipient(
            project_id,
            mcp_server_id,
            mcp_server_version_id,
            slot.id,
        )
        try:
            envelope = SecretEnvelope.protect(
                plaintext,
                recipient=recipient,
                key=self._key(request_id),
            )
        except SecretProtectionFailed:
            raise AssetStorageUnavailable(request_id) from None
        revision = int(state.revision) + 1
        await self._destroy_current(
            state,
            reason="replace",
            revision=revision,
            actor_user_id=actor_user_id,
            request_id=request_id,
        )
        generation = ProjectMcpSecretGenerationRow(
            project_id=project_id,
            mcp_server_id=mcp_server_id,
            mcp_server_version_id=mcp_server_version_id,
            slot_id=slot.id,
            revision=revision,
            nonce=envelope.nonce,
            ciphertext=envelope.ciphertext,
            envelope_digest=secret_envelope_digest(recipient, envelope),
            created_by_user_id=actor_user_id,
        )
        self.session.add(generation)
        await self.session.flush()
        state.current_generation_id = generation.id
        state.revision = revision
        state.updated_by_user_id = actor_user_id
        state.updated_at = datetime.now(UTC)
        await self.session.flush()
        return state

    async def clear(
        self,
        *,
        project_id: uuid.UUID,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
        slots: Sequence[McpSecretSlotRow],
        slot_name: str,
        actor_user_id: str,
        request_id: str,
    ) -> ProjectMcpSecretStateRow:
        slot = next((item for item in slots if item.name == slot_name), None)
        if slot is None:
            raise AssetValidationFailed(request_id)
        states = await self.ensure_states(
            project_id=project_id,
            mcp_server_id=mcp_server_id,
            mcp_server_version_id=mcp_server_version_id,
            slots=slots,
            actor_user_id=actor_user_id,
            request_id=request_id,
        )
        state = next(row for row in states if row.slot_id == slot.id)
        revision = int(state.revision) + 1
        await self._destroy_current(
            state,
            reason="clear",
            revision=revision,
            actor_user_id=actor_user_id,
            request_id=request_id,
        )
        state.revision = revision
        state.updated_by_user_id = actor_user_id
        state.updated_at = datetime.now(UTC)
        await self.session.flush()
        return state

    async def load_materials(
        self,
        *,
        project_id: uuid.UUID,
        mcp_server_id: uuid.UUID,
        mcp_server_version_id: uuid.UUID,
        slots: Sequence[McpSecretSlotRow],
        require_required: bool,
        for_update: bool,
        request_id: str,
    ) -> tuple[McpSecretMaterial, ...]:
        states = await self.list_states(
            project_id=project_id,
            mcp_server_id=mcp_server_id,
            mcp_server_version_id=mcp_server_version_id,
            for_update=for_update,
        )
        by_slot = {row.slot_id: row for row in states}
        if set(by_slot) - {slot.id for slot in slots}:
            raise AssetValidationFailed(request_id)
        if require_required and any(slot.required and (slot.id not in by_slot or by_slot[slot.id].current_generation_id is None) for slot in slots):
            raise AssetValidationFailed(request_id)
        result: list[McpSecretMaterial] = []
        for slot in slots:
            state = by_slot.get(slot.id)
            if state is None or state.current_generation_id is None:
                continue
            statement = select(ProjectMcpSecretGenerationRow).where(
                ProjectMcpSecretGenerationRow.project_id == project_id,
                ProjectMcpSecretGenerationRow.mcp_server_id == mcp_server_id,
                ProjectMcpSecretGenerationRow.mcp_server_version_id == mcp_server_version_id,
                ProjectMcpSecretGenerationRow.slot_id == slot.id,
                ProjectMcpSecretGenerationRow.id == state.current_generation_id,
                ProjectMcpSecretGenerationRow.revision == state.revision,
            )
            if for_update:
                statement = statement.with_for_update()
            generation = (await self.session.execute(statement)).scalar_one_or_none()
            if generation is None:
                raise AssetValidationFailed(request_id)
            result.append(
                McpSecretMaterial(
                    project_id=project_id,
                    mcp_server_id=mcp_server_id,
                    mcp_server_version_id=mcp_server_version_id,
                    slot_id=slot.id,
                    slot_name=slot.name,
                    required=slot.required,
                    revision=int(state.revision),
                    generation_id=generation.id,
                    generation_digest=generation.envelope_digest,
                    envelope=SecretEnvelope(
                        nonce=bytes(generation.nonce),
                        ciphertext=bytes(generation.ciphertext),
                    ),
                )
            )
        return tuple(result)

    def materialize(
        self,
        material: McpSecretMaterial,
        *,
        request_id: str,
    ) -> Mapping[str, Mapping[str, str]]:
        recipient = mcp_secret_recipient(
            material.project_id,
            material.mcp_server_id,
            material.mcp_server_version_id,
            material.slot_id,
        )
        try:
            decoded = json.loads(
                material.envelope.materialize(
                    recipient=recipient,
                    key=self._key(request_id),
                ).decode("utf-8")
            )
            if not isinstance(decoded, dict):
                raise ValueError
            return decoded
        except (
            json.JSONDecodeError,
            SecretMaterializationFailed,
            TypeError,
            UnicodeError,
            ValueError,
        ):
            raise AssetStorageUnavailable(request_id) from None

    async def copy_compatible(
        self,
        *,
        project_id: uuid.UUID,
        mcp_server_id: uuid.UUID,
        source_version_id: uuid.UUID,
        source_slots: Sequence[McpSecretSlotRow],
        target_version_id: uuid.UUID,
        target_slots: Sequence[McpSecretSlotRow],
        actor_user_id: str,
        request_id: str,
    ) -> tuple[ProjectMcpSecretStateRow, ...]:
        source_by_name = {slot.name: slot for slot in source_slots}
        target_by_name = {slot.name: slot for slot in target_slots}
        compatible_names = {name for name, target in target_by_name.items() if name in source_by_name and source_by_name[name].payload_schema == target.payload_schema and source_by_name[name].required == target.required}
        source_materials = await self.load_materials(
            project_id=project_id,
            mcp_server_id=mcp_server_id,
            mcp_server_version_id=source_version_id,
            slots=source_slots,
            require_required=False,
            for_update=True,
            request_id=request_id,
        )
        copied: list[ProjectMcpSecretStateRow] = []
        for material in source_materials:
            if material.slot_name not in compatible_names:
                continue
            payload = self.materialize(material, request_id=request_id)
            copied.append(
                await self.replace(
                    project_id=project_id,
                    mcp_server_id=mcp_server_id,
                    mcp_server_version_id=target_version_id,
                    slots=target_slots,
                    slot_name=material.slot_name,
                    payload=payload,
                    actor_user_id=actor_user_id,
                    request_id=request_id,
                )
            )
        return tuple(copied)

    async def _destroy_current(
        self,
        state: ProjectMcpSecretStateRow,
        *,
        reason: str,
        revision: int,
        actor_user_id: str,
        request_id: str,
    ) -> None:
        if state.current_generation_id is None:
            return
        generation = (
            await self.session.execute(
                select(ProjectMcpSecretGenerationRow)
                .where(
                    ProjectMcpSecretGenerationRow.project_id == state.project_id,
                    ProjectMcpSecretGenerationRow.mcp_server_id == state.mcp_server_id,
                    ProjectMcpSecretGenerationRow.mcp_server_version_id == state.mcp_server_version_id,
                    ProjectMcpSecretGenerationRow.slot_id == state.slot_id,
                    ProjectMcpSecretGenerationRow.id == state.current_generation_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if generation is None:
            raise AssetStorageUnavailable(request_id)
        state.current_generation_id = None
        await self.session.flush()
        self.session.add(
            ProjectMcpSecretTombstoneRow(
                project_id=state.project_id,
                mcp_server_id=state.mcp_server_id,
                mcp_server_version_id=state.mcp_server_version_id,
                slot_id=state.slot_id,
                destroyed_generation_id=generation.id,
                revision=revision,
                envelope_digest=generation.envelope_digest,
                reason=reason,
                destroyed_by_user_id=actor_user_id,
            )
        )
        await self.session.execute(delete(ProjectMcpSecretGenerationRow).where(ProjectMcpSecretGenerationRow.id == generation.id))


__all__ = [
    "McpSecretMaterial",
    "McpSecretStore",
    "mcp_secret_closure_digest",
    "mcp_secret_recipient",
]
