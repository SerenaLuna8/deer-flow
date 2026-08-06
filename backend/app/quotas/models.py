from __future__ import annotations

import re
import uuid
import weakref
from dataclasses import dataclass
from threading import Lock
from typing import Literal, TypeGuard

from deerflow.runtime.private_scope import PrivateResourceScope

QuotaDimension = Literal[
    "members",
    "storage_bytes",
    "concurrent_runs",
    "mcp_calls_daily",
]
ProjectStorageQuotaOperation = Literal["reserve", "release"]

QUOTA_DIMENSIONS: tuple[QuotaDimension, ...] = (
    "members",
    "storage_bytes",
    "concurrent_runs",
    "mcp_calls_daily",
)
_KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_HMAC = re.compile(r"[0-9a-f]{64}")


class QuotaError(Exception):
    """Base class for content-free quota domain failures."""


class QuotaPolicyInvalid(QuotaError):
    """A project policy is not a valid platform tightening."""


class QuotaForbidden(QuotaError):
    """The actor cannot mutate project quota policy."""


class QuotaConflict(QuotaError):
    """Quota version or idempotency authority conflicts."""


class QuotaUnavailable(QuotaError):
    """The current platform quota policy cannot be read safely."""

    def __init__(self) -> None:
        super().__init__("Quota policy unavailable")


class QuotaExceeded(QuotaError):
    """A new consumption would cross the effective hard limit."""

    def __init__(self, dimension: QuotaDimension, limit: int) -> None:
        super().__init__("project quota exceeded")
        self.dimension = dimension
        self.limit = limit


_COMPENSATION_DIMENSIONS: dict[str, QuotaDimension] = {
    "file_delete": "storage_bytes",
    "membership_end": "members",
    "run_terminal": "concurrent_runs",
}


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QuotaCompensationAuthority:
    """Explicit internal authority for releasing a prior exact reservation."""

    scope: PrivateResourceScope
    reason: str
    dimension: QuotaDimension


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QuotaReconciliationAuthority:
    """Explicit trusted-operation authority for project quota repair."""

    project_id: uuid.UUID
    operation: str


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ProjectStorageQuotaAuthority:
    """Explicit internal authority for one project-owned storage mutation."""

    project_id: uuid.UUID
    operation: ProjectStorageQuotaOperation


_COMPENSATION_AUTHORITIES: dict[
    int,
    tuple[
        weakref.ReferenceType[QuotaCompensationAuthority],
        tuple[PrivateResourceScope, str, QuotaDimension],
    ],
] = {}
_RECONCILIATION_AUTHORITIES: dict[
    int,
    tuple[
        weakref.ReferenceType[QuotaReconciliationAuthority],
        tuple[uuid.UUID, str],
    ],
] = {}
_PROJECT_STORAGE_AUTHORITIES: dict[
    int,
    tuple[
        weakref.ReferenceType[ProjectStorageQuotaAuthority],
        tuple[uuid.UUID, ProjectStorageQuotaOperation],
    ],
] = {}
_AUTHORITY_LOCK = Lock()


def _register_authority(authority: object, registry: dict, snapshot: tuple) -> None:
    identity = id(authority)

    def discard(reference: weakref.ReferenceType) -> None:
        with _AUTHORITY_LOCK:
            current = registry.get(identity)
            if current is not None and current[0] is reference:
                del registry[identity]

    reference = weakref.ref(authority, discard)
    with _AUTHORITY_LOCK:
        registry[identity] = (reference, snapshot)


def _issue_quota_compensation_authority(
    scope: PrivateResourceScope,
    *,
    reason: str,
) -> QuotaCompensationAuthority:
    dimension = _COMPENSATION_DIMENSIONS.get(reason)
    if type(scope) is not PrivateResourceScope or dimension is None:
        raise QuotaForbidden("trusted quota compensation authority is required")
    try:
        uuid.UUID(scope.project_id)
        uuid.UUID(scope.owner_user_id)
        if type(scope.membership_version) is not int or scope.membership_version < 1:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise QuotaForbidden("trusted quota compensation authority is required") from None
    authority = object.__new__(QuotaCompensationAuthority)
    object.__setattr__(authority, "scope", scope)
    object.__setattr__(authority, "reason", reason)
    object.__setattr__(authority, "dimension", dimension)
    _register_authority(
        authority,
        _COMPENSATION_AUTHORITIES,
        (scope, reason, dimension),
    )
    return authority


def _is_issued_quota_compensation_authority(
    value: object,
) -> TypeGuard[QuotaCompensationAuthority]:
    if type(value) is not QuotaCompensationAuthority:
        return False
    with _AUTHORITY_LOCK:
        issued = _COMPENSATION_AUTHORITIES.get(id(value))
    return issued is not None and issued[0]() is value and issued[1] == (value.scope, value.reason, value.dimension)


