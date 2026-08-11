"""Durable, private cleanup authority for isolated Workflow Code resources.

This module is an application-internal boundary.  It deliberately does not
define public DTOs and never logs lease tokens, encrypted provider locators, or
provider resource identifiers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import struct
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from deerflow.workflows.code_execution import (
    CodeActivationIdentity,
    CodeCleanupReceipt,
    CodeExecutionCompletion,
    CodeExecutionControl,
    CodeProvisioningHandle,
    IsolatedCodeCleanupPending,
    IsolatedCodeExecutionLease,
    IsolatedCodeExecutionRequest,
    IsolatedCodeExecutionResult,
)

_TOKEN_MIN_BYTES = 32
_TOKEN_MAX_BYTES = 512
_MAX_LOCATOR_BYTES = 64 * 1024
_KEY_BYTES = 32
_NONCE_BYTES = 12
_TAG_BYTES = 16
_LOCATOR_MAGIC = b"AWCL"
_LOCATOR_VERSION = 1
_LOCATOR_PREFIX = struct.Struct("!4sBH")


class WorkflowCodeLeaseConflict(RuntimeError):
    """Stable, secret-free rejection for a stale or conflicting fence."""

    def __init__(self) -> None:
        super().__init__("workflow code lease conflict")


class WorkflowCodeLocatorInvalid(ValueError):
    """Stable failure for invalid plaintext/context/keyring inputs."""

    def __init__(self) -> None:
        super().__init__("workflow code locator invalid")


class WorkflowCodeLocatorDecryptFailed(RuntimeError):
    """Stable failure for every unreadable or wrongly scoped locator."""

    def __init__(self) -> None:
        super().__init__("workflow code locator decryption failed")


class WorkflowCodeLeaseState(StrEnum):
    PROVISIONING = "provisioning"
    RUNNING = "running"
    CLEANUP_PENDING = "cleanup_pending"
    DESTROYED = "destroyed"


def _strict_positive_int(value: object, *, maximum: int | None = None) -> int:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        raise TypeError("expected a bounded positive integer")
    return value


def _aware_utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError("expected an aware datetime")
    return value.astimezone(UTC)


def _opaque_text(value: object, *, maximum: int = 512) -> str:
    if type(value) is not str or not value or len(value) > maximum or value.strip() != value:
        raise TypeError("expected bounded opaque text")
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _raw_token(value: object) -> str:
    value = _opaque_text(value, maximum=_TOKEN_MAX_BYTES)
    if len(value.encode("utf-8")) < _TOKEN_MIN_BYTES:
        raise TypeError("expected a strong opaque token")
    return value


def _sha256(value: object) -> str:
    value = _opaque_text(value, maximum=64)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise TypeError("expected a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class WorkflowCodeExecutionFence:
    workflow_run_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    origin_trace_id: str
    job_id: uuid.UUID
    workflow_epoch: int
    job_attempt_number: int
    worker_id: uuid.UUID
    profile_digest: str
    raw_job_lease_token: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (
            self.workflow_run_id,
            self.project_id,
            self.job_id,
            self.worker_id,
        ):
            if not isinstance(value, uuid.UUID):
                raise TypeError("expected UUID execution coordinates")
        _opaque_text(self.owner_user_id, maximum=36)
        _opaque_text(self.origin_trace_id)
        _strict_positive_int(self.workflow_epoch)
        _strict_positive_int(self.job_attempt_number)
        _sha256(self.profile_digest)
        _raw_token(self.raw_job_lease_token)

    @property
    def lease_token_hash(self) -> str:
        return _digest(self.raw_job_lease_token)

    def with_raw_job_lease_token(self, token: str) -> WorkflowCodeExecutionFence:
        return replace(self, raw_job_lease_token=token)

    def with_workflow_epoch(self, epoch: int) -> WorkflowCodeExecutionFence:
        return replace(self, workflow_epoch=epoch)

    def with_job_attempt_number(self, attempt: int) -> WorkflowCodeExecutionFence:
        return replace(self, job_attempt_number=attempt)

    def with_worker_id(self, worker_id: uuid.UUID) -> WorkflowCodeExecutionFence:
        return replace(self, worker_id=worker_id)


@dataclass(frozen=True, slots=True)
class WorkflowCodeLocatorContext:
    lease_row_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    workflow_run_id: uuid.UUID
    node_id: str
    activation_id: str
    activation_attempt: int
    profile_digest: str

    def __post_init__(self) -> None:
        for value in (self.lease_row_id, self.project_id, self.workflow_run_id):
            if not isinstance(value, uuid.UUID):
                raise WorkflowCodeLocatorInvalid
        try:
            _opaque_text(self.owner_user_id, maximum=36)
            _opaque_text(self.node_id, maximum=128)
            _opaque_text(self.activation_id, maximum=128)
            _strict_positive_int(self.activation_attempt, maximum=1_000_000)
            _sha256(self.profile_digest)
        except TypeError:
            raise WorkflowCodeLocatorInvalid from None

    def aad(self) -> bytes:
        payload = {
            "activation_attempt": self.activation_attempt,
            "activation_id": self.activation_id,
            "lease_row_id": str(self.lease_row_id),
            "node_id": self.node_id,
            "owner_user_id": self.owner_user_id,
            "profile_digest": self.profile_digest,
            "project_id": str(self.project_id),
            "workflow_run_id": str(self.workflow_run_id),
        }
        return b"actweave-workflow-code-locator:v1:" + json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


@dataclass(frozen=True, slots=True)
class WorkflowCodeLocatorKeyring:
    active_key_id: str
    _keys: Mapping[str, bytes] = field(repr=False)

    def __post_init__(self) -> None:
        try:
            active = _opaque_text(self.active_key_id, maximum=255)
            keys = dict(self._keys)
            if active not in keys or not keys:
                raise ValueError
            for key_id, key in keys.items():
                _opaque_text(key_id, maximum=255)
                if type(key) is not bytes or len(key) != _KEY_BYTES:
                    raise ValueError
        except (TypeError, ValueError):
            raise WorkflowCodeLocatorInvalid from None
        object.__setattr__(self, "_keys", MappingProxyType(keys))

    @property
    def active_key(self) -> bytes:
        return self._keys[self.active_key_id]

    def key_for(self, key_id: str) -> bytes:
        try:
            return self._keys[key_id]
        except (KeyError, TypeError):
            raise WorkflowCodeLocatorDecryptFailed from None


@runtime_checkable
class WorkflowCodeCleanupCodec(Protocol):
    def seal(
        self,
        lease: IsolatedCodeExecutionLease,
        context: WorkflowCodeLocatorContext,
    ) -> bytes: ...

    def open(
        self,
        ciphertext: bytes,
        context: WorkflowCodeLocatorContext,
    ) -> IsolatedCodeExecutionLease: ...


class AesGcmWorkflowCodeCleanupCodec:
    """Authenticated, versioned envelope for the private provider locator."""

    def __init__(self, keyring: WorkflowCodeLocatorKeyring) -> None:
        if not isinstance(keyring, WorkflowCodeLocatorKeyring):
            raise TypeError("keyring must be WorkflowCodeLocatorKeyring")
        self._keyring = keyring

    def seal(
        self,
        lease: IsolatedCodeExecutionLease,
        context: WorkflowCodeLocatorContext,
    ) -> bytes:
        if not isinstance(lease, IsolatedCodeExecutionLease):
            raise TypeError("lease must be IsolatedCodeExecutionLease")
        if not isinstance(context, WorkflowCodeLocatorContext):
            raise TypeError("context must be WorkflowCodeLocatorContext")
        if lease.profile_digest != context.profile_digest:
            raise WorkflowCodeLocatorInvalid
        expected_activation_digest = CodeActivationIdentity(
            project_id=str(context.project_id),
            owner_user_id=context.owner_user_id,
            workflow_run_id=str(context.workflow_run_id),
            node_id=context.node_id,
            activation_id=context.activation_id,
            attempt=context.activation_attempt,
        ).digest()
        if lease.activation_digest != expected_activation_digest:
            raise WorkflowCodeLocatorInvalid
        plaintext = json.dumps(lease.model_dump(mode="json"), separators=(",", ":"), sort_keys=True).encode("utf-8")
        key_id = self._keyring.active_key_id.encode("utf-8")
        nonce = os.urandom(_NONCE_BYTES)
        encrypted = AESGCM(self._keyring.active_key).encrypt(nonce, plaintext, context.aad())
        envelope = _LOCATOR_PREFIX.pack(_LOCATOR_MAGIC, _LOCATOR_VERSION, len(key_id))
        envelope += key_id + nonce + encrypted
        if len(envelope) > _MAX_LOCATOR_BYTES:
            raise WorkflowCodeLocatorInvalid
        return envelope

    def open(
        self,
        ciphertext: bytes,
        context: WorkflowCodeLocatorContext,
    ) -> IsolatedCodeExecutionLease:
        try:
            if type(ciphertext) is not bytes or not isinstance(context, WorkflowCodeLocatorContext):
                raise ValueError
            if len(ciphertext) < _LOCATOR_PREFIX.size + 1 + _NONCE_BYTES + _TAG_BYTES:
                raise ValueError
            magic, version, key_id_size = _LOCATOR_PREFIX.unpack_from(ciphertext)
            if magic != _LOCATOR_MAGIC or version != _LOCATOR_VERSION or key_id_size < 1:
                raise ValueError
            key_start = _LOCATOR_PREFIX.size
            nonce_start = key_start + key_id_size
            cipher_start = nonce_start + _NONCE_BYTES
            if cipher_start + _TAG_BYTES > len(ciphertext):
                raise ValueError
            key_id = ciphertext[key_start:nonce_start].decode("utf-8")
            nonce = ciphertext[nonce_start:cipher_start]
            plaintext = AESGCM(self._keyring.key_for(key_id)).decrypt(nonce, ciphertext[cipher_start:], context.aad())
            payload = json.loads(plaintext.decode("utf-8"))
            lease = IsolatedCodeExecutionLease.model_validate(payload, strict=True)
            if self.seal_validation(lease, context) is not True:
                raise ValueError
            canonical = json.dumps(lease.model_dump(mode="json"), separators=(",", ":"), sort_keys=True).encode("utf-8")
            if plaintext != canonical:
                raise ValueError
            return lease
        except Exception:
            raise WorkflowCodeLocatorDecryptFailed from None

    @staticmethod
    def seal_validation(
        lease: IsolatedCodeExecutionLease,
        context: WorkflowCodeLocatorContext,
    ) -> bool:
        expected = CodeActivationIdentity(
            project_id=str(context.project_id),
            owner_user_id=context.owner_user_id,
            workflow_run_id=str(context.workflow_run_id),
            node_id=context.node_id,
            activation_id=context.activation_id,
            attempt=context.activation_attempt,
        ).digest()
        return lease.activation_digest == expected and lease.profile_digest == context.profile_digest


@dataclass(frozen=True, slots=True)
class WorkflowCodeLeaseRecord:
    id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: str
    workflow_run_id: uuid.UUID
    node_id: str
    activation_id: str
    activation_attempt: int
    job_id: uuid.UUID
    workflow_epoch: int
    job_attempt_number: int
    worker_id: uuid.UUID
    reconciliation_key_hash: str = field(repr=False)
    profile_digest: str
    state: WorkflowCodeLeaseState
    execution_lease_token_hash: str | None = field(repr=False)
    cleanup_locator_ciphertext: bytes | None = field(repr=False)
    cleanup_deadline: datetime
    cleanup_owner_worker_id: uuid.UUID | None
    cleanup_lease_token_hash: str | None = field(repr=False)
    cleanup_lease_expires_at: datetime | None
    cleanup_attempt: int
    destroyed_at: datetime | None
    created_at: datetime

    @property
    def locator_context(self) -> WorkflowCodeLocatorContext:
        return WorkflowCodeLocatorContext(
            lease_row_id=self.id,
            project_id=self.project_id,
            owner_user_id=self.owner_user_id,
            workflow_run_id=self.workflow_run_id,
            node_id=self.node_id,
            activation_id=self.activation_id,
            activation_attempt=self.activation_attempt,
            profile_digest=self.profile_digest,
        )


@dataclass(frozen=True, slots=True)
class WorkflowCodeCleanupClaim:
    record: WorkflowCodeLeaseRecord
    raw_cleanup_token: str = field(repr=False)
    authority_observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WorkflowCodeProvisioningReservation:
    record: WorkflowCodeLeaseRecord
    provider_handle: CodeProvisioningHandle = field(repr=False)


@dataclass(frozen=True, slots=True)
class WorkflowCodeCleanupPending:
    record: WorkflowCodeLeaseRecord


_RECORD_COLUMNS = """
    id, project_id, owner_user_id, workflow_run_id, node_id, activation_id,
    activation_attempt, job_id, workflow_epoch, job_attempt_number, worker_id,
    reconciliation_key_hash,
    profile_digest, state, execution_lease_token_hash,
    cleanup_locator_ciphertext, cleanup_deadline, cleanup_owner_worker_id,
    cleanup_lease_token_hash, cleanup_lease_expires_at, cleanup_attempt,
    destroyed_at, created_at
