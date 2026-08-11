"""Closed Workflow state machines shared by repositories and services."""

from __future__ import annotations

from enum import StrEnum


class WorkflowStateConflict(ValueError):
    """A requested state edge is outside the frozen Workflow lifecycle."""


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SIDE_EFFECT_UNKNOWN = "side_effect_unknown"

    @classmethod
    def active(cls) -> frozenset[RunStatus]:
        return frozenset({cls.QUEUED, cls.RUNNING})

    @classmethod
    def terminal(cls) -> frozenset[RunStatus]:
        return frozenset(
            {
                cls.SUCCEEDED,
                cls.FAILED,
                cls.CANCELLED,
                cls.SIDE_EFFECT_UNKNOWN,
            }
        )


class EffectStatus(StrEnum):
    PREPARED = "prepared"
    DISPATCHING = "dispatching"
    SETTLED = "settled"
    FAILED_SAFE = "failed_safe"
    UNKNOWN = "unknown"

    @classmethod
    def terminal(cls) -> frozenset[EffectStatus]:
        return frozenset({cls.SETTLED, cls.FAILED_SAFE, cls.UNKNOWN})


class CodeLeaseState(StrEnum):
    PROVISIONING = "provisioning"
    RUNNING = "running"
    CLEANUP_PENDING = "cleanup_pending"
    DESTROYED = "destroyed"


_RUN_EDGES = {
    RunStatus.QUEUED: frozenset({RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.SIDE_EFFECT_UNKNOWN,
        }
    ),
}
_EFFECT_EDGES = {
    EffectStatus.PREPARED: frozenset({EffectStatus.PREPARED, EffectStatus.DISPATCHING}),
    EffectStatus.DISPATCHING: frozenset(
        {
            EffectStatus.DISPATCHING,
            EffectStatus.SETTLED,
            EffectStatus.FAILED_SAFE,
            EffectStatus.UNKNOWN,
        }
    ),
}
_CODE_LEASE_EDGES = {
    CodeLeaseState.PROVISIONING: frozenset(
        {
            CodeLeaseState.PROVISIONING,
            CodeLeaseState.RUNNING,
            CodeLeaseState.CLEANUP_PENDING,
        }
    ),
    CodeLeaseState.RUNNING: frozenset({CodeLeaseState.RUNNING, CodeLeaseState.CLEANUP_PENDING}),
    CodeLeaseState.CLEANUP_PENDING: frozenset({CodeLeaseState.CLEANUP_PENDING, CodeLeaseState.DESTROYED}),
}


def ensure_run_transition(source: RunStatus, target: RunStatus) -> RunStatus:
    if type(source) is not RunStatus or type(target) is not RunStatus:
        raise TypeError("RunStatus values are required")
    if target not in _RUN_EDGES.get(source, frozenset()):
        raise WorkflowStateConflict(f"invalid Workflow Run transition: {source.value} -> {target.value}")
    return target


def ensure_manual_retry_allowed(status: RunStatus) -> RunStatus:
    if type(status) is not RunStatus:
        raise TypeError("RunStatus is required")
    if status not in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
        raise WorkflowStateConflict(f"manual retry is forbidden for {status.value}")
    return status


def ensure_effect_transition(source: EffectStatus, target: EffectStatus) -> EffectStatus:
    if type(source) is not EffectStatus or type(target) is not EffectStatus:
        raise TypeError("EffectStatus values are required")
    if target not in _EFFECT_EDGES.get(source, frozenset()):
        raise WorkflowStateConflict(f"invalid Workflow effect transition: {source.value} -> {target.value}")
    return target


def ensure_code_lease_transition(
    source: CodeLeaseState,
    target: CodeLeaseState,
) -> CodeLeaseState:
    if type(source) is not CodeLeaseState or type(target) is not CodeLeaseState:
        raise TypeError("CodeLeaseState values are required")
    if target not in _CODE_LEASE_EDGES.get(source, frozenset()):
        raise WorkflowStateConflict(f"invalid Workflow code lease transition: {source.value} -> {target.value}")
    return target


__all__ = [
    "CodeLeaseState",
    "EffectStatus",
    "RunStatus",
    "WorkflowStateConflict",
    "ensure_code_lease_transition",
    "ensure_effect_transition",
    "ensure_manual_retry_allowed",
    "ensure_run_transition",
]
