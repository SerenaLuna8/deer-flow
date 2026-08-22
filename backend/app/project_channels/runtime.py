from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.project_channels.errors import ChannelInstanceValidationFailed
from app.project_channels.providers import (
    CHANNEL_PROVIDER_SPECS,
    validate_channel_configuration,
)
from app.project_channels.secret_store import channel_secret_recipient
from deerflow.persistence.channel_connections.model import (
    ProjectChannelInstanceRow,
    ProjectChannelSecretGenerationRow,
    ProjectChannelSecretStateRow,
)
from deerflow.persistence.channel_connections.project_instance_repository import (
    ChannelInstanceLeaseClaim,
    ProjectChannelInstanceRepository,
)
from deerflow.secrets import (
    SecretEnvelope,
    SecretKey,
    SecretKeyInvalid,
    SecretMaterializationFailed,
)

_LEASE_TTL_SECONDS = 30
_HEARTBEAT_SECONDS = 10


class ChannelRuntimeMaterializationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Channel runtime configuration unavailable")


@dataclass(frozen=True)
class ProjectChannelRuntimeConfig:
    instance_id: uuid.UUID
    provider: str
    config: dict[str, Any] = field(repr=False)
    instance_revision: int = 0
    secret_revision: int = 0
    secret_generation_id: uuid.UUID | None = None

    @property
    def closure(self) -> tuple[int, int, uuid.UUID | None]:
        return (
            self.instance_revision,
            self.secret_revision,
            self.secret_generation_id,
        )


