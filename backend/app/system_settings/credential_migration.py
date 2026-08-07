"""Re-point system models at a rotated System Credential version.

A system model resolves its provider key through an exact
``credential_version_id`` and the runtime adapter deliberately accepts a
``retired`` version so frozen Run snapshots stay reproducible. Replacing a
Credential therefore leaves every pinned model decrypting the previous
envelope. This adapter mints the next immutable model version carrying the same
provider payload against the new Credential version, which is the only way to
move a model onto a rotated key.

Locks are taken in the catalog order used by ``SystemModelCatalogService``
(catalog state, then model, then model version) *before* the caller locks the
Credential, so a concurrent model edit and a Credential migration can never
deadlock against each other.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared_assets.credential_service import SystemModelMigrationIncompatible
from app.system_settings.models import UpdateSystemModel
from app.system_settings.validation import (
    ModelSettingsInvalid,
    canonical_model_payload_checksum,
)
from deerflow.persistence.shared_assets import CredentialVersionRow
from deerflow.persistence.system_settings import (
    SystemModelCatalogStateRow,
    SystemModelConfigRow,
    SystemModelConfigVersionRow,
)


@dataclass
class _LockedPin:
    model: SystemModelConfigRow
    version: SystemModelConfigVersionRow


@dataclass
class _BoundMigration:
    """One Credential migration's view of the locked system model catalog."""

    session: AsyncSession
    state: SystemModelCatalogStateRow | None = None
    pins: tuple[_LockedPin, ...] = field(default_factory=tuple)

    async def lock_pinned_models(self, credential_id: uuid.UUID) -> int:
        self.state = (await self.session.execute(select(SystemModelCatalogStateRow).where(SystemModelCatalogStateRow.id == 1).with_for_update(of=SystemModelCatalogStateRow))).scalar_one_or_none()
        if self.state is None:
            raise SystemModelMigrationIncompatible
        models = tuple(
            (
                await self.session.execute(
                    select(SystemModelConfigRow)
                    .join(
                        SystemModelConfigVersionRow,
                        (SystemModelConfigVersionRow.id == SystemModelConfigRow.current_version_id) & (SystemModelConfigVersionRow.model_config_id == SystemModelConfigRow.id),
                    )
                    .where(SystemModelConfigVersionRow.credential_id == credential_id)
                    .order_by(SystemModelConfigRow.id)
                    .with_for_update(of=SystemModelConfigRow)
                )
            ).scalars()
        )
        pins: list[_LockedPin] = []
        for model in models:
            version = (
                await self.session.execute(
                    select(SystemModelConfigVersionRow)
                    .where(
                        SystemModelConfigVersionRow.id == model.current_version_id,
                        SystemModelConfigVersionRow.model_config_id == model.id,
                    )
                    .with_for_update(of=SystemModelConfigVersionRow)
                )
            ).scalar_one_or_none()
            if version is None:
                raise SystemModelMigrationIncompatible
            pins.append(_LockedPin(model=model, version=version))
        self.pins = tuple(pins)
        return len(self.pins)

    async def count_stale_pins(
        self,
        credential_id: uuid.UUID,
        target_version_id: uuid.UUID,
    ) -> int:
        """Count current model pointers a migration would have to re-point.

        Deliberately lock-free: this answers "how much is still on the old
        envelope" for a Credential replacement, which must not acquire the
        catalog locks that ``lock_pinned_models`` orders for the migration.
        """

        statement = (
            select(func.count())
            .select_from(SystemModelConfigRow)
            .join(
                SystemModelConfigVersionRow,
                (SystemModelConfigVersionRow.id == SystemModelConfigRow.current_version_id) & (SystemModelConfigVersionRow.model_config_id == SystemModelConfigRow.id),
            )
            .where(
                SystemModelConfigVersionRow.credential_id == credential_id,
                SystemModelConfigVersionRow.credential_version_id != target_version_id,
            )
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def repoint(
        self,
        target_version: CredentialVersionRow,
        *,
        user_id: uuid.UUID,
    ) -> int:
        state = self.state
        if state is None:
            raise SystemModelMigrationIncompatible
        target_env = target_version.payload_schema.get("env") if isinstance(target_version.payload_schema, dict) else None
        if not isinstance(target_env, list) or not all(isinstance(value, str) for value in target_env):
            raise SystemModelMigrationIncompatible
        stale = tuple(pin for pin in self.pins if pin.version.credential_version_id != target_version.id)
        if not stale:
            return 0
        for pin in stale:
            if pin.version.credential_id != target_version.credential_id or pin.version.credential_env_key not in target_env:
                raise SystemModelMigrationIncompatible
            successor = SystemModelConfigVersionRow(
                id=uuid.uuid4(),
                model_config_id=pin.model.id,
                version_number=await self._next_version_number(pin.model),
                provider_adapter=pin.version.provider_adapter,
                provider_model=pin.version.provider_model,
                settings=dict(pin.version.settings),
                supports_thinking=pin.version.supports_thinking,
                supports_reasoning_effort=pin.version.supports_reasoning_effort,
                supports_vision=pin.version.supports_vision,
                credential_id=pin.version.credential_id,
                credential_version_id=target_version.id,
                credential_env_key=pin.version.credential_env_key,
                payload_checksum=_repointed_checksum(pin, target_version.id),
                supersedes_version_id=pin.version.id,
                created_by_user_id=str(user_id),
            )
            self.session.add(successor)
            await self.session.flush()
            pin.model.current_version_id = successor.id
            pin.model.revision += 1
            pin.model.updated_by_user_id = str(user_id)
            await self.session.flush()
        state.revision += 1
        state.updated_by_user_id = str(user_id)
        await self.session.flush()
        return len(stale)

    async def _next_version_number(self, model: SystemModelConfigRow) -> int:
        highest = await self.session.scalar(
            select(func.max(SystemModelConfigVersionRow.version_number)).where(
                SystemModelConfigVersionRow.model_config_id == model.id,
            )
        )
        return int(highest or 0) + 1


def _repointed_checksum(pin: _LockedPin, credential_version_id: uuid.UUID) -> str:
    """Prove the canonical payload round-trips before minting a new checksum.

    Recomputing the locked version's own checksum from its stored columns is
    what makes the re-point safe: an equal result means the reconstruction is
    faithful, so the only difference in the new checksum is the Credential
    version. Any drift or out-of-band edit fails closed instead of silently
    publishing a payload whose checksum no longer describes it.
    """

    model_id = uuid.UUID(str(pin.model.id))
    try:
        verified = canonical_model_payload_checksum(
            model_id,
            _command(pin, uuid.UUID(str(pin.version.credential_version_id))),
        )
        if verified != pin.version.payload_checksum:
            raise SystemModelMigrationIncompatible
        return canonical_model_payload_checksum(
            model_id,
            _command(pin, credential_version_id),
        )
    except (ModelSettingsInvalid, TypeError, ValueError):
        raise SystemModelMigrationIncompatible from None


def _command(pin: _LockedPin, credential_version_id: uuid.UUID) -> UpdateSystemModel:
    return UpdateSystemModel(
        display_name=pin.model.display_name,
        description=pin.model.description,
        provider_adapter=pin.version.provider_adapter,
        provider_model=pin.version.provider_model,
        settings=pin.version.settings,
        supports_thinking=pin.version.supports_thinking,
        supports_reasoning_effort=pin.version.supports_reasoning_effort,
        supports_vision=pin.version.supports_vision,
        credential_id=uuid.UUID(str(pin.version.credential_id)),
        credential_version_id=credential_version_id,
        credential_env_key=pin.version.credential_env_key,
        sort_order=pin.model.sort_order,
    )


class SystemModelCredentialMigrationAdapter:
    """Bind one Credential-migration transaction to the system model catalog."""

    def bind(self, session: AsyncSession) -> _BoundMigration:
        return _BoundMigration(session=session)


__all__ = ["SystemModelCredentialMigrationAdapter"]