def _issue_quota_reconciliation_authority(
    project_id: object,
    *,
    operation: str,
) -> QuotaReconciliationAuthority:
    if operation != "quota_repair":
        raise QuotaForbidden("trusted quota reconciliation authority is required")
    try:
        selected = uuid.UUID(str(project_id))
    except (AttributeError, TypeError, ValueError):
        raise QuotaForbidden("trusted quota reconciliation authority is required") from None
    authority = object.__new__(QuotaReconciliationAuthority)
    object.__setattr__(authority, "project_id", selected)
    object.__setattr__(authority, "operation", operation)
    _register_authority(
        authority,
        _RECONCILIATION_AUTHORITIES,
        (selected, operation),
    )
    return authority


def _is_issued_quota_reconciliation_authority(
    value: object,
) -> TypeGuard[QuotaReconciliationAuthority]:
    if type(value) is not QuotaReconciliationAuthority:
        return False
    with _AUTHORITY_LOCK:
        issued = _RECONCILIATION_AUTHORITIES.get(id(value))
    return issued is not None and issued[0]() is value and issued[1] == (value.project_id, value.operation)


def _issue_project_storage_quota_authority(
    project_id: object,
    *,
    operation: ProjectStorageQuotaOperation,
) -> ProjectStorageQuotaAuthority:
    if operation not in {"reserve", "release"}:
        raise QuotaForbidden("trusted project storage quota authority is required")
    try:
        selected = uuid.UUID(str(project_id))
    except (AttributeError, TypeError, ValueError):
        raise QuotaForbidden("trusted project storage quota authority is required") from None
    authority = object.__new__(ProjectStorageQuotaAuthority)
    object.__setattr__(authority, "project_id", selected)
    object.__setattr__(authority, "operation", operation)
    _register_authority(
        authority,
        _PROJECT_STORAGE_AUTHORITIES,
        (selected, operation),
    )
    return authority


def _is_issued_project_storage_quota_authority(
    value: object,
) -> TypeGuard[ProjectStorageQuotaAuthority]:
    if type(value) is not ProjectStorageQuotaAuthority:
        return False
    with _AUTHORITY_LOCK:
        issued = _PROJECT_STORAGE_AUTHORITIES.get(id(value))
    try:
        snapshot = (value.project_id, value.operation)
    except AttributeError:
        return False
    return issued is not None and issued[0]() is value and issued[1] == snapshot


@dataclass(frozen=True, slots=True)
class QuotaSourceRef:
    key_id: str
    hmac_hex: str

    def __post_init__(self) -> None:
        if _KEY_ID.fullmatch(self.key_id) is None or _HMAC.fullmatch(self.hmac_hex) is None:
            raise ValueError("quota source reference is invalid")


@dataclass(frozen=True, slots=True)
class ProjectQuotaLimits:
    member_limit: int | None = None
    storage_bytes_limit: int | None = None
    concurrent_run_limit: int | None = None
    mcp_calls_daily_limit: int | None = None


@dataclass(frozen=True, slots=True)
class EffectiveQuotaLimits:
    member_limit: int
    storage_bytes_limit: int
    concurrent_run_limit: int
    mcp_calls_daily_limit: int


@dataclass(frozen=True, slots=True)
class ProjectQuotaPolicy:
    configured: ProjectQuotaLimits
    effective: EffectiveQuotaLimits
    version: int


@dataclass(frozen=True, slots=True)
class QuotaUsageDimension:
    dimension: QuotaDimension
    bucket: str
    used: int
    reserved: int
    limit: int
    warning_threshold_reached: bool


@dataclass(frozen=True, slots=True)
class ProjectQuotaUsage:
    policy: ProjectQuotaPolicy
    dimensions: tuple[QuotaUsageDimension, ...]


@dataclass(frozen=True, slots=True)
class QuotaMutation:
    dimension: QuotaDimension
    bucket: str
    used: int
    reserved: int
    limit: int
    threshold_crossed: bool
    created: bool


@dataclass(frozen=True, slots=True)
class QuotaDifference:
    dimension: QuotaDimension
    bucket: str
    current: int
    expected: int


@dataclass(frozen=True, slots=True)
class QuotaReconciliationReport:
    project_id: str
    differences: tuple[QuotaDifference, ...]
    applied: bool


__all__ = [
    "EffectiveQuotaLimits",
    "ProjectStorageQuotaAuthority",
    "ProjectStorageQuotaOperation",
    "ProjectQuotaLimits",
    "ProjectQuotaPolicy",
    "ProjectQuotaUsage",
    "QUOTA_DIMENSIONS",
    "QuotaConflict",
    "QuotaCompensationAuthority",
    "QuotaDifference",
    "QuotaDimension",
    "QuotaError",
    "QuotaExceeded",
    "QuotaForbidden",
    "QuotaMutation",
    "QuotaPolicyInvalid",
    "QuotaReconciliationAuthority",
    "QuotaReconciliationReport",
    "QuotaSourceRef",
    "QuotaUsageDimension",
]
