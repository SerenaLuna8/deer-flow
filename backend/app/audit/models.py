from __future__ import annotations

import uuid
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import Lock
from types import MappingProxyType
from typing import Literal, Protocol, Self, TypeGuard

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)


class AuditError(Exception):
    """Base class for content-free audit failures."""


class AuditMetadataRejected(AuditError):
    def __init__(self) -> None:
        super().__init__("Audit metadata was rejected")


class AuditAuthorityRejected(AuditError):
    def __init__(self) -> None:
        super().__init__("Audit authority was rejected")


class AuditCursorRejected(AuditError):
    def __init__(self) -> None:
        super().__init__("Audit cursor was rejected")


class AuditUnavailable(AuditError):
    def __init__(self) -> None:
        super().__init__("Audit storage is unavailable")


class AuditAction(StrEnum):
    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"
    PROJECT_SUSPENDED = "project.suspended"
    PROJECT_RESUMED = "project.resumed"
    PROJECT_DELETION_REQUESTED = "project.deletion_requested"
    PROJECT_RECOVERED = "project.recovered"
    INVITATION_CREATED = "invitation.created"
    INVITATION_REVOKED = "invitation.revoked"
    INVITATION_REDEEMED = "invitation.redeemed"
    MEMBER_JOINED = "member.joined"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_REMOVED = "member.removed"
    MEMBER_LEFT = "member.left"
    ASSET_CREATED = "asset.created"
    ASSET_UPDATED = "asset.updated"
    ASSET_PUBLISHED = "asset.published"
    ASSET_EXPORTED = "asset.exported"
    ASSET_DEPRECATED = "asset.deprecated"
    ASSET_DELETED = "asset.deleted"
    ASSET_BOUND = "asset.bound"
    ASSET_UNBOUND = "asset.unbound"
    ASSET_CREDENTIAL_CREATED = "asset.credential_created"
    ASSET_CREDENTIAL_REPLACED = "asset.credential_replaced"
    ASSET_CREDENTIAL_REVOKED = "asset.credential_revoked"
    ASSET_CREDENTIAL_DELETED = "asset.credential_deleted"
    ASSET_CREDENTIAL_GRANTS_MIGRATED = "asset.credential_grants_migrated"
    AUTOMATION_CREATED = "automation.created"
    AUTOMATION_UPDATED = "automation.updated"
    AUTOMATION_DELETED = "automation.deleted"
    AUTOMATION_TRIGGERED = "automation.triggered"
    QUOTA_POLICY_UPDATED = "quota.policy_updated"
    QUOTA_RECONCILED = "quota.reconciled"
    RUN_ADMITTED = "run.admitted"
    RUN_CANCEL_REQUESTED = "run.cancel_requested"
    RUN_FILES_FINALIZED = "run.files_finalized"
    RUN_TERMINAL = "run.terminal"
    HOST_EXECUTION_APPROVAL_REQUESTED = "host_execution.approval_requested"
    HOST_EXECUTION_APPROVAL_AVAILABLE = "host_execution.approval_available"
    HOST_EXECUTION_APPROVAL_DECIDED = "host_execution.approval_decided"
    HOST_EXECUTION_APPROVAL_CLAIMED = "host_execution.approval_claimed"
    HOST_EXECUTION_APPROVAL_TERMINAL = "host_execution.approval_terminal"
    MEMORY_REMEMBER = "memory.remember"
    MEMORY_RECALL_EXECUTED = "memory.recall.executed"
    MEMORY_SEAL_ADMITTED = "memory.seal.admitted"
    MEMORY_SEAL_SETTLED = "memory.seal.settled"
    MEMORY_DREAM_ADMITTED = "memory.dream.admitted"
    MEMORY_DREAM_SETTLED = "memory.dream.settled"
    MEMORY_INJECTION_SKIPPED = "memory.injection.skipped"
    MEMORY_DREAM_REVIEW_FLAGGED = "memory.dream.review_flagged"
    MEMORY_RESTORE_EXECUTED = "memory.restore.executed"
    MEMORY_RESET_EXECUTED = "memory.reset.executed"
    JOB_DEAD = "job.dead"
    JOB_REQUEUED = "job.requeued"
    PURGE_COMPLETED = "purge.completed"
    AUDIT_CORRECTED = "audit.corrected"
    SYSTEM_SETTING_UPDATED = "system_setting.updated"


class AuditTargetKind(StrEnum):
    PROJECT = "project"
    INVITATION = "invitation"
    MEMBERSHIP = "membership"
    ASSET = "asset"
    AUTOMATION = "automation"
    QUOTA = "quota"
    RUN = "run"
    JOB = "job"
    PURGE = "purge"
    AUDIT = "audit"
    SYSTEM_SETTING = "system_setting"
    ACCOUNT = "account"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    REJECTED = "rejected"
    FAILED = "failed"


class AuditProcess(StrEnum):
    GATEWAY = "gateway"
    WORKER = "worker"
    SCHEDULER = "scheduler"
    OPERATOR = "operator"
    MIGRATION = "migration"


class AuditPlatformRole(StrEnum):
    SYSTEM_ADMIN = "system_admin"


class AuditScope(StrEnum):
    PROJECT = "project"
    PLATFORM = "platform"
    EITHER = "either"