class ProjectChannelSecretMaterializer:
    """Resolve the then-current Channel bundle only at the delivery boundary."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        secret_key: SecretKey | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_key = secret_key

    async def load(self, instance_id: uuid.UUID) -> ProjectChannelRuntimeConfig:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    statement = (
                        select(
                            ProjectChannelInstanceRow,
                            ProjectChannelSecretStateRow,
                            ProjectChannelSecretGenerationRow,
                        )
                        .join(
                            ProjectChannelSecretStateRow,
                            (ProjectChannelSecretStateRow.project_id == ProjectChannelInstanceRow.project_id)
                            & (ProjectChannelSecretStateRow.channel_instance_id == ProjectChannelInstanceRow.id)
                            & (ProjectChannelSecretStateRow.current_generation_id.is_not(None)),
                        )
                        .join(
                            ProjectChannelSecretGenerationRow,
                            (ProjectChannelSecretGenerationRow.id == ProjectChannelSecretStateRow.current_generation_id)
                            & (ProjectChannelSecretGenerationRow.project_id == ProjectChannelSecretStateRow.project_id)
                            & (ProjectChannelSecretGenerationRow.channel_instance_id == ProjectChannelSecretStateRow.channel_instance_id),
                        )
                        .where(
                            ProjectChannelInstanceRow.id == instance_id,
                            ProjectChannelInstanceRow.deleted_at.is_(None),
                            ProjectChannelInstanceRow.desired_status == "enabled",
                        )
                    )
                    material = (await session.execute(statement)).one_or_none()
            if material is None:
                raise ChannelRuntimeMaterializationError("channel_secrets_unavailable")
            instance, state, generation = material
            spec = CHANNEL_PROVIDER_SPECS.get(instance.provider)
            if spec is None or not isinstance(instance.public_config, Mapping):
                raise ChannelRuntimeMaterializationError("channel_secrets_unavailable")
            try:
                plaintext = SecretEnvelope(
                    nonce=bytes(generation.nonce),
                    ciphertext=bytes(generation.ciphertext),
                ).materialize(
                    recipient=channel_secret_recipient(instance),
                    key=self._secret_key or SecretKey.from_environment(),
                )
                payload = json.loads(plaintext)
            except (
                json.JSONDecodeError,
                SecretKeyInvalid,
                SecretMaterializationFailed,
                TypeError,
                UnicodeError,
            ):
                raise ChannelRuntimeMaterializationError("channel_secrets_unavailable") from None
            if not isinstance(payload, Mapping):
                raise ChannelRuntimeMaterializationError("channel_secrets_unavailable")
            secrets = {field: payload.get(field) for field in spec.secret_fields}
            try:
                normalized = validate_channel_configuration(
                    instance.provider,
                    public_config=instance.public_config,
                    secrets=secrets,
                    request_id="channel-runtime",
                )
            except ChannelInstanceValidationFailed:
                raise ChannelRuntimeMaterializationError("channel_secrets_unavailable") from None
            config: dict[str, Any] = {
                "enabled": True,
                **normalized.public_config,
            }
            for secret_field in spec.secret_fields:
                value = payload.get(secret_field)
                if not isinstance(value, str) or not value:
                    raise ChannelRuntimeMaterializationError("channel_secrets_unavailable")
                config[secret_field] = value
            return ProjectChannelRuntimeConfig(
                instance_id=instance.id,
                provider=instance.provider,
                config=config,
                instance_revision=instance.revision,
                secret_revision=state.revision,
                secret_generation_id=generation.id,
            )
        except ChannelRuntimeMaterializationError:
            raise
        except (DBAPIError, SATimeoutError):
            raise ChannelRuntimeMaterializationError("channel_secrets_unavailable") from None


class ProjectChannelRuntimeCoordinator:
    """Lease and reconcile project channel instances across Gateway processes."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        channel_service: Any,
        *,
        repository: ProjectChannelInstanceRepository | Any | None = None,
        materializer: ProjectChannelSecretMaterializer | Any | None = None,
        holder_id: uuid.UUID | None = None,
        start_heartbeat_tasks: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._channel_service = channel_service
        self._repository = repository or ProjectChannelInstanceRepository()
        self._materializer = materializer or ProjectChannelSecretMaterializer(session_factory)
        self._holder_id = holder_id or uuid.uuid4()
        self._start_heartbeat_tasks = start_heartbeat_tasks
        self._leases: dict[uuid.UUID, ChannelInstanceLeaseClaim] = {}
        self._heartbeats: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._applied_revisions: dict[uuid.UUID, int] = {}
        self._applied_closures: dict[
            uuid.UUID,
            tuple[int, int, uuid.UUID | None],
        ] = {}
        self._stopping = False
        set_authority = getattr(
            self._channel_service,
            "set_channel_instance_authority",
            None,
        )
        if set_authority is not None:
            set_authority(self._lease_authorized)

    async def start(self) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                rows = await self._repository.list_enabled_instances(session)
        for row in rows:
            await self.reconcile(row.id)

    async def stop(self) -> None:
        self._stopping = True
        for instance_id in tuple(self._leases):
            await self.remove(instance_id)
        for task in tuple(self._heartbeats.values()):
            task.cancel()
        if self._heartbeats:
            await asyncio.gather(*self._heartbeats.values(), return_exceptions=True)
        self._heartbeats.clear()

    async def reconcile(self, instance_id: uuid.UUID) -> bool:
        instance_id = uuid.UUID(str(instance_id))
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            row = await self._get_instance(instance_id)
            if row is None or row.deleted_at is not None or row.desired_status != "enabled":
                return await self._remove_locked(instance_id, set_stopped=row is not None)

            claim = self._leases.get(instance_id)
            if claim is None:
                async with self._session_factory() as session:
                    async with session.begin():
                        claim = await self._repository.claim_instance_lease(
                            session,
                            instance_id,
                            self._holder_id,
                            _LEASE_TTL_SECONDS,
                        )
                if claim is None:
                    # Keep monitoring this enabled instance so another Gateway
                    # can take over after the current owner's lease expires.
                    self._ensure_heartbeat(instance_id)
                    return True
                self._leases[instance_id] = claim

            # Renewal must begin before provider startup. Some adapters perform
            # network handshakes that can outlive the 30-second lease TTL.
            self._ensure_heartbeat(instance_id)
            if not await self._set_status(
                instance_id,
                "starting",
                None,
                expected_revision=row.revision,
            ):
                await self._remove_locked(instance_id, set_stopped=False)
                return False
            error_code = "channel_provider_start_failed"
            attempt_revision = row.revision
            try:
                ready = await self._configure(instance_id)
            except ChannelRuntimeMaterializationError as exc:
                ready = False
                error_code = exc.code
                # Never leave a previously running adapter on stale secrets
                # when the current exact domain-owned generation cannot load.
                await self._channel_service.remove_channel_instance(str(instance_id))
                self._applied_revisions.pop(instance_id, None)
                self._applied_closures.pop(instance_id, None)
            status_written = await self._set_status(
                instance_id,
                "running" if ready else "error",
                None if ready else error_code,
                expected_revision=(self._applied_revisions.get(instance_id) if ready else attempt_revision),
            )
            if not status_written:
                await self._remove_locked(instance_id, set_stopped=False)
                return False
            return ready

    async def remove(self, instance_id: uuid.UUID) -> bool:
        instance_id = uuid.UUID(str(instance_id))
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            return await self._remove_locked(instance_id, set_stopped=True)

    async def _remove_locked(
        self,
        instance_id: uuid.UUID,
        *,
        set_stopped: bool,
    ) -> bool:
        removed = await self._channel_service.remove_channel_instance(str(instance_id))
        if not removed:
            return False
        task = self._heartbeats.pop(instance_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        claim = self._leases.get(instance_id)
        if set_stopped and claim is not None:
            try:
                await self._set_status(instance_id, "stopped", None)
            except Exception:
                # A lost/expired lease must not prevent local fail-closed stop.
                pass
        claim = self._leases.pop(instance_id, None)
        self._applied_revisions.pop(instance_id, None)
        self._applied_closures.pop(instance_id, None)
        if claim is not None:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._repository.release_instance_lease(
                        session,
                        instance_id,
                        claim.holder_id,
                        claim.lease_token,
                        claim.fencing_generation,
                    )
        return bool(removed)

    async def _configure(self, instance_id: uuid.UUID) -> bool:
        try:
            materialized = await self._materializer.load(instance_id)
            ready = bool(
                await self._channel_service.configure_channel_instance(
                    str(instance_id),
                    materialized.provider,
                    dict(materialized.config),
                )
            )
            if ready:
                revision = getattr(materialized, "instance_revision", 0)
                if not revision:
                    row = await self._get_instance(instance_id)
                    revision = int(getattr(row, "revision", 0))
                self._applied_revisions[instance_id] = revision
                closure = getattr(materialized, "closure", None)
                if closure is not None and closure[0]:
                    self._applied_closures[instance_id] = closure
                else:
                    self._applied_closures.pop(instance_id, None)
            else:
                self._applied_revisions.pop(instance_id, None)
                self._applied_closures.pop(instance_id, None)
            return ready
        except ChannelRuntimeMaterializationError:
            raise
        except Exception:
            return False

    async def _get_instance(self, instance_id: uuid.UUID):
        async with self._session_factory() as session:
            async with session.begin():
                return await self._repository.get_instance(
                    session,
                    instance_id,
                    include_deleted=True,
                )

    async def _lease_authorized(
        self,
        provider: str,
        channel_instance_id: str,
    ) -> bool:
        try:
            instance_id = uuid.UUID(channel_instance_id)
        except (TypeError, ValueError):
            return False
        claim = self._leases.get(instance_id)
        if claim is None:
            return False
        closure = self._applied_closures.get(instance_id)
        if closure is None:
            return False
        instance_revision, secret_revision, secret_generation_id = closure
        if secret_generation_id is None:
            return False
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    return await self._repository.is_instance_lease_authorized(
                        session,
                        channel_instance_id=instance_id,
                        provider=provider,
                        holder_id=claim.holder_id,
                        lease_token=claim.lease_token,
                        fencing_generation=claim.fencing_generation,
                        expected_revision=instance_revision,
                        secret_revision=secret_revision,
                        secret_generation_id=secret_generation_id,
                    )
        except Exception:
            return False

    async def _set_status(
        self,
        instance_id: uuid.UUID,
        observed_status: str,
        last_error_code: str | None,
        *,
        expected_revision: int | None = None,
    ) -> bool:
        claim = self._leases.get(instance_id)
        if claim is None:
            return False
        async with self._session_factory() as session:
            async with session.begin():
                updated = await self._repository.set_observed_status_with_lease(
                    session,
                    channel_instance_id=instance_id,
                    observed_status=observed_status,
                    last_error_code=last_error_code,
                    holder_id=claim.holder_id,
                    lease_token=claim.lease_token,
                    fencing_generation=claim.fencing_generation,
                    expected_revision=expected_revision,
                )
        return updated is not None

    def _ensure_heartbeat(self, instance_id: uuid.UUID) -> None:
        if not self._start_heartbeat_tasks or instance_id in self._heartbeats:
            return
        self._heartbeats[instance_id] = asyncio.create_task(
            self._heartbeat(instance_id),
            name=f"project-channel-heartbeat-{instance_id}",
        )

    async def _heartbeat(self, instance_id: uuid.UUID) -> None:
        try:
            while not self._stopping:
                await asyncio.sleep(_HEARTBEAT_SECONDS)
                if not await self._heartbeat_once(instance_id):
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            lock = self._locks.setdefault(instance_id, asyncio.Lock())
            async with lock:
                try:
                    await self._remove_locked(instance_id, set_stopped=False)
                finally:
                    self._leases.pop(instance_id, None)
                    self._applied_revisions.pop(instance_id, None)
                    self._applied_closures.pop(instance_id, None)
        finally:
            if self._heartbeats.get(instance_id) is asyncio.current_task():
                self._heartbeats.pop(instance_id, None)

    async def _heartbeat_once(self, instance_id: uuid.UUID) -> bool:
        claim = self._leases.get(instance_id)
        if claim is None:
            try:
                await self.reconcile(instance_id)
                return True
            except Exception:
                return True
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    renewed = await self._repository.renew_instance_lease(
                        session,
                        instance_id,
                        claim.holder_id,
                        claim.lease_token,
                        claim.fencing_generation,
                        _LEASE_TTL_SECONDS,
                    )
        except Exception:
            # A supervisor that cannot prove lease renewal must stop locally;
            # keeping a long-lived adapter alive would bypass fencing.
            lock = self._locks.setdefault(instance_id, asyncio.Lock())
            async with lock:
                try:
                    removed = await self._remove_locked(
                        instance_id,
                        set_stopped=False,
                    )
                except Exception:
                    removed = False
                if not removed:
                    self._leases.pop(instance_id, None)
                    self._applied_revisions.pop(instance_id, None)
                    self._applied_closures.pop(instance_id, None)
            return False
        if renewed is None:
            lock = self._locks.setdefault(instance_id, asyncio.Lock())
            async with lock:
                removed = await self._remove_locked(instance_id, set_stopped=True)
                if not removed:
                    self._leases.pop(instance_id, None)
                    self._applied_revisions.pop(instance_id, None)
                    self._applied_closures.pop(instance_id, None)
            return False
        self._leases[instance_id] = renewed

        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        if lock.locked():
            # Management/startup work owns the instance lock. Renewal stays
            # independent so a slow provider handshake cannot expire the
            # lease; health repair is retried on the next heartbeat.
            return True

        row = await self._get_instance(instance_id)
        current_revision = int(getattr(row, "revision", 0)) if row is not None else 0
        status = self._channel_service.get_channel_instance_status(str(instance_id))
        applied_revision = self._applied_revisions.get(instance_id)
        if applied_revision is None:
            closure = self._applied_closures.get(instance_id)
            applied_revision = closure[0] if closure is not None else None
        needs_reconfigure = row is None or row.deleted_at is not None or row.desired_status != "enabled" or not isinstance(status, Mapping) or status.get("running") is not True or applied_revision != current_revision
        if not needs_reconfigure:
            return True

        async with lock:
            # Reconcile may have removed or replaced the lease while this
            # heartbeat waited for the local instance lock.
            active_claim = self._leases.get(instance_id)
            if active_claim is None or active_claim.lease_token != renewed.lease_token or active_claim.fencing_generation != renewed.fencing_generation:
                return False
            row = await self._get_instance(instance_id)
            if row is None or row.deleted_at is not None or row.desired_status != "enabled":
                await self._remove_locked(instance_id, set_stopped=True)
                return False
            if not await self._set_status(
                instance_id,
                "starting",
                None,
                expected_revision=row.revision,
            ):
                await self._remove_locked(instance_id, set_stopped=False)
                return False
            error_code = "channel_provider_start_failed"
            attempt_revision = row.revision
            try:
                ready = await asyncio.wait_for(
                    self._configure(instance_id),
                    timeout=_LEASE_TTL_SECONDS - _HEARTBEAT_SECONDS,
                )
            except ChannelRuntimeMaterializationError as exc:
                ready = False
                error_code = exc.code
                await self._channel_service.remove_channel_instance(str(instance_id))
                self._applied_revisions.pop(instance_id, None)
                self._applied_closures.pop(instance_id, None)
            except TimeoutError:
                ready = False
                await self._channel_service.remove_channel_instance(str(instance_id))
                self._applied_revisions.pop(instance_id, None)
                self._applied_closures.pop(instance_id, None)
            if not await self._set_status(
                instance_id,
                "running" if ready else "error",
                None if ready else error_code,
                expected_revision=(self._applied_revisions.get(instance_id) if ready else attempt_revision),
            ):
                await self._remove_locked(instance_id, set_stopped=False)
                return False
        return True


__all__ = [
    "ChannelRuntimeMaterializationError",
    "ProjectChannelSecretMaterializer",
    "ProjectChannelRuntimeConfig",
    "ProjectChannelRuntimeCoordinator",
]
