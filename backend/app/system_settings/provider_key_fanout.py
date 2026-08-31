"""Provider Key fan-out over bound text models, inside the caller's transaction.

This is the narrow collaborator between the retrieval model registry (which
owns Provider rows and their write transactions) and the System Model catalog
(which owns per-model secret generations). It never opens transactions, never
increments the catalog revision and never performs network I/O: the caller
holds ``admin → catalog state → provider`` locks, then delegates the
``system_model_configs → generation`` tail of the lock protocol here.

Fan-out takes every bound text model (suspended included) with
``FOR UPDATE NOWAIT`` in UUID order, then the current secret generations, and
only after all locks are held re-encrypts the validated plaintext Key per
model recipient. Any busy lock raises :class:`ProviderKeyFanoutLockBusy` so
the caller rolls back the whole settle and answers HTTP 409.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditOutcome,
    AuditTarget,
    AuditTargetKind,
    SystemAuditContext,
)
from app.audit.service import AuditService
from app.system_settings.models import UpdateSystemModel
from app.system_settings.secrets import (
    model_secret_envelope_digest,
    model_secret_recipient,
)
from app.system_settings.validation import (
    canonical_model_payload_checksum,
    is_provider_adapter_eligible_for_new_binding,
    provider_api_key_required,
)
from deerflow.persistence.system_settings import (
    SystemModelConfigRow,
    SystemModelSecretGenerationRow,
    SystemModelSecretTombstoneRow,
)
from deerflow.secrets import SecretEnvelope, SecretKey

_LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


class ProviderKeyFanoutLockBusy(Exception):
    """A NOWAIT model or generation lock was busy; caller must roll back."""


class ProviderKeyFanoutInvariant(Exception):
    """A bound model points at a generation row that does not exist."""


def is_lock_not_available(error: BaseException) -> bool:
    """Whether this database error is exactly PostgreSQL 55P03 (lock busy)."""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for attribute in ("sqlstate", "pgcode"):
            if getattr(current, attribute, None) == _LOCK_NOT_AVAILABLE_SQLSTATE:
                return True
        current = getattr(current, "orig", None) or current.__cause__
    return False


async def count_bound_text_models(
    session: AsyncSession,
    provider_id: uuid.UUID,
) -> tuple[int, int]:
    """Return ``(total, active)`` text models bound to this Provider."""

    statuses = (
        await session.execute(
            select(SystemModelConfigRow.status).where(
                SystemModelConfigRow.provider_id == provider_id,
            )
        )
    ).scalars()
    total = 0
    active = 0
    for status in statuses:
        total += 1
        if status == "active":
            active += 1
    return total, active


async def lock_bound_text_models_nowait(
    session: AsyncSession,
    provider_id: uuid.UUID,
) -> list[SystemModelConfigRow]:
    """Lock every bound text model in UUID order, refusing to wait."""

    try:
        rows = (await session.scalars(select(SystemModelConfigRow).where(SystemModelConfigRow.provider_id == provider_id).order_by(SystemModelConfigRow.id).with_for_update(of=SystemModelConfigRow, nowait=True))).all()
    except DBAPIError as error:
        if is_lock_not_available(error):
            raise ProviderKeyFanoutLockBusy from None
        raise
    return list(rows)


async def _lock_current_generation_nowait(
    session: AsyncSession,
    model: SystemModelConfigRow,
) -> SystemModelSecretGenerationRow | None:
    if model.current_secret_generation_id is None:
        return None
    try:
        generation = (
            await session.execute(
                select(SystemModelSecretGenerationRow)
                .where(
                    SystemModelSecretGenerationRow.id == model.current_secret_generation_id,
                    SystemModelSecretGenerationRow.model_config_id == model.id,
                )
                .with_for_update(of=SystemModelSecretGenerationRow, nowait=True)
            )
        ).scalar_one_or_none()
    except DBAPIError as error:
        if is_lock_not_available(error):
            raise ProviderKeyFanoutLockBusy from None
        raise
    if generation is None:
        raise ProviderKeyFanoutInvariant
    return generation


def derive_model_base_url(
    model: SystemModelConfigRow,
    base_url: str,
) -> bool:
    """Pin the provider-derived ``settings.base_url``; return whether it changed.

    When the derived URL changes, the canonical payload checksum is recomputed
    from the exact persisted settings; a pure Key rotation leaves both alone.
    """

    if model.settings.get("base_url") == base_url:
        return False
    settings = dict(model.settings)
    settings["base_url"] = base_url
    model.settings = settings
    flag_modified(model, "settings")
    model.payload_checksum = canonical_model_payload_checksum(
        uuid.UUID(str(model.id)),
        UpdateSystemModel(
            display_name=model.display_name,
            provider_id=uuid.UUID(str(model.provider_id)),
            provider_adapter=model.provider_adapter,
            provider_model=model.provider_model,
            max_input_tokens=model.max_input_tokens,
            settings=settings,
            supports_thinking=model.supports_thinking,
            supports_reasoning_effort=model.supports_reasoning_effort,
            supports_vision=model.supports_vision,
        ),
    )
    return True


def build_model_secret_generation(
    model: SystemModelConfigRow,
    *,
    api_key: str,
    revision: int,
    actor_user_id: str,
    secret_key: SecretKey,
) -> SystemModelSecretGenerationRow:
    """Protect one plaintext Key for this model's current recipient."""

    recipient = model_secret_recipient(
        uuid.UUID(str(model.id)),
        model.provider_adapter,
        model.settings,
    )
    envelope = SecretEnvelope.protect(
        api_key.encode("utf-8"),
        recipient=recipient,
        key=secret_key,
    )
    return SystemModelSecretGenerationRow(
        id=uuid.uuid4(),
        model_config_id=model.id,
        revision=revision,
        nonce=envelope.nonce,
        ciphertext=envelope.ciphertext,
        envelope_digest=model_secret_envelope_digest(recipient, envelope),
        created_by_user_id=actor_user_id,
    )