@dataclass(frozen=True, slots=True)
class AuditActionVariant:
    scope: AuditScope
    actor: Literal["user", "system", "process"]
    processes: frozenset[AuditProcess] = frozenset()
    metadata_equals: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class AuditActionContract:
    target_kind: AuditTargetKind
    variants: tuple[AuditActionVariant, ...]
    authority_matches_project: bool = False


def _variant(
    scope: AuditScope,
    actor: Literal["user", "system", "process"],
    *,
    processes: tuple[AuditProcess, ...] = (),
    metadata_equals: tuple[tuple[str, object], ...] = (),
) -> AuditActionVariant:
    return AuditActionVariant(
        scope=scope,
        actor=actor,
        processes=frozenset(processes),
        metadata_equals=metadata_equals,
    )


def _contract(
    target_kind: AuditTargetKind,
    scope: AuditScope,
    *actors: Literal["user", "system", "process"],
    processes: tuple[AuditProcess, ...] = (),
    authority_matches_project: bool = False,
) -> AuditActionContract:
    return AuditActionContract(
        target_kind=target_kind,
        variants=tuple(
            _variant(
                scope,
                actor,
                processes=processes if actor == "process" else (),
            )
            for actor in actors
        ),
        authority_matches_project=authority_matches_project,
    )


_ACTION_CONTRACTS: dict[AuditAction, AuditActionContract] = {}
for _action in (
    AuditAction.PROJECT_CREATED,
    AuditAction.PROJECT_UPDATED,
    AuditAction.PROJECT_DELETION_REQUESTED,
    AuditAction.PROJECT_RECOVERED,
):
    _ACTION_CONTRACTS[_action] = _contract(
        AuditTargetKind.PROJECT,
        AuditScope.PROJECT,
        "user",
        "system",
        authority_matches_project=True,
    )
for _action in (AuditAction.PROJECT_SUSPENDED, AuditAction.PROJECT_RESUMED):
    _ACTION_CONTRACTS[_action] = _contract(
        AuditTargetKind.PROJECT,
        AuditScope.PROJECT,
        "user",
        "system",
        authority_matches_project=True,
    )
for _action in (
    AuditAction.INVITATION_CREATED,
    AuditAction.INVITATION_REVOKED,
    AuditAction.INVITATION_REDEEMED,
):
    _ACTION_CONTRACTS[_action] = _contract(
        AuditTargetKind.INVITATION,
        AuditScope.PROJECT,
        "user",
    )
for _action in (
    AuditAction.MEMBER_JOINED,
    AuditAction.MEMBER_ROLE_CHANGED,
    AuditAction.MEMBER_REMOVED,
    AuditAction.MEMBER_LEFT,
):
    _ACTION_CONTRACTS[_action] = _contract(
        AuditTargetKind.MEMBERSHIP,
        AuditScope.PROJECT,
        "user",
        "system",
    )
for _action in (
    AuditAction.ASSET_CREATED,
    AuditAction.ASSET_UPDATED,
    AuditAction.ASSET_PUBLISHED,
    AuditAction.ASSET_EXPORTED,
    AuditAction.ASSET_DEPRECATED,
    AuditAction.ASSET_DELETED,
    AuditAction.ASSET_CREDENTIAL_CREATED,
    AuditAction.ASSET_CREDENTIAL_REPLACED,
    AuditAction.ASSET_CREDENTIAL_REVOKED,
    AuditAction.ASSET_CREDENTIAL_DELETED,
    AuditAction.ASSET_CREDENTIAL_GRANTS_MIGRATED,
):
    _ACTION_CONTRACTS[_action] = AuditActionContract(
        target_kind=AuditTargetKind.ASSET,
        variants=(
            _variant(AuditScope.PROJECT, "user"),
            _variant(AuditScope.PROJECT, "system"),
            _variant(AuditScope.PLATFORM, "system"),
        ),
    )
for _action in (AuditAction.ASSET_BOUND, AuditAction.ASSET_UNBOUND):
    _ACTION_CONTRACTS[_action] = _contract(
        AuditTargetKind.ASSET,
        AuditScope.PROJECT,
        "user",
        "system",
    )
for _action in (
    AuditAction.AUTOMATION_CREATED,
    AuditAction.AUTOMATION_UPDATED,
    AuditAction.AUTOMATION_DELETED,
):
    _ACTION_CONTRACTS[_action] = _contract(
        AuditTargetKind.AUTOMATION,
        AuditScope.PROJECT,
        "user",
    )