"""
_ALIASED_RECORD_COLUMNS = """
    l.id, l.project_id, l.owner_user_id, l.workflow_run_id, l.node_id,
    l.activation_id, l.activation_attempt, l.job_id, l.workflow_epoch,
    l.job_attempt_number, l.worker_id, l.reconciliation_key_hash,
    l.profile_digest, l.state,
    l.execution_lease_token_hash, l.cleanup_locator_ciphertext,
    l.cleanup_deadline, l.cleanup_owner_worker_id,
    l.cleanup_lease_token_hash, l.cleanup_lease_expires_at,
    l.cleanup_attempt, l.destroyed_at, l.created_at
"""


class PostgresWorkflowCodeLeaseStore:
    def __init__(self, engine: AsyncEngine) -> None:
        if not isinstance(engine, AsyncEngine):
            raise TypeError("engine must be AsyncEngine")
        self._engine = engine

    async def begin_provisioning(
        self,
        fence: WorkflowCodeExecutionFence,
        activation: CodeActivationIdentity,
        *,
        profile_digest: str,
        cleanup_deadline: datetime,
        _reconciliation_key: str | None = None,
    ) -> WorkflowCodeLeaseRecord:
        self._validate_activation(fence, activation, profile_digest)
        deadline = _aware_utc(cleanup_deadline)
        lease_id = uuid.uuid4()
        reconciliation_key = secrets.token_urlsafe(32) if _reconciliation_key is None else _raw_token(_reconciliation_key)
        try:
            async with self._engine.begin() as connection:
                await self._lock_current_execution_authority(connection, fence)
                result = await connection.execute(
                    text(
                        f"""INSERT INTO workflow_code_sandbox_leases (
                            id, project_id, owner_user_id, workflow_run_id, node_id,
                            activation_id, activation_attempt, job_id, workflow_epoch,
                            job_attempt_number, worker_id, reconciliation_key_hash,
                            profile_digest, state,
                            execution_lease_token_hash, cleanup_deadline, created_at, updated_at
                        ) VALUES (
                            :id, :project, :owner, :run, :node, :activation,
                            :activation_attempt, :job, :epoch, :job_attempt, :worker,
                            :reconciliation_hash, :profile, 'provisioning', :token_hash,
                            :deadline, clock_timestamp(), clock_timestamp()
                        ) RETURNING {_RECORD_COLUMNS}"""
                    ),
                    {
                        "id": lease_id,
                        "project": fence.project_id,
                        "owner": fence.owner_user_id,
                        "run": fence.workflow_run_id,
                        "node": uuid.UUID(activation.node_id),
                        "activation": activation.activation_id,
                        "activation_attempt": activation.attempt,
                        "job": fence.job_id,
                        "epoch": fence.workflow_epoch,
                        "job_attempt": fence.job_attempt_number,
                        "worker": fence.worker_id,
                        "reconciliation_hash": _digest(reconciliation_key),
                        "profile": profile_digest,
                        "token_hash": fence.lease_token_hash,
                        "deadline": deadline,
                    },
                )
                return self._record(result.mappings().one())
        except WorkflowCodeLeaseConflict:
            raise
        except Exception as exc:
            # Uniqueness/FK/check failures are intentionally flattened: SQL text and
            # driver detail can otherwise reveal private authority coordinates.
            raise WorkflowCodeLeaseConflict from exc

    async def reserve_provisioning(
        self,
        fence: WorkflowCodeExecutionFence,
        activation: CodeActivationIdentity,
        *,
        profile_digest: str,
        cleanup_deadline: datetime,
    ) -> WorkflowCodeProvisioningReservation:
        """Commit a server-owned handle before provider acquisition begins."""

        raw_key = secrets.token_urlsafe(32)
        record = await self.begin_provisioning(
            fence,
            activation,
            profile_digest=profile_digest,
            cleanup_deadline=cleanup_deadline,
            _reconciliation_key=raw_key,
        )
        return WorkflowCodeProvisioningReservation(
            record=record,
            provider_handle=CodeProvisioningHandle(
                lease_id=str(record.id),
                reconciliation_key=raw_key,
            ),
        )

    async def activate(
        self,
        lease_id: uuid.UUID,
        fence: WorkflowCodeExecutionFence,
        *,
        cleanup_locator_ciphertext: bytes,
    ) -> WorkflowCodeLeaseRecord:
        self._lease_id(lease_id)
        locator = self._locator(cleanup_locator_ciphertext)
        async with self._engine.begin() as connection:
            await self._lock_current_execution_authority(connection, fence)
            record = await self._lock_record(connection, lease_id)
            self._require_execution_record(record, fence, WorkflowCodeLeaseState.PROVISIONING)
            result = await connection.execute(
                text(
                    f"""UPDATE workflow_code_sandbox_leases
                        SET state='running', cleanup_locator_ciphertext=:locator,
                            updated_at=clock_timestamp()
                        WHERE id=:id RETURNING {_RECORD_COLUMNS}"""
                ),
                {"id": lease_id, "locator": locator},
            )
            return self._record(result.mappings().one())

    async def begin_cleanup(
        self,
        lease_id: uuid.UUID,
        fence: WorkflowCodeExecutionFence,
        *,
        cleanup_worker_id: uuid.UUID | None = None,
        cleanup_lease_seconds: int = 30,
    ) -> WorkflowCodeCleanupClaim:
        self._lease_id(lease_id)
        owner = cleanup_worker_id if cleanup_worker_id is not None else fence.worker_id
        if not isinstance(owner, uuid.UUID):
            raise TypeError("cleanup_worker_id must be UUID")
        duration = _strict_positive_int(cleanup_lease_seconds, maximum=3600)
        raw_token = secrets.token_urlsafe(32)
        async with self._engine.begin() as connection:
            await self._lock_current_execution_authority(connection, fence)
            record = await self._lock_record(connection, lease_id)
            self._require_execution_record(record, fence, WorkflowCodeLeaseState.RUNNING)
            result = await connection.execute(
                text(
                    f"""UPDATE workflow_code_sandbox_leases
                        SET state='cleanup_pending', execution_lease_token_hash=NULL,
                            cleanup_handoff_at=clock_timestamp(), cleanup_owner_worker_id=:owner,
                            cleanup_lease_token_hash=:cleanup_hash,
                            cleanup_lease_expires_at=clock_timestamp()
                                + make_interval(secs => :duration),
                            cleanup_attempt=cleanup_attempt + 1,
                            updated_at=clock_timestamp()
                        WHERE id=:id RETURNING {_RECORD_COLUMNS},
                            clock_timestamp() AS authority_observed_at"""
                ),
                {
                    "id": lease_id,
                    "owner": owner,
                    "cleanup_hash": _digest(raw_token),
                    "duration": duration,
                },
            )
            claimed = result.mappings().one()
            return WorkflowCodeCleanupClaim(
                self._record(claimed),
                raw_token,
                claimed["authority_observed_at"],
            )

    async def handoff_running_resource(
        self,
        lease_id: uuid.UUID,
        *,
        raw_reconciliation_key: str,
        cleanup_worker_id: uuid.UUID,
        cleanup_lease_seconds: int,
    ) -> WorkflowCodeCleanupClaim:
        """Irrevocably hand a running row to cleanup after execution fence loss."""

        self._lease_id(lease_id)
        reconciliation_hash = _digest(_raw_token(raw_reconciliation_key))
        if not isinstance(cleanup_worker_id, uuid.UUID):
            raise TypeError("cleanup_worker_id must be UUID")
        duration = _strict_positive_int(cleanup_lease_seconds, maximum=3600)
        raw_token = secrets.token_urlsafe(32)
        async with self._engine.begin() as connection:
            snapshot = await self._read_record(connection, lease_id)
            await self._lock_cleanup_scope(connection, snapshot)
            record = await self._lock_record(connection, lease_id)
            if record.state is not WorkflowCodeLeaseState.RUNNING or not secrets.compare_digest(record.reconciliation_key_hash, reconciliation_hash):
                raise WorkflowCodeLeaseConflict
            result = await connection.execute(
                text(
                    f"""UPDATE workflow_code_sandbox_leases
                        SET state='cleanup_pending', execution_lease_token_hash=NULL,
                            cleanup_handoff_at=clock_timestamp(),
                            cleanup_owner_worker_id=:owner,
                            cleanup_lease_token_hash=:cleanup_hash,
                            cleanup_lease_expires_at=clock_timestamp()
                                + make_interval(secs => :duration),
                            cleanup_attempt=cleanup_attempt + 1,
                            updated_at=clock_timestamp()
                        WHERE id=:id RETURNING {_RECORD_COLUMNS},
                            clock_timestamp() AS authority_observed_at"""
                ),
                {
                    "id": lease_id,
                    "owner": cleanup_worker_id,
                    "cleanup_hash": _digest(raw_token),
                    "duration": duration,
                },
            )
            claimed = result.mappings().one()
            return WorkflowCodeCleanupClaim(
                self._record(claimed),
                raw_token,
                claimed["authority_observed_at"],
            )

    async def quarantine_acquired_resource(
        self,
        lease_id: uuid.UUID,
        *,
        raw_reconciliation_key: str,
        cleanup_locator_ciphertext: bytes | None,
        cleanup_worker_id: uuid.UUID,
        cleanup_lease_seconds: int,
    ) -> WorkflowCodeCleanupClaim:
        """Make an acquired resource durable when running activation loses its fence."""

        self._lease_id(lease_id)
        reconciliation_hash = _digest(_raw_token(raw_reconciliation_key))
        locator = None if cleanup_locator_ciphertext is None else self._locator(cleanup_locator_ciphertext)
        if not isinstance(cleanup_worker_id, uuid.UUID):
            raise TypeError("cleanup_worker_id must be UUID")
        duration = _strict_positive_int(cleanup_lease_seconds, maximum=3600)
        raw_token = secrets.token_urlsafe(32)
        async with self._engine.begin() as connection:
            snapshot = await self._read_record(connection, lease_id)
            await self._lock_cleanup_scope(connection, snapshot)
            record = await self._lock_record(connection, lease_id)
            if record.state is not WorkflowCodeLeaseState.PROVISIONING or not secrets.compare_digest(record.reconciliation_key_hash, reconciliation_hash):
                raise WorkflowCodeLeaseConflict
            result = await connection.execute(
                text(
                    f"""UPDATE workflow_code_sandbox_leases
                        SET state='cleanup_pending', execution_lease_token_hash=NULL,
                            cleanup_locator_ciphertext=:locator,
                            cleanup_handoff_at=clock_timestamp(), cleanup_owner_worker_id=:owner,
                            cleanup_lease_token_hash=:cleanup_hash,
                            cleanup_lease_expires_at=clock_timestamp()
                                + make_interval(secs => :duration),
                            cleanup_attempt=cleanup_attempt + 1,
                            updated_at=clock_timestamp()
                        WHERE id=:id RETURNING {_RECORD_COLUMNS},
                            clock_timestamp() AS authority_observed_at"""
                ),
                {
                    "id": lease_id,
                    "locator": locator,
                    "owner": cleanup_worker_id,
                    "cleanup_hash": _digest(raw_token),
                    "duration": duration,
                },
            )
            claimed = result.mappings().one()
            return WorkflowCodeCleanupClaim(
                self._record(claimed),
                raw_token,
                claimed["authority_observed_at"],
            )

    async def claim_cleanup_pending(
        self,
        *,
        cleanup_worker_id: uuid.UUID,
        cleanup_lease_seconds: int,
    ) -> WorkflowCodeCleanupClaim | None:
        if not isinstance(cleanup_worker_id, uuid.UUID):
            raise TypeError("cleanup_worker_id must be UUID")
        duration = _strict_positive_int(cleanup_lease_seconds, maximum=3600)
        raw_token = secrets.token_urlsafe(32)
        async with self._engine.begin() as connection:
            candidate = await connection.execute(
                text(
                    f"""SELECT {_ALIASED_RECORD_COLUMNS}
                        FROM workflow_code_sandbox_leases l
                        JOIN workflow_runs wr ON wr.id=l.workflow_run_id
                            AND wr.project_id=l.project_id AND wr.owner_user_id=l.owner_user_id
                        JOIN workflow_run_jobs m ON m.workflow_run_id=l.workflow_run_id
                            AND m.execution_epoch=l.workflow_epoch AND m.job_id=l.job_id
                        JOIN jobs j ON j.id=l.job_id
                        JOIN job_attempts ja ON ja.job_id=l.job_id
                            AND ja.attempt_number=l.job_attempt_number
                        WHERE (
                            l.state='cleanup_pending' AND l.cleanup_handoff_at IS NOT NULL
                            AND (l.cleanup_owner_worker_id IS NULL
                                OR l.cleanup_lease_expires_at <= CURRENT_TIMESTAMP)
                        ) OR (
                            l.state IN ('provisioning','running') AND (
                                wr.status <> 'running'
                                OR wr.current_job_id IS DISTINCT FROM l.job_id
                                OR wr.execution_epoch <> l.workflow_epoch
                                OR j.status NOT IN ('leased','running')
                                OR j.cancel_requested_at IS NOT NULL
                                OR j.lease_owner_id IS DISTINCT FROM l.worker_id
                                OR j.lease_token_hash IS DISTINCT FROM l.execution_lease_token_hash
                                OR j.lease_expires_at <= CURRENT_TIMESTAMP
                                OR ja.worker_id IS DISTINCT FROM l.worker_id
                                OR ja.lease_token_hash IS DISTINCT FROM l.execution_lease_token_hash
                                OR ja.outcome IS NOT NULL
                            )
                        )
                        ORDER BY l.created_at, l.id
                        FOR UPDATE OF l, wr, m, j, ja SKIP LOCKED
                        LIMIT 1"""
                ),
                {},
            )
            mapping = candidate.mappings().one_or_none()
            if mapping is None:
                return None
            record = self._record(mapping)
            result = await connection.execute(
                text(
                    f"""UPDATE workflow_code_sandbox_leases
                        SET state='cleanup_pending', execution_lease_token_hash=NULL,
                            cleanup_handoff_at=COALESCE(cleanup_handoff_at,clock_timestamp()),
                            cleanup_owner_worker_id=:owner,
                            cleanup_lease_token_hash=:cleanup_hash,
                            cleanup_lease_expires_at=clock_timestamp()
                                + make_interval(secs => :duration),
                            cleanup_attempt=cleanup_attempt + 1,
                            updated_at=clock_timestamp()
                        WHERE id=:id RETURNING {_RECORD_COLUMNS},
                            clock_timestamp() AS authority_observed_at"""
                ),
                {
                    "id": record.id,
                    "owner": cleanup_worker_id,
                    "cleanup_hash": _digest(raw_token),
                    "duration": duration,
                },
            )
            claimed = result.mappings().one()
            return WorkflowCodeCleanupClaim(
                self._record(claimed),
                raw_token,
                claimed["authority_observed_at"],
            )

    async def release_cleanup_claim(
        self,
        lease_id: uuid.UUID,
        *,
        cleanup_worker_id: uuid.UUID,
        raw_cleanup_token: str,
    ) -> WorkflowCodeLeaseRecord:
        return await self._finish_cleanup_claim(
            lease_id,
            cleanup_worker_id=cleanup_worker_id,
            raw_cleanup_token=raw_cleanup_token,
            destroyed=False,
        )

    async def confirm_destroyed(
        self,
        lease_id: uuid.UUID,
        *,
        cleanup_worker_id: uuid.UUID,
        raw_cleanup_token: str,
    ) -> WorkflowCodeLeaseRecord:
        return await self._finish_cleanup_claim(
            lease_id,
            cleanup_worker_id=cleanup_worker_id,
            raw_cleanup_token=raw_cleanup_token,
            destroyed=True,
        )

    async def _finish_cleanup_claim(
        self,
        lease_id: uuid.UUID,
        *,
        cleanup_worker_id: uuid.UUID,
        raw_cleanup_token: str,
        destroyed: bool,
    ) -> WorkflowCodeLeaseRecord:
        self._lease_id(lease_id)
        if not isinstance(cleanup_worker_id, uuid.UUID):
            raise TypeError("cleanup_worker_id must be UUID")
        cleanup_hash = _digest(_raw_token(raw_cleanup_token))
        if type(destroyed) is not bool:
            raise TypeError("destroyed must be bool")
        async with self._engine.begin() as connection:
            snapshot = await self._read_record(connection, lease_id)
            await self._lock_cleanup_scope(connection, snapshot)
            record = await self._lock_record(connection, lease_id)
            if (
                record.state is not WorkflowCodeLeaseState.CLEANUP_PENDING
                or record.cleanup_owner_worker_id != cleanup_worker_id
                or not secrets.compare_digest(record.cleanup_lease_token_hash or "", cleanup_hash)
                or record.cleanup_lease_expires_at is None
            ):
                raise WorkflowCodeLeaseConflict
            # The expiry comparison is part of the same database transaction;
            # process clocks never participate in cleanup authority.
            current = await connection.execute(
                text("SELECT cleanup_lease_expires_at > CURRENT_TIMESTAMP FROM workflow_code_sandbox_leases WHERE id=:id"),
                {"id": lease_id},
            )
            if current.scalar_one() is not True:
                raise WorkflowCodeLeaseConflict
            if destroyed:
                assignment = """state='destroyed', cleanup_locator_ciphertext=NULL,
                    cleanup_handoff_at=NULL, cleanup_owner_worker_id=NULL,
                    cleanup_lease_token_hash=NULL, cleanup_lease_expires_at=NULL,
                    destroyed_at=clock_timestamp(), updated_at=clock_timestamp()"""
            else:
                assignment = """cleanup_owner_worker_id=NULL,
                    cleanup_lease_token_hash=NULL, cleanup_lease_expires_at=NULL,
                    updated_at=clock_timestamp()"""
            result = await connection.execute(
                text(
                    f"""UPDATE workflow_code_sandbox_leases SET {assignment}
                        WHERE id=:id RETURNING {_RECORD_COLUMNS}"""
                ),
                {"id": lease_id},
            )
            return self._record(result.mappings().one())

    async def execution_fence_is_current(
        self,
        fence: WorkflowCodeExecutionFence,
    ) -> bool:
        try:
            async with self._engine.begin() as connection:
                await self._lock_current_execution_authority(connection, fence)
        except WorkflowCodeLeaseConflict:
            return False
        return True

    async def get(self, lease_id: uuid.UUID) -> WorkflowCodeLeaseRecord | None:
        self._lease_id(lease_id)
        async with self._engine.connect() as connection:
            result = await connection.execute(
                text(
                    f"""SELECT {_RECORD_COLUMNS}
                        FROM workflow_code_sandbox_leases WHERE id=:id"""
                ),
                {"id": lease_id},
            )
            mapping = result.mappings().one_or_none()
            return None if mapping is None else self._record(mapping)

    @staticmethod
    def _validate_activation(
        fence: WorkflowCodeExecutionFence,
        activation: CodeActivationIdentity,
        profile_digest: str,
    ) -> None:
        if not isinstance(fence, WorkflowCodeExecutionFence):
            raise TypeError("fence must be WorkflowCodeExecutionFence")
        if not isinstance(activation, CodeActivationIdentity):
            raise TypeError("activation must be CodeActivationIdentity")
        _sha256(profile_digest)
        if activation.project_id != str(fence.project_id) or activation.owner_user_id != fence.owner_user_id or activation.workflow_run_id != str(fence.workflow_run_id) or profile_digest != fence.profile_digest:
            raise WorkflowCodeLeaseConflict

    @staticmethod
    def _lease_id(value: object) -> uuid.UUID:
        if not isinstance(value, uuid.UUID):
            raise TypeError("lease_id must be UUID")
        return value

    @staticmethod
    def _locator(value: object) -> bytes:
        if type(value) is not bytes or not value or len(value) > _MAX_LOCATOR_BYTES:
            raise TypeError("cleanup locator must be bounded ciphertext bytes")
        return value

    @staticmethod
    async def _lock_current_execution_authority(
        connection: AsyncConnection,
        fence: WorkflowCodeExecutionFence,
    ) -> None:
        if not isinstance(fence, WorkflowCodeExecutionFence):
            raise TypeError("fence must be WorkflowCodeExecutionFence")
        result = await connection.execute(
            text(
                """SELECT wr.id
                   FROM workflow_runs wr
                   JOIN workflow_run_jobs m ON m.workflow_run_id=wr.id
                       AND m.execution_epoch=wr.execution_epoch
                       AND m.job_id=wr.current_job_id
                       AND m.project_id=wr.project_id
                       AND m.owner_user_id=wr.owner_user_id
                   JOIN jobs j ON j.id=m.job_id
                   JOIN job_attempts ja ON ja.job_id=j.id
                       AND ja.attempt_number=:job_attempt
                   WHERE wr.id=:run AND wr.project_id=:project
                     AND wr.owner_user_id=:owner AND wr.origin_trace_id=:trace
                     AND wr.status='running'
                     AND wr.execution_epoch=:epoch AND wr.current_job_id=:job
                     AND j.job_type='workflow_run' AND j.project_id=:project
                     AND j.owner_user_id=:owner AND j.workflow_run_id=:run
                     AND j.workflow_epoch=:epoch AND j.origin_trace_id=:trace
                     AND j.required_worker_profile_digest
                         IS NOT DISTINCT FROM wr.required_worker_profile_digest
                     AND wr.required_worker_profile_digest=:profile
                     AND j.required_worker_profile_digest=:profile
                     AND j.status IN ('leased','running')
                     AND j.cancel_requested_at IS NULL
                     AND j.lease_owner_id=:worker AND j.lease_token_hash=:token_hash
                     AND j.lease_expires_at > CURRENT_TIMESTAMP
                     AND ja.worker_id=:worker AND ja.lease_token_hash=:token_hash
                     AND ja.outcome IS NULL
                   FOR UPDATE OF wr, m, j, ja"""
            ),
            {
                "run": fence.workflow_run_id,
                "project": fence.project_id,
                "owner": fence.owner_user_id,
                "trace": fence.origin_trace_id,
                "epoch": fence.workflow_epoch,
                "job": fence.job_id,
                "job_attempt": fence.job_attempt_number,
                "worker": fence.worker_id,
                "token_hash": fence.lease_token_hash,
                "profile": fence.profile_digest,
            },
        )
        if result.first() is None:
            raise WorkflowCodeLeaseConflict

    @staticmethod
    async def _lock_cleanup_scope(
        connection: AsyncConnection,
        record: WorkflowCodeLeaseRecord,
    ) -> None:
        result = await connection.execute(
            text(
                """SELECT wr.id
                   FROM workflow_runs wr
                   JOIN workflow_run_jobs m ON m.workflow_run_id=wr.id
                       AND m.execution_epoch=:epoch AND m.job_id=:job
                   JOIN jobs j ON j.id=m.job_id AND j.project_id=:project
                       AND j.owner_user_id=:owner AND j.workflow_run_id=:run
                       AND j.workflow_epoch=:epoch
                   JOIN job_attempts ja ON ja.job_id=j.id
                       AND ja.attempt_number=:job_attempt
                   WHERE wr.id=:run AND wr.project_id=:project
                     AND wr.owner_user_id=:owner
                   FOR UPDATE OF wr, m, j, ja"""
            ),
            {
                "run": record.workflow_run_id,
                "project": record.project_id,
                "owner": record.owner_user_id,
                "epoch": record.workflow_epoch,
                "job": record.job_id,
                "job_attempt": record.job_attempt_number,
            },
        )
        if result.first() is None:
            raise WorkflowCodeLeaseConflict

    @staticmethod
    async def _lock_record(
        connection: AsyncConnection,
        lease_id: uuid.UUID,
    ) -> WorkflowCodeLeaseRecord:
        result = await connection.execute(
            text(
                f"""SELECT {_RECORD_COLUMNS}
                    FROM workflow_code_sandbox_leases WHERE id=:id FOR UPDATE"""
            ),
            {"id": lease_id},
        )
        mapping = result.mappings().one_or_none()
        if mapping is None:
            raise WorkflowCodeLeaseConflict
        return PostgresWorkflowCodeLeaseStore._record(mapping)

    @staticmethod
    async def _read_record(
        connection: AsyncConnection,
        lease_id: uuid.UUID,
    ) -> WorkflowCodeLeaseRecord:
        result = await connection.execute(
            text(
                f"""SELECT {_RECORD_COLUMNS}
                    FROM workflow_code_sandbox_leases WHERE id=:id"""
            ),
            {"id": lease_id},
        )
        mapping = result.mappings().one_or_none()
        if mapping is None:
            raise WorkflowCodeLeaseConflict
        return PostgresWorkflowCodeLeaseStore._record(mapping)

    @staticmethod
    def _require_execution_record(
        record: WorkflowCodeLeaseRecord,
        fence: WorkflowCodeExecutionFence,
        state: WorkflowCodeLeaseState,
    ) -> None:
        if (
            record.state is not state
            or record.workflow_run_id != fence.workflow_run_id
            or record.project_id != fence.project_id
            or record.owner_user_id != fence.owner_user_id
            or record.job_id != fence.job_id
            or record.workflow_epoch != fence.workflow_epoch
            or record.job_attempt_number != fence.job_attempt_number
            or record.worker_id != fence.worker_id
            or record.profile_digest != fence.profile_digest
            or not secrets.compare_digest(record.execution_lease_token_hash or "", fence.lease_token_hash)
        ):
            raise WorkflowCodeLeaseConflict

    @staticmethod
    def _record(mapping: Mapping[str, object]) -> WorkflowCodeLeaseRecord:
        return WorkflowCodeLeaseRecord(
            id=mapping["id"],
            project_id=mapping["project_id"],
            owner_user_id=mapping["owner_user_id"],
            workflow_run_id=mapping["workflow_run_id"],
            node_id=str(mapping["node_id"]),
            activation_id=mapping["activation_id"],
            activation_attempt=mapping["activation_attempt"],
            job_id=mapping["job_id"],
            workflow_epoch=mapping["workflow_epoch"],
            job_attempt_number=mapping["job_attempt_number"],
            worker_id=mapping["worker_id"],
            reconciliation_key_hash=mapping["reconciliation_key_hash"],
            profile_digest=mapping["profile_digest"],
            state=WorkflowCodeLeaseState(mapping["state"]),
            execution_lease_token_hash=mapping["execution_lease_token_hash"],
            cleanup_locator_ciphertext=mapping["cleanup_locator_ciphertext"],
            cleanup_deadline=mapping["cleanup_deadline"],
            cleanup_owner_worker_id=mapping["cleanup_owner_worker_id"],
            cleanup_lease_token_hash=mapping["cleanup_lease_token_hash"],
            cleanup_lease_expires_at=mapping["cleanup_lease_expires_at"],
            cleanup_attempt=mapping["cleanup_attempt"],
            destroyed_at=mapping["destroyed_at"],
            created_at=mapping["created_at"],
        )


@runtime_checkable
class _CleanupProvider(Protocol):
    def acquire_reserved(
        self,
        request: IsolatedCodeExecutionRequest,
        handle: CodeProvisioningHandle,
    ) -> IsolatedCodeExecutionLease: ...

    def cleanup(
        self,
        lease: IsolatedCodeExecutionLease,
        *,
        reason: str,
    ) -> CodeCleanupReceipt: ...

    def execute(
        self,
        lease: IsolatedCodeExecutionLease,
        request: IsolatedCodeExecutionRequest,
        control: CodeExecutionControl,
    ) -> IsolatedCodeExecutionResult: ...

    def reconcile_provisioning(
        self,
        *,
        lease_id: str,
        reconciliation_key_hash: str,
    ) -> CodeCleanupReceipt: ...

    def release_provisioning_handle(
        self,
        *,
        lease_id: str,
        reconciliation_key_hash: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkflowCodeProvisionedLease:
    record: WorkflowCodeLeaseRecord
    provider_lease: IsolatedCodeExecutionLease = field(repr=False)
    raw_reconciliation_key: str = field(repr=False)


async def _join_non_cancellable(task: asyncio.Task[IsolatedCodeExecutionLease]) -> tuple[IsolatedCodeExecutionLease, bool]:
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


async def _join_none_barrier(task: asyncio.Task[None]) -> bool:
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            return cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


def _release_provider_handle(
    provider: _CleanupProvider,
    record: WorkflowCodeLeaseRecord,
) -> None:
    try:
        provider.release_provisioning_handle(
            lease_id=str(record.id),
            reconciliation_key_hash=record.reconciliation_key_hash,
        )
    except Exception:
        # Database ``destroyed`` is authoritative; local janitor failure does
        # not roll the durable state transition back.
        pass


class WorkflowCodeProvisioningCoordinator:
    """The only durable Worker path into provider ``acquire_reserved``."""

    def __init__(
        self,
        *,
        store: PostgresWorkflowCodeLeaseStore,
        provider: _CleanupProvider,
        codec: WorkflowCodeCleanupCodec,
    ) -> None:
        if not isinstance(store, PostgresWorkflowCodeLeaseStore):
            raise TypeError("store must be PostgresWorkflowCodeLeaseStore")
        if isinstance(provider, bool) or not isinstance(provider, _CleanupProvider):
            raise TypeError("provider must implement durable Workflow Code operations")
        if isinstance(codec, bool) or not isinstance(codec, WorkflowCodeCleanupCodec):
            raise TypeError("codec must implement the cleanup codec")
        self._store = store
        self._provider = provider
        self._codec = codec

    async def reserve_and_acquire(
        self,
        fence: WorkflowCodeExecutionFence,
        request: IsolatedCodeExecutionRequest,
        *,
        cleanup_deadline: datetime,
        cleanup_lease_seconds: int,
    ) -> WorkflowCodeProvisionedLease:
        if not isinstance(request, IsolatedCodeExecutionRequest):
            raise TypeError("request must be IsolatedCodeExecutionRequest")
        reservation = await self._store.reserve_provisioning(
            fence,
            request.activation,
            profile_digest=request.profile_digest,
            cleanup_deadline=cleanup_deadline,
        )
        acquire_task = asyncio.create_task(
            asyncio.to_thread(
                self._provider.acquire_reserved,
                request,
                reservation.provider_handle,
            )
        )
        try:
            provider_lease, cancellation_pending = await _join_non_cancellable(acquire_task)
        except BaseException:
            cancelled = await _join_none_barrier(
                asyncio.create_task(
                    self._reconcile_failed_acquire(
                        reservation,
                        cleanup_worker_id=fence.worker_id,
                        cleanup_lease_seconds=cleanup_lease_seconds,
                    )
                )
            )
            if cancelled:
                raise asyncio.CancelledError
            raise
        try:
            if (
                not isinstance(provider_lease, IsolatedCodeExecutionLease)
                or provider_lease.lease_id != reservation.provider_handle.lease_id
                or provider_lease.activation_digest != request.activation.digest()
                or provider_lease.profile_digest != request.profile_digest
            ):
                raise TypeError("provider returned a lease outside the journaled handle")
            locator = self._codec.seal(
                provider_lease,
                reservation.record.locator_context,
            )
        except BaseException:
            cancelled = await _join_none_barrier(
                asyncio.create_task(
                    self._reconcile_failed_acquire(
                        reservation,
                        cleanup_worker_id=fence.worker_id,
                        cleanup_lease_seconds=cleanup_lease_seconds,
                    )
                )
            )
            if cancelled:
                raise asyncio.CancelledError
            raise
        try:
            running = await self._store.activate(
                reservation.record.id,
                fence,
                cleanup_locator_ciphertext=locator,
            )
        except BaseException:
            cancelled = await _join_none_barrier(
                asyncio.create_task(
                    self._cleanup_failed_activation(
                        reservation,
                        provider_lease,
                        locator,
                        cleanup_worker_id=fence.worker_id,
                        cleanup_lease_seconds=cleanup_lease_seconds,
                        reason="lease_lost",
                    )
                )
            )
            if cancelled:
                raise asyncio.CancelledError
            raise
        if cancellation_pending:
            await _join_none_barrier(
                asyncio.create_task(
                    self._cleanup_running_resource(
                        running,
                        provider_lease,
                        raw_reconciliation_key=reservation.provider_handle.reconciliation_key,
                        fence=fence,
                        cleanup_lease_seconds=cleanup_lease_seconds,
                        reason="cancelled",
                    )
                )
            )
            raise asyncio.CancelledError
        return WorkflowCodeProvisionedLease(
            running,
            provider_lease,
            reservation.provider_handle.reconciliation_key,
        )

    async def _cleanup_failed_activation(
        self,
        reservation: WorkflowCodeProvisioningReservation,
        provider_lease: IsolatedCodeExecutionLease,
        locator: bytes,
        *,
        cleanup_worker_id: uuid.UUID,
        cleanup_lease_seconds: int,
        reason: str,
    ) -> None:
        claim = await self._store.quarantine_acquired_resource(
            reservation.record.id,
            raw_reconciliation_key=reservation.provider_handle.reconciliation_key,
            cleanup_locator_ciphertext=locator,
            cleanup_worker_id=cleanup_worker_id,
            cleanup_lease_seconds=cleanup_lease_seconds,
        )
        await self._settle_cleanup_claim(
            claim,
            provider_lease,
            cleanup_worker_id=cleanup_worker_id,
            reason=reason,
        )

    async def _cleanup_running_resource(
        self,
        record: WorkflowCodeLeaseRecord,
        provider_lease: IsolatedCodeExecutionLease,
        *,
        raw_reconciliation_key: str,
        fence: WorkflowCodeExecutionFence,
        cleanup_lease_seconds: int,
        reason: str,
    ) -> None:
        try:
            claim = await self._store.begin_cleanup(
                record.id,
                fence,
                cleanup_worker_id=fence.worker_id,
                cleanup_lease_seconds=cleanup_lease_seconds,
            )
        except WorkflowCodeLeaseConflict:
            claim = await self._store.handoff_running_resource(
                record.id,
                raw_reconciliation_key=raw_reconciliation_key,
                cleanup_worker_id=fence.worker_id,
                cleanup_lease_seconds=cleanup_lease_seconds,
            )
        await self._settle_cleanup_claim(
            claim,
            provider_lease,
            cleanup_worker_id=fence.worker_id,
            reason=reason,
        )

    async def _settle_cleanup_claim(
        self,
        claim: WorkflowCodeCleanupClaim,
        provider_lease: IsolatedCodeExecutionLease,
        *,
        cleanup_worker_id: uuid.UUID,
        reason: str,
    ) -> None:
        try:
            receipt = await asyncio.to_thread(
                self._provider.cleanup,
                provider_lease,
                reason=reason,
            )
            if not isinstance(receipt, CodeCleanupReceipt) or receipt.lease_id != provider_lease.lease_id:
                raise TypeError("cleanup provider returned an invalid receipt")
        except BaseException:
            await self._store.release_cleanup_claim(
                claim.record.id,
                cleanup_worker_id=cleanup_worker_id,
                raw_cleanup_token=claim.raw_cleanup_token,
            )
            raise
        if receipt.state == "destroyed_confirmed":
            await self._store.confirm_destroyed(
                claim.record.id,
                cleanup_worker_id=cleanup_worker_id,
                raw_cleanup_token=claim.raw_cleanup_token,
            )
            _release_provider_handle(self._provider, claim.record)
        else:
            await self._store.release_cleanup_claim(
                claim.record.id,
                cleanup_worker_id=cleanup_worker_id,
                raw_cleanup_token=claim.raw_cleanup_token,
            )

    async def _reconcile_failed_acquire(
        self,
        reservation: WorkflowCodeProvisioningReservation,
        *,
        cleanup_worker_id: uuid.UUID,
        cleanup_lease_seconds: int,
    ) -> None:
        claim = await self._store.quarantine_acquired_resource(
            reservation.record.id,
            raw_reconciliation_key=reservation.provider_handle.reconciliation_key,
            cleanup_locator_ciphertext=None,
            cleanup_worker_id=cleanup_worker_id,
            cleanup_lease_seconds=cleanup_lease_seconds,
        )
        if claim.authority_observed_at is None:
            raise RuntimeError("cleanup claim lacks database authority time")
        try:
            receipt = await asyncio.shield(
                asyncio.to_thread(
                    self._provider.reconcile_provisioning,
                    lease_id=str(reservation.record.id),
                    reconciliation_key_hash=reservation.record.reconciliation_key_hash,
                )
            )
            if not isinstance(receipt, CodeCleanupReceipt) or receipt.lease_id != str(reservation.record.id):
                raise TypeError("cleanup provider returned an invalid receipt")
        except BaseException:
            await self._store.release_cleanup_claim(
                claim.record.id,
                cleanup_worker_id=cleanup_worker_id,
                raw_cleanup_token=claim.raw_cleanup_token,
            )
            raise
        if receipt.state == "destroyed_confirmed":
            await self._store.confirm_destroyed(
                claim.record.id,
                cleanup_worker_id=cleanup_worker_id,
                raw_cleanup_token=claim.raw_cleanup_token,
            )
            _release_provider_handle(self._provider, claim.record)
        else:
            await self._store.release_cleanup_claim(
                claim.record.id,
                cleanup_worker_id=cleanup_worker_id,
                raw_cleanup_token=claim.raw_cleanup_token,
            )


class WorkflowCodeExecutionCoordinator:
    """Unique journaled execute path with a destroy-before-result barrier."""

    def __init__(
        self,
        *,
        provisioning: WorkflowCodeProvisioningCoordinator,
        store: PostgresWorkflowCodeLeaseStore,
        provider: _CleanupProvider,
    ) -> None:
        if not isinstance(provisioning, WorkflowCodeProvisioningCoordinator):
            raise TypeError("provisioning must be WorkflowCodeProvisioningCoordinator")
        if not isinstance(store, PostgresWorkflowCodeLeaseStore):
            raise TypeError("store must be PostgresWorkflowCodeLeaseStore")
        if isinstance(provider, bool) or not isinstance(provider, _CleanupProvider):
            raise TypeError("provider must implement durable Workflow Code operations")
        self._provisioning = provisioning
        self._store = store
        self._provider = provider

    async def execute(
        self,
        fence: WorkflowCodeExecutionFence,
        request: IsolatedCodeExecutionRequest,
        *,
        cleanup_deadline: datetime,
        cleanup_lease_seconds: int,
    ) -> CodeExecutionCompletion:
        provisioned = await self._provisioning.reserve_and_acquire(
            fence,
            request,
            cleanup_deadline=cleanup_deadline,
            cleanup_lease_seconds=cleanup_lease_seconds,
        )
        current = threading.Event()
        current.set()
        control = CodeExecutionControl(lease_is_current=current.is_set)
        execute_task = asyncio.create_task(
            asyncio.to_thread(
                self._provider.execute,
                provisioned.provider_lease,
                request,
                control,
            )
        )
        cancellation_pending = False
        probe_error: BaseException | None = None
        while not execute_task.done():
            try:
                await asyncio.wait({execute_task}, timeout=0.05)
            except asyncio.CancelledError:
                cancellation_pending = True
                control.cancel()
            if not execute_task.done() and probe_error is None:
                try:
                    if not await self._store.execution_fence_is_current(fence):
                        current.clear()
                except asyncio.CancelledError:
                    cancellation_pending = True
                    control.cancel()
                except BaseException as exc:
                    probe_error = exc
                    current.clear()
        result: IsolatedCodeExecutionResult | None = None
        execution_error: BaseException | None = None
        execution_traceback = None
        try:
            result = execute_task.result()
            if not isinstance(result, IsolatedCodeExecutionResult):
                raise TypeError("provider returned an invalid execution result")
        except BaseException as exc:
            execution_error = exc
            execution_traceback = exc.__traceback__
        if probe_error is not None:
            execution_error = probe_error
            execution_traceback = probe_error.__traceback__

        reason = self._cleanup_reason(result, execution_error)
        barrier_task = asyncio.create_task(
            self._cleanup_barrier(
                provisioned,
                fence,
                cleanup_lease_seconds=cleanup_lease_seconds,
                reason=reason,
            )
        )
        while True:
            try:
                receipt = await asyncio.shield(barrier_task)
                break
            except asyncio.CancelledError:
                cancellation_pending = True
                control.cancel()
        fence_current_after_destroy = await self._store.execution_fence_is_current(fence)
        if cancellation_pending:
            raise asyncio.CancelledError
        if execution_error is not None:
            raise execution_error.with_traceback(execution_traceback)
        if result is None:  # pragma: no cover - invalid provider result is re-raised
            raise RuntimeError("provider returned no execution result")
        if not fence_current_after_destroy:
            result = IsolatedCodeExecutionResult(
                outcome="cancelled",
                exit_code=result.exit_code,
                result=None,
                stdout_tail="",
                stderr_tail="",
                truncated=result.truncated,
                duration_ms=result.duration_ms,
                interruption="lease_lost",
            )
            receipt = CodeCleanupReceipt(
                lease_id=receipt.lease_id,
                state="destroyed_confirmed",
                reason="lease_lost",
            )
        return CodeExecutionCompletion(
            lease=provisioned.provider_lease,
            result=result,
            cleanup=receipt,
        )

    async def _cleanup_barrier(
        self,
        provisioned: WorkflowCodeProvisionedLease,
        fence: WorkflowCodeExecutionFence,
        *,
        cleanup_lease_seconds: int,
        reason: str,
    ) -> CodeCleanupReceipt:
        try:
            claim = await self._store.begin_cleanup(
                provisioned.record.id,
                fence,
                cleanup_worker_id=fence.worker_id,
                cleanup_lease_seconds=cleanup_lease_seconds,
            )
        except WorkflowCodeLeaseConflict:
            claim = await self._store.handoff_running_resource(
                provisioned.record.id,
                raw_reconciliation_key=provisioned.raw_reconciliation_key,
                cleanup_worker_id=fence.worker_id,
                cleanup_lease_seconds=cleanup_lease_seconds,
            )
        try:
            receipt = await asyncio.to_thread(
                self._provider.cleanup,
                provisioned.provider_lease,
                reason=reason,
            )
            if not isinstance(receipt, CodeCleanupReceipt) or receipt.lease_id != provisioned.provider_lease.lease_id:
                raise TypeError("cleanup provider returned an invalid receipt")
        except BaseException:
            await self._store.release_cleanup_claim(
                claim.record.id,
                cleanup_worker_id=fence.worker_id,
                raw_cleanup_token=claim.raw_cleanup_token,
            )
            raise
        if receipt.state != "destroyed_confirmed":
            await self._store.release_cleanup_claim(
                claim.record.id,
                cleanup_worker_id=fence.worker_id,
                raw_cleanup_token=claim.raw_cleanup_token,
            )
            raise IsolatedCodeCleanupPending(provisioned.provider_lease, receipt)
        await self._store.confirm_destroyed(
            claim.record.id,
            cleanup_worker_id=fence.worker_id,
            raw_cleanup_token=claim.raw_cleanup_token,
        )
        _release_provider_handle(self._provider, claim.record)
        return receipt

    @staticmethod
    def _cleanup_reason(
        result: IsolatedCodeExecutionResult | None,
        execution_error: BaseException | None,
    ) -> str:
        if execution_error is not None or result is None:
            return "failed"
        if result.outcome == "succeeded":
            return "completed"
        if result.outcome == "timeout":
            return "timeout"
        if result.outcome == "cancelled":
            return result.interruption or "cancelled"
        return "failed"


class WorkflowCodeCleanupCoordinator:
    """Claim, decrypt, destroy, and durably confirm one resource at a time."""

    def __init__(
        self,
        *,
        store: PostgresWorkflowCodeLeaseStore,
        provider: _CleanupProvider,
        codec: WorkflowCodeCleanupCodec,
    ) -> None:
        if not isinstance(store, PostgresWorkflowCodeLeaseStore):
            raise TypeError("store must be PostgresWorkflowCodeLeaseStore")
        if isinstance(provider, bool) or not isinstance(provider, _CleanupProvider):
            raise TypeError("provider must implement cleanup")
        if isinstance(codec, bool) or not isinstance(codec, WorkflowCodeCleanupCodec):
            raise TypeError("codec must implement the cleanup codec")
        self._store = store
        self._provider = provider
        self._codec = codec

    async def reap_one(
        self,
        *,
        cleanup_worker_id: uuid.UUID,
        cleanup_lease_seconds: int,
    ) -> WorkflowCodeLeaseRecord | WorkflowCodeCleanupPending | None:
        claim = await self._store.claim_cleanup_pending(
            cleanup_worker_id=cleanup_worker_id,
            cleanup_lease_seconds=cleanup_lease_seconds,
        )
        if claim is None:
            return None
        ciphertext = claim.record.cleanup_locator_ciphertext
        if ciphertext is None:
            try:
                if claim.authority_observed_at is None:
                    raise RuntimeError("cleanup claim lacks database authority time")
                receipt = self._provider.reconcile_provisioning(
                    lease_id=str(claim.record.id),
                    reconciliation_key_hash=claim.record.reconciliation_key_hash,
                )
                if not isinstance(receipt, CodeCleanupReceipt) or receipt.lease_id != str(claim.record.id):
                    raise TypeError("cleanup provider returned an invalid receipt")
            except BaseException:
                await self._store.release_cleanup_claim(
                    claim.record.id,
                    cleanup_worker_id=cleanup_worker_id,
                    raw_cleanup_token=claim.raw_cleanup_token,
                )
                raise
            if receipt.state == "destroyed_confirmed":
                destroyed = await self._store.confirm_destroyed(
                    claim.record.id,
                    cleanup_worker_id=cleanup_worker_id,
                    raw_cleanup_token=claim.raw_cleanup_token,
                )
                _release_provider_handle(self._provider, claim.record)
                return destroyed
            released = await self._store.release_cleanup_claim(
                claim.record.id,
                cleanup_worker_id=cleanup_worker_id,
                raw_cleanup_token=claim.raw_cleanup_token,
            )
            return WorkflowCodeCleanupPending(released)
        try:
            lease = self._codec.open(ciphertext, claim.record.locator_context)
            receipt = self._provider.cleanup(lease, reason="cleanup_retry")
            if not isinstance(receipt, CodeCleanupReceipt) or receipt.lease_id != lease.lease_id:
                raise TypeError("cleanup provider returned an invalid receipt")
        except BaseException:
            await self._store.release_cleanup_claim(
                claim.record.id,
                cleanup_worker_id=cleanup_worker_id,
                raw_cleanup_token=claim.raw_cleanup_token,
            )
            raise
        if receipt.state == "destroyed_confirmed":
            destroyed = await self._store.confirm_destroyed(
                claim.record.id,
                cleanup_worker_id=cleanup_worker_id,
                raw_cleanup_token=claim.raw_cleanup_token,
            )
            _release_provider_handle(self._provider, claim.record)
            return destroyed
        released = await self._store.release_cleanup_claim(
            claim.record.id,
            cleanup_worker_id=cleanup_worker_id,
            raw_cleanup_token=claim.raw_cleanup_token,
        )
        return WorkflowCodeCleanupPending(released)