async def regenerate_model_secret(
    session: AsyncSession,
    model: SystemModelConfigRow,
    previous: SystemModelSecretGenerationRow | None,
    *,
    api_key: str,
    actor_user_id: str,
    secret_key: SecretKey,
    reason: str,
) -> SystemModelSecretGenerationRow:
    """Replace this model's generation with a re-protected plaintext Key.

    ``previous`` must already be locked by the caller (FOR UPDATE or NOWAIT).
    The old generation is tombstoned with ``reason`` and destroyed; the model's
    ``secret_revision`` advances exactly once.
    """

    next_revision = int(model.secret_revision) + 1
    generation = build_model_secret_generation(
        model,
        api_key=api_key,
        revision=next_revision,
        actor_user_id=actor_user_id,
        secret_key=secret_key,
    )
    session.add(generation)
    await session.flush()
    model.current_secret_generation_id = generation.id
    model.secret_revision = next_revision
    await session.flush()
    if previous is not None:
        session.add(
            SystemModelSecretTombstoneRow(
                generation_id=previous.id,
                model_config_id=previous.model_config_id,
                revision=previous.revision,
                envelope_digest=previous.envelope_digest,
                reason=reason,
                destroyed_by_user_id=actor_user_id,
                created_at=previous.created_at,
            )
        )
        await session.delete(previous)
        await session.flush()
    return generation


def _secret_readiness(model: SystemModelConfigRow) -> str:
    eligible = is_provider_adapter_eligible_for_new_binding(model.provider_adapter)
    configured = model.current_secret_generation_id is not None
    ready = eligible and (not provider_api_key_required(model.provider_adapter) or configured)
    return "ready" if ready else "unready"


class ProviderKeyFanout:
    """Locked-scope re-encryption of one Provider Key across bound text models."""

    def __init__(
        self,
        *,
        secret_key: SecretKey,
        audit_service: AuditService | None = None,
    ) -> None:
        self._secret_key = secret_key
        self._audit_service = audit_service

    async def rotate_bound_text_models(
        self,
        session: AsyncSession,
        actor: SystemAuditContext,
        *,
        provider_id: uuid.UUID,
        base_url: str,
        api_key: str,
    ) -> int:
        """Re-encrypt the validated Key for every bound text model.

        Returns the number of models regenerated. Acquires every model and
        generation lock before mutating anything, so a busy lock rolls the
        whole settle back with zero partial updates.
        """

        models = await lock_bound_text_models_nowait(session, provider_id)
        if not models:
            return 0
        previous_generations = [await _lock_current_generation_nowait(session, model) for model in models]
        for model, previous in zip(models, previous_generations, strict=True):
            old_recipient = model_secret_recipient(
                uuid.UUID(str(model.id)),
                model.provider_adapter,
                model.settings,
            )
            derive_model_base_url(model, base_url)
            new_recipient = model_secret_recipient(
                uuid.UUID(str(model.id)),
                model.provider_adapter,
                model.settings,
            )
            reason = "recipient_changed" if new_recipient != old_recipient else "replaced"
            had_secret = previous is not None
            generation = await regenerate_model_secret(
                session,
                model,
                previous,
                api_key=api_key,
                actor_user_id=str(actor.user_id),
                secret_key=self._secret_key,
                reason=reason,
            )
            model.revision = int(model.revision) + 1
            model.updated_by_user_id = str(actor.user_id)
            await session.flush()
            await self._append_secret_event(
                session,
                actor,
                model,
                action=("model.secret.replace" if had_secret else "model.secret.configure"),
                generation_id=generation.id,
                reason=(reason if had_secret else "created"),
            )
        return len(models)

    async def _append_secret_event(
        self,
        session: AsyncSession,
        actor: SystemAuditContext,
        model: SystemModelConfigRow,
        *,
        action: str,
        generation_id: uuid.UUID | None,
        reason: str,
    ) -> None:
        if self._audit_service is None:
            return
        await self._audit_service.append(
            session,
            AuditActor.system_admin(actor),
            AuditAction.ASSET_UPDATED,
            AuditTarget(AuditTargetKind.ASSET, uuid.UUID(str(model.id)), None),
            AuditOutcome.SUCCESS,
            {
                "asset_kind": "model",
                "operation": action,
                "generation_id": generation_id,
                "revision": int(model.secret_revision),
                "result": "configured",
                "reason": reason,
                "readiness": _secret_readiness(model),
            },
            request_id=actor.request_id,
        )


__all__ = [
    "ProviderKeyFanout",
    "ProviderKeyFanoutInvariant",
    "ProviderKeyFanoutLockBusy",
    "build_model_secret_generation",
    "count_bound_text_models",
    "derive_model_base_url",
    "is_lock_not_available",
    "lock_bound_text_models_nowait",
    "regenerate_model_secret",
]