_ACTION_CONTRACTS[AuditAction.AUTOMATION_TRIGGERED] = AuditActionContract(
    target_kind=AuditTargetKind.AUTOMATION,
    variants=(
        _variant(
            AuditScope.PROJECT,
            "user",
            metadata_equals=(("trigger_kind", "manual"),),
        ),
        _variant(
            AuditScope.PROJECT,
            "process",
            processes=(AuditProcess.SCHEDULER,),
            metadata_equals=(("trigger_kind", "scheduled"),),
        ),
    ),
)
_ACTION_CONTRACTS[AuditAction.QUOTA_POLICY_UPDATED] = _contract(
    AuditTargetKind.QUOTA,
    AuditScope.PROJECT,
    "user",
    "system",
)
_ACTION_CONTRACTS[AuditAction.QUOTA_RECONCILED] = _contract(
    AuditTargetKind.QUOTA,
    AuditScope.PROJECT,
    "system",
    "process",
    processes=(AuditProcess.OPERATOR,),
)
_ACTION_CONTRACTS[AuditAction.RUN_ADMITTED] = AuditActionContract(
    target_kind=AuditTargetKind.RUN,
    variants=(
        _variant(
            AuditScope.PROJECT,
            "user",
            metadata_equals=(
                ("job_type", "private_run"),
                ("non_interactive", False),
            ),
        ),
        _variant(
            AuditScope.PROJECT,
            "user",
            metadata_equals=(
                ("job_type", "automation_run"),
                ("non_interactive", True),
            ),
        ),
        _variant(
            AuditScope.PROJECT,
            "process",
            processes=(AuditProcess.SCHEDULER,),
            metadata_equals=(
                ("job_type", "automation_run"),
                ("non_interactive", True),
            ),
        ),
    ),
)
_ACTION_CONTRACTS[AuditAction.RUN_CANCEL_REQUESTED] = _contract(
    AuditTargetKind.RUN,
    AuditScope.PROJECT,
    "user",
)
_ACTION_CONTRACTS[AuditAction.RUN_FILES_FINALIZED] = _contract(
    AuditTargetKind.RUN,
    AuditScope.PROJECT,
    "process",
    processes=(AuditProcess.WORKER,),
)
_ACTION_CONTRACTS[AuditAction.RUN_TERMINAL] = AuditActionContract(
    target_kind=AuditTargetKind.RUN,
    variants=(
        _variant(
            AuditScope.PROJECT,
            "process",
            processes=(AuditProcess.WORKER,),
        ),
        _variant(
            AuditScope.PROJECT,
            "process",
            processes=(AuditProcess.GATEWAY,),
            metadata_equals=(("status", "cancelled"),),
        ),
    ),
)
for _action in (
    AuditAction.HOST_EXECUTION_APPROVAL_REQUESTED,
    AuditAction.HOST_EXECUTION_APPROVAL_AVAILABLE,
    AuditAction.HOST_EXECUTION_APPROVAL_CLAIMED,
):
    _ACTION_CONTRACTS[_action] = _contract(
        AuditTargetKind.RUN,
        AuditScope.PROJECT,
        "process",
        processes=(AuditProcess.WORKER,),
    )
_ACTION_CONTRACTS[AuditAction.HOST_EXECUTION_APPROVAL_DECIDED] = _contract(
    AuditTargetKind.RUN,
    AuditScope.PROJECT,
    "user",
)
_ACTION_CONTRACTS[AuditAction.HOST_EXECUTION_APPROVAL_TERMINAL] = AuditActionContract(
    target_kind=AuditTargetKind.RUN,
    variants=(
        _variant(
            AuditScope.PROJECT,
            "process",
            processes=(AuditProcess.WORKER,),
        ),
        *(
            _variant(
                AuditScope.PROJECT,
                "process",
                processes=(AuditProcess.GATEWAY,),
                metadata_equals=(("status", status),),
            )
            for status in ("expired", "cancelled", "unknown")
        ),
    ),
)
_ACTION_CONTRACTS[AuditAction.MEMORY_SEAL_ADMITTED] = _contract(
    AuditTargetKind.JOB,
    AuditScope.PROJECT,
    "process",
    processes=(AuditProcess.SCHEDULER,),
)
_ACTION_CONTRACTS[AuditAction.MEMORY_SEAL_SETTLED] = _contract(
    AuditTargetKind.JOB,
    AuditScope.PROJECT,
    "process",
    processes=(AuditProcess.WORKER,),
)
_ACTION_CONTRACTS[AuditAction.MEMORY_DREAM_ADMITTED] = AuditActionContract(
    target_kind=AuditTargetKind.JOB,
    variants=(
        _variant(
            AuditScope.PROJECT,
            "user",
            metadata_equals=(("origin", "manual"),),
        ),
        _variant(
            AuditScope.PROJECT,
            "process",
            processes=(AuditProcess.SCHEDULER,),
            metadata_equals=(("origin", "scheduled"),),
        ),
        _variant(
            AuditScope.PROJECT,
            "process",
            processes=(AuditProcess.WORKER,),
            metadata_equals=(("origin", "prepared"),),
        ),
    ),
)
_ACTION_CONTRACTS[AuditAction.MEMORY_DREAM_SETTLED] = AuditActionContract(
    target_kind=AuditTargetKind.JOB,
    variants=(
        _variant(
            AuditScope.PROJECT,
            "process",
            processes=(AuditProcess.WORKER,),
        ),
        _variant(
            AuditScope.PROJECT,
            "process",
            processes=(AuditProcess.GATEWAY,),
            metadata_equals=(("disposition", "cancelled"),),
        ),
    ),
)
_ACTION_CONTRACTS[AuditAction.MEMORY_REMEMBER] = _contract(
    AuditTargetKind.RUN,
    AuditScope.PROJECT,
    "process",
    processes=(AuditProcess.WORKER,),
)
_ACTION_CONTRACTS[AuditAction.MEMORY_RECALL_EXECUTED] = _contract(
    AuditTargetKind.RUN,
    AuditScope.PROJECT,
    "process",
    processes=(AuditProcess.WORKER,),
)
_ACTION_CONTRACTS[AuditAction.MEMORY_INJECTION_SKIPPED] = _contract(
    AuditTargetKind.RUN,
    AuditScope.PROJECT,
    "process",
    processes=(AuditProcess.GATEWAY, AuditProcess.SCHEDULER),
)
_ACTION_CONTRACTS[AuditAction.MEMORY_DREAM_REVIEW_FLAGGED] = _contract(
    AuditTargetKind.JOB,
    AuditScope.PROJECT,
    "process",
    processes=(AuditProcess.WORKER,),
)
_ACTION_CONTRACTS[AuditAction.MEMORY_RESTORE_EXECUTED] = _contract(
    AuditTargetKind.PROJECT,
    AuditScope.PROJECT,
    "user",
    authority_matches_project=True,
)
_ACTION_CONTRACTS[AuditAction.MEMORY_RESET_EXECUTED] = AuditActionContract(
    target_kind=AuditTargetKind.ACCOUNT,
    variants=(
        _variant(
            AuditScope.PLATFORM,
            "user",
            metadata_equals=(("scope", "account"),),
        ),
        _variant(
            AuditScope.PROJECT,
            "user",
            metadata_equals=(("scope", "project"),),
        ),
    ),
)
_ACTION_CONTRACTS[AuditAction.JOB_DEAD] = _contract(
    AuditTargetKind.JOB,
    AuditScope.PROJECT,
    "process",
    processes=(AuditProcess.WORKER,),
)
_ACTION_CONTRACTS[AuditAction.JOB_REQUEUED] = _contract(
    AuditTargetKind.JOB,
    AuditScope.PROJECT,
    "system",
)
_ACTION_CONTRACTS[AuditAction.PURGE_COMPLETED] = AuditActionContract(
    target_kind=AuditTargetKind.PURGE,
    variants=(
        _variant(
            AuditScope.PROJECT,
            "process",
            processes=(AuditProcess.WORKER,),
            metadata_equals=(("resource_kind", "project"),),
        ),
        _variant(
            AuditScope.PROJECT,
            "process",
            processes=(AuditProcess.WORKER,),
            metadata_equals=(("resource_kind", "file"),),
        ),
        _variant(
            AuditScope.PROJECT,
            "process",
            processes=(AuditProcess.WORKER,),
            metadata_equals=(("resource_kind", "former_owner"),),
        ),
        _variant(
            AuditScope.PLATFORM,
            "process",
            processes=(AuditProcess.WORKER,),
            metadata_equals=(("resource_kind", "account"),),
        ),
    ),
)
_ACTION_CONTRACTS[AuditAction.AUDIT_CORRECTED] = _contract(
    AuditTargetKind.AUDIT,
    AuditScope.EITHER,
    "system",
)
_ACTION_CONTRACTS[AuditAction.SYSTEM_SETTING_UPDATED] = _contract(
    AuditTargetKind.SYSTEM_SETTING,
    AuditScope.PLATFORM,
    "system",
)
AUDIT_ACTION_CONTRACTS: Mapping[AuditAction, AuditActionContract] = MappingProxyType(_ACTION_CONTRACTS)


@dataclass(frozen=True, slots=True, weakref_slot=True)
class AuditActor:
    user_id: uuid.UUID | None = None
    process: AuditProcess | None = None
    platform_role: AuditPlatformRole | None = None

    def __post_init__(self) -> None:
        user_valid = self.user_id is None or type(self.user_id) is uuid.UUID
        process_valid = self.process is None or type(self.process) is AuditProcess
        role_valid = self.platform_role is None or type(self.platform_role) is AuditPlatformRole
        if not user_valid or not process_valid or not role_valid or ((self.user_id is None) == (self.process is None)):
            raise AuditAuthorityRejected()
        if self.process is not None and self.platform_role is not None:
            raise AuditAuthorityRejected()

    @classmethod
    def user(
        cls,
        user_id: uuid.UUID,
    ) -> AuditActor:
        return cls(user_id=user_id)

    @classmethod
    def system_admin(cls, context: SystemAuditContext) -> AuditActor:
        if not is_issued_system_audit_context(context):
            raise AuditAuthorityRejected()
        actor = cls(
            user_id=context.user_id,
            platform_role=AuditPlatformRole.SYSTEM_ADMIN,
        )
        _register_elevated_actor(actor)
        return actor

    @classmethod
    def trusted_process(cls, context: AuditProcessContext) -> AuditActor:
        if not is_issued_audit_process_context(context):
            raise AuditAuthorityRejected()
        actor = cls(process=context.process)
        _register_elevated_actor(actor, process_issuer_id=context.issuer_id)
        return actor


@dataclass(frozen=True, slots=True)
class AuditTarget:
    kind: AuditTargetKind
    authority_id: uuid.UUID
    project_id: uuid.UUID | None

    def __post_init__(self) -> None:
        if type(self.kind) is not AuditTargetKind or type(self.authority_id) is not uuid.UUID or (self.project_id is not None and type(self.project_id) is not uuid.UUID):
            raise AuditAuthorityRejected()


@dataclass(frozen=True, slots=True)
class AuditRecord:
    id: uuid.UUID
    occurred_at: datetime
    actor_user_id: uuid.UUID | None
    actor_process: AuditProcess | None
    actor_platform_role: AuditPlatformRole | None
    project_id: uuid.UUID | None
    action: AuditAction
    target_kind: AuditTargetKind
    outcome: AuditOutcome
    public_error_code: str | None
    request_id: str | None
    job_id: uuid.UUID | None
    attempt_id: uuid.UUID | None
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class AuditPage:
    items: tuple[AuditRecord, ...]
    next_cursor: str | None


class _AuthenticatedSystemAdmin(Protocol):
    id: uuid.UUID
    system_role: str


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class SystemAuditContext:
    user_id: uuid.UUID
    request_id: str


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class AuditProcessContext:
    process: AuditProcess
    issuer_id: uuid.UUID


_SYSTEM_CONTEXTS: dict[
    int,
    tuple[weakref.ReferenceType[SystemAuditContext], tuple[uuid.UUID, str]],
] = {}
_PROCESS_CONTEXTS: dict[
    int,
    tuple[
        weakref.ReferenceType[AuditProcessContext],
        tuple[AuditProcess, uuid.UUID],
    ],
] = {}
_SYSTEM_CONTEXT_LOCK = Lock()
_ELEVATED_ACTORS: dict[
    int,
    tuple[
        weakref.ReferenceType[AuditActor],
        tuple[
            uuid.UUID | None,
            AuditProcess | None,
            AuditPlatformRole | None,
            uuid.UUID | None,
        ],
    ],
] = {}


def _valid_request_id(value: object) -> bool:
    return type(value) is str and 1 <= len(value) <= 512 and value == value.strip() and all(32 <= ord(character) <= 126 for character in value)


def is_issued_audit_process_context(
    value: object,
) -> TypeGuard[AuditProcessContext]:
    if type(value) is not AuditProcessContext:
        return False
    with _SYSTEM_CONTEXT_LOCK:
        issued = _PROCESS_CONTEXTS.get(id(value))
    try:
        return issued is not None and issued[0]() is value and issued[1] == (value.process, value.issuer_id)
    except AttributeError:
        return False


class _AuditProcessRegistry:
    """AuditService-owned one-time process authority registry."""

    def __init__(self) -> None:
        self._issuer_id = uuid.uuid4()
        self._context: AuditProcessContext | None = None

    def bind(self, process: AuditProcess) -> AuditProcessContext:
        if type(process) is not AuditProcess:
            raise AuditAuthorityRejected()
        if self._context is not None:
            if self._context.process is not process:
                raise AuditAuthorityRejected()
            return self._context
        context = object.__new__(AuditProcessContext)
        object.__setattr__(context, "process", process)
        object.__setattr__(context, "issuer_id", self._issuer_id)
        identity = id(context)

        def discard(reference: weakref.ReferenceType[AuditProcessContext]) -> None:
            with _SYSTEM_CONTEXT_LOCK:
                current = _PROCESS_CONTEXTS.get(identity)
                if current is not None and current[0] is reference:
                    del _PROCESS_CONTEXTS[identity]

        reference = weakref.ref(context, discard)
        with _SYSTEM_CONTEXT_LOCK:
            _PROCESS_CONTEXTS[identity] = (
                reference,
                (process, self._issuer_id),
            )
        self._context = context
        return context

    def owns(self, context: object) -> TypeGuard[AuditProcessContext]:
        return is_issued_audit_process_context(context) and context is self._context and context.issuer_id == self._issuer_id

    @property
    def issuer_id(self) -> uuid.UUID:
        return self._issuer_id


def _register_elevated_actor(
    actor: AuditActor,
    *,
    process_issuer_id: uuid.UUID | None = None,
) -> None:
    if (actor.process is None) != (process_issuer_id is None):
        raise AuditAuthorityRejected()
    identity = id(actor)
    snapshot = (
        actor.user_id,
        actor.process,
        actor.platform_role,
        process_issuer_id,
    )

    def discard(reference: weakref.ReferenceType[AuditActor]) -> None:
        with _SYSTEM_CONTEXT_LOCK:
            current = _ELEVATED_ACTORS.get(identity)
            if current is not None and current[0] is reference:
                del _ELEVATED_ACTORS[identity]

    reference = weakref.ref(actor, discard)
    with _SYSTEM_CONTEXT_LOCK:
        _ELEVATED_ACTORS[identity] = (reference, snapshot)


def is_issued_elevated_audit_actor(
    value: object,
    *,
    process_issuer_id: uuid.UUID | None = None,
) -> TypeGuard[AuditActor]:
    if type(value) is not AuditActor:
        return False
    with _SYSTEM_CONTEXT_LOCK:
        issued = _ELEVATED_ACTORS.get(id(value))
    try:
        return (
            issued is not None
            and issued[0]() is value
            and issued[1]
            == (
                value.user_id,
                value.process,
                value.platform_role,
                process_issuer_id,
            )
        )
    except AttributeError:
        return False


def resolve_system_audit_context(
    user: _AuthenticatedSystemAdmin,
    *,
    request_id: str,
) -> SystemAuditContext:
    try:
        user_id = user.id
        if user.system_role != AuditPlatformRole.SYSTEM_ADMIN.value or type(user_id) is not uuid.UUID or not _valid_request_id(request_id):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise AuditAuthorityRejected() from None
    context = object.__new__(SystemAuditContext)
    object.__setattr__(context, "user_id", user_id)
    object.__setattr__(context, "request_id", request_id)
    identity = id(context)

    def discard(reference: weakref.ReferenceType[SystemAuditContext]) -> None:
        with _SYSTEM_CONTEXT_LOCK:
            current = _SYSTEM_CONTEXTS.get(identity)
            if current is not None and current[0] is reference:
                del _SYSTEM_CONTEXTS[identity]

    reference = weakref.ref(context, discard)
    with _SYSTEM_CONTEXT_LOCK:
        _SYSTEM_CONTEXTS[identity] = (reference, (user_id, request_id))
    return context


def is_issued_system_audit_context(value: object) -> TypeGuard[SystemAuditContext]:
    if type(value) is not SystemAuditContext:
        return False
    with _SYSTEM_CONTEXT_LOCK:
        issued = _SYSTEM_CONTEXTS.get(id(value))
    try:
        return issued is not None and issued[0]() is value and issued[1] == (value.user_id, value.request_id)
    except AttributeError:
        return False


class _AuditMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class EmptyAuditMetadata(_AuditMetadata):
    pass


class RoleAuditMetadata(_AuditMetadata):
    role: Literal["admin", "editor", "runner", "viewer"]


class RoleChangedAuditMetadata(_AuditMetadata):
    previous_role: Literal["admin", "editor", "runner", "viewer"]
    role: Literal["admin", "editor", "runner", "viewer"]


class AssetAuditMetadata(_AuditMetadata):
    asset_kind: Literal["agent", "skill", "mcp"]
    operation: Literal[
        "agent.create",
        "agent.version.create",
        "agent.instructions.update",
        "agent.capability_bindings.update",
        "agent.version.activate",
        "agent.delete",
        "agent.enable",
        "agent.suspend",
        "agent.default.set",
        "agent.default.clear",
        "skill.create",
        "skill.version.create",
        "skill.version.activate",
        "skill.export",
        "skill.version.revoke",
        "skill.delete",
        "skill.enable",
        "skill.credential_bindings.configure",
        "skill.suspend",
        "mcp.create",
        "mcp.version.create",
        "mcp.submit_approval",
        "mcp.approve",
        "mcp.credential_grants.configure",
        "mcp.publish",
        "mcp.archive",
        "mcp.suspend",
        "mcp.activate",
        "mcp.delete",
        "credential.create",
        "credential.replace",
        "credential.revoke",
        "credential.delete",
        "credential.grants.migrate",
        "binding.enable",
        "binding.upgrade",
        "binding.rollback",
        "binding.sync_current",
        "binding.disable",
    ]
    version_number: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_asset_operation(self) -> Self:
        operation_domain = self.operation.partition(".")[0]
        if operation_domain in {"agent", "skill", "mcp"} and operation_domain != self.asset_kind:
            raise ValueError("asset audit operation does not match the asset kind")
        if operation_domain == "credential" and self.asset_kind != "mcp":
            raise ValueError("Credential audit operations must target an MCP asset")

        versioned_agent_operations = {
            "agent.version.create",
            "agent.instructions.update",
            "agent.capability_bindings.update",
            "agent.version.activate",
        }
        versioned_skill_operations = {
            "skill.version.create",
            "skill.version.activate",
            "skill.export",
            "skill.version.revoke",
        }
        if operation_domain == "agent":
            expects_version = self.operation in versioned_agent_operations
            if expects_version != (self.version_number is not None):
                raise ValueError("Agent audit version coordinates are inconsistent")
        elif operation_domain == "skill":
            expects_version = self.operation in versioned_skill_operations
            if expects_version != (self.version_number is not None):
                raise ValueError("Skill audit version coordinates are inconsistent")
        elif self.version_number is not None:
            raise ValueError("asset audit version number is not supported for this operation")
        return self


class AutomationAuditMetadata(_AuditMetadata):
    trigger_kind: Literal["manual", "scheduled"] | None = None


class AutomationTriggeredAuditMetadata(_AuditMetadata):
    trigger_kind: Literal["manual", "scheduled"]


class QuotaPolicyAuditMetadata(_AuditMetadata):
    member_limit: StrictInt | None = Field(default=None, ge=1)
    storage_bytes_limit: StrictInt | None = Field(default=None, ge=0)
    concurrent_run_limit: StrictInt | None = Field(default=None, ge=1)
    mcp_calls_daily_limit: StrictInt | None = Field(default=None, ge=0)
    version: StrictInt = Field(ge=1)


class QuotaReconciledAuditMetadata(_AuditMetadata):
    changed_dimensions: StrictInt = Field(ge=0, le=4)


class RunAdmittedAuditMetadata(_AuditMetadata):
    job_type: Literal["private_run", "automation_run"]
    non_interactive: StrictBool


class RunTerminalAuditMetadata(_AuditMetadata):
    job_type: Literal["private_run", "automation_run"]
    status: Literal["completed", "failed", "cancelled"]
    public_error_code: StrictStr | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")


class RunFilesFinalizedAuditMetadata(_AuditMetadata):
    created_count: StrictInt = Field(ge=0)
    modified_count: StrictInt = Field(ge=0)
    deleted_count: StrictInt = Field(ge=0)
    artifact_count: StrictInt = Field(ge=0)
    committed_bytes: StrictInt = Field(ge=0)


class HostExecutionApprovalDecisionAuditMetadata(_AuditMetadata):
    decision: Literal["allow_once", "deny"]


class HostExecutionApprovalTerminalAuditMetadata(_AuditMetadata):
    status: Literal[
        "finished",
        "launch_failed",
        "unknown",
        "cancelled",
        "expired",
    ]


class MemoryRememberAuditMetadata(_AuditMetadata):
    # Only the closed durability tag is recorded; memory content never
    # reaches the audit trail.
    kind: Literal["permanent", "durable", "ephemeral", "correction"]


class MemoryRecallExecutedAuditMetadata(_AuditMetadata):
    # Content-free recall quality signal: bucketed result count, which ranking
    # stage produced the top hit, whether a tag filter narrowed the search, and
    # the normalized query's Unicode code-point length bucket. Query and episode
    # text never reach the audit trail.
    result_bucket: Literal["0", "1-2", "3+"]
    matched_stage: Literal["exact", "similarity", "none"]
    tags_filtered: StrictBool
    query_len_bucket: Literal["1-4", "5-16", "17-64", "65-200"]


class MemorySealSettledAuditMetadata(_AuditMetadata):
    # Content-free outcome of one idle-seal attempt: ``sealed`` drained the
    # thread, ``noop`` yielded to a live Run.
    disposition: Literal["sealed", "noop"]


class MemoryDreamAdmittedAuditMetadata(_AuditMetadata):
    origin: Literal["manual", "scheduled", "prepared"]
    trigger: Literal["auto_dream", "manual_dream", "budget_rewrite"]
    history_count: StrictInt = Field(ge=0, le=20)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        origin_matches = (
            (self.origin == "manual" and self.trigger in {"manual_dream", "budget_rewrite"})
            or (self.origin == "scheduled" and self.trigger in {"auto_dream", "budget_rewrite"})
            or (self.origin == "prepared" and self.trigger in {"manual_dream", "budget_rewrite"})
        )
        history_matches = (self.trigger == "budget_rewrite" and self.history_count == 0) or (self.trigger != "budget_rewrite" and self.history_count >= 1)
        if not origin_matches or not history_matches:
            raise ValueError("Memory Dream admission metadata is inconsistent")
        return self


class MemoryDreamSettledAuditMetadata(_AuditMetadata):
    disposition: Literal["published", "cancelled", "dead"]
    version: StrictInt | None = Field(default=None, ge=1)
    public_error_code: StrictStr | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{0,63}$",
    )

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        valid = (
            (self.disposition == "published" and self.version is not None and self.public_error_code is None)
            or (self.disposition == "cancelled" and self.version is None and self.public_error_code is None)
            or (self.disposition == "dead" and self.version is None and self.public_error_code is not None)
        )
        if not valid:
            raise ValueError("Memory Dream settlement metadata is inconsistent")
        return self


class MemoryRestoreAuditMetadata(_AuditMetadata):
    source_version: StrictInt = Field(ge=1)
    previous_version: StrictInt = Field(ge=1)
    published_version: StrictInt = Field(ge=2)
    changed: StrictBool

    @model_validator(mode="after")
    def validate_version(self) -> Self:
        if self.published_version != self.previous_version + 1:
            raise ValueError("Memory restore versions are inconsistent")
        return self


class MemoryResetAuditMetadata(_AuditMetadata):
    scope: Literal["account", "project"]
    projects_affected: StrictInt | None = Field(default=None, ge=0)
    scopes_reset: StrictInt | None = Field(default=None, ge=0)
    history_entries: StrictInt | None = Field(default=None, ge=0)
    documents: StrictInt | None = Field(default=None, ge=0)
    versions: StrictInt | None = Field(default=None, ge=0)
    dream_runs: StrictInt | None = Field(default=None, ge=0)
    prepare_runs: StrictInt | None = Field(default=None, ge=0)
    snapshots: StrictInt | None = Field(default=None, ge=0)
    episodes: StrictInt | None = Field(default=None, ge=0)
    jobs_cancelled: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        counts = (
            self.projects_affected,
            self.scopes_reset,
            self.history_entries,
            self.documents,
            self.versions,
            self.dream_runs,
            self.prepare_runs,
            self.snapshots,
            self.episodes,
            self.jobs_cancelled,
        )
        if self.scope == "account":
            valid = all(value is not None for value in counts)
        else:
            valid = all(value is None for value in counts)
        if not valid:
            raise ValueError("Memory reset metadata is inconsistent")
        return self


class MemoryInjectionSkippedAuditMetadata(_AuditMetadata):
    # The only degradation reason today; storage corruption stays fail-closed
    # and never reaches this event.
    reason: Literal["over_budget"]


class MemoryDreamReviewFlaggedAuditMetadata(_AuditMetadata):
    # Content-free review signal: the published version plus a decile bucket
    # of the pure-deletion ratio; never document text.
    version: StrictInt = Field(ge=1)
    deletion_ratio_bucket: Literal[
        "40-50%",
        "50-60%",
        "60-70%",
        "70-80%",
        "80-90%",
        "90-100%",
    ]


class JobAuditMetadata(_AuditMetadata):
    job_type: Literal[
        "private_run",
        "automation_run",
        "retention_purge",
        "mcp_discovery",
        "memory_dream",
        "memory_dream_prepare",
        "memory_seal",
    ]
    public_error_code: StrictStr | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    attempt_count: StrictInt = Field(ge=0, le=20)
    retry_safety: Literal["safe", "unknown", "unsafe"]


class PurgeAuditMetadata(_AuditMetadata):
    resource_kind: Literal["project", "account", "file", "former_owner"]
    purged_count: StrictInt = Field(ge=0)


class CorrectionAuditMetadata(_AuditMetadata):
    correction_kind: Literal["outcome", "metadata", "target"]


class SystemSettingAuditMetadata(_AuditMetadata):
    section: Literal["agent_runtime", "auth", "automations", "memory_document", "quotas"]
    revision: StrictInt = Field(ge=2)
    schema_version: StrictInt = Field(ge=1)
    payload_checksum: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    effect_scope: Literal[
        "new_requests_and_runs",
        "new_requests",
        "new_memory_documents",
        "next_authoritative_check",
    ]


_AUDIT_METADATA_MODELS: dict[AuditAction, type[_AuditMetadata]] = {action: EmptyAuditMetadata for action in AuditAction}
for _action in (
    AuditAction.INVITATION_CREATED,
    AuditAction.INVITATION_REDEEMED,
    AuditAction.MEMBER_JOINED,
):
    _AUDIT_METADATA_MODELS[_action] = RoleAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.MEMBER_ROLE_CHANGED] = RoleChangedAuditMetadata
for _action in (
    AuditAction.ASSET_CREATED,
    AuditAction.ASSET_UPDATED,
    AuditAction.ASSET_PUBLISHED,
    AuditAction.ASSET_EXPORTED,
    AuditAction.ASSET_DEPRECATED,
    AuditAction.ASSET_DELETED,
    AuditAction.ASSET_BOUND,
    AuditAction.ASSET_UNBOUND,
    AuditAction.ASSET_CREDENTIAL_CREATED,
    AuditAction.ASSET_CREDENTIAL_REPLACED,
    AuditAction.ASSET_CREDENTIAL_REVOKED,
    AuditAction.ASSET_CREDENTIAL_DELETED,
    AuditAction.ASSET_CREDENTIAL_GRANTS_MIGRATED,
):
    _AUDIT_METADATA_MODELS[_action] = AssetAuditMetadata
for _action in (
    AuditAction.AUTOMATION_CREATED,
    AuditAction.AUTOMATION_UPDATED,
    AuditAction.AUTOMATION_DELETED,
    AuditAction.AUTOMATION_TRIGGERED,
):
    _AUDIT_METADATA_MODELS[_action] = AutomationAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.AUTOMATION_TRIGGERED] = AutomationTriggeredAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.QUOTA_POLICY_UPDATED] = QuotaPolicyAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.QUOTA_RECONCILED] = QuotaReconciledAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.RUN_ADMITTED] = RunAdmittedAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.RUN_FILES_FINALIZED] = RunFilesFinalizedAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.RUN_TERMINAL] = RunTerminalAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.HOST_EXECUTION_APPROVAL_DECIDED] = HostExecutionApprovalDecisionAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.HOST_EXECUTION_APPROVAL_TERMINAL] = HostExecutionApprovalTerminalAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.MEMORY_REMEMBER] = MemoryRememberAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.MEMORY_RECALL_EXECUTED] = MemoryRecallExecutedAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.MEMORY_SEAL_SETTLED] = MemorySealSettledAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.MEMORY_DREAM_ADMITTED] = MemoryDreamAdmittedAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.MEMORY_DREAM_SETTLED] = MemoryDreamSettledAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.MEMORY_INJECTION_SKIPPED] = MemoryInjectionSkippedAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.MEMORY_DREAM_REVIEW_FLAGGED] = MemoryDreamReviewFlaggedAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.MEMORY_RESTORE_EXECUTED] = MemoryRestoreAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.MEMORY_RESET_EXECUTED] = MemoryResetAuditMetadata
for _action in (AuditAction.JOB_DEAD, AuditAction.JOB_REQUEUED):
    _AUDIT_METADATA_MODELS[_action] = JobAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.PURGE_COMPLETED] = PurgeAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.AUDIT_CORRECTED] = CorrectionAuditMetadata
_AUDIT_METADATA_MODELS[AuditAction.SYSTEM_SETTING_UPDATED] = SystemSettingAuditMetadata
AUDIT_METADATA_MODELS: Mapping[AuditAction, type[_AuditMetadata]] = MappingProxyType(_AUDIT_METADATA_MODELS)


__all__ = [
    "AUDIT_ACTION_CONTRACTS",
    "AUDIT_METADATA_MODELS",
    "AuditAction",
    "AuditActionContract",
    "AuditActionVariant",
    "AuditActor",
    "AuditAuthorityRejected",
    "AuditCursorRejected",
    "AuditError",
    "AuditMetadataRejected",
    "AuditOutcome",
    "AuditPage",
    "AuditPlatformRole",
    "AuditProcess",
    "AuditProcessContext",
    "AuditRecord",
    "AuditScope",
    "AuditTarget",
    "AuditTargetKind",
    "AuditUnavailable",
    "SystemAuditContext",
    "is_issued_audit_process_context",
    "is_issued_elevated_audit_actor",
    "is_issued_system_audit_context",
    "resolve_system_audit_context",
]
