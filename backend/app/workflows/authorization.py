"""Workflow action authorization over server-issued Project capabilities.

Membership authority is always re-resolved and, for mutations, locked in the
existing Project -> Membership order.  Domain actions map to closed Project
capability conjunctions; neither request fields nor project roles are accepted
as authority at this boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.revalidation import PrivateWorkRevalidator
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.workflows.errors import (
    WorkflowForbidden,
    WorkflowNotFound,
    WorkflowUnavailable,
)


class WorkflowAction(StrEnum):
    # Domain actions, not public Project capability literals. Callers combine
    # an operation action with CODE_USE/HTTP_USE when a graph needs them.
    READ = "read"
    EDIT = "edit"
    PUBLISH = "publish"
    EXECUTE = "execute"
    CODE_USE = "code.use"
    HTTP_USE = "http.use"
    HTTP_WRITE = "http.write"
    CREDENTIAL_GRANT = "credential.grant"
    RUN_READ_OWN = "run.read_own"
    RUN_CANCEL_OWN = "run.cancel_own"
    RETRY = "retry"


class WorkflowCapabilityPolicy(Protocol):
    def allows(
        self,
        context: ProjectContext,
        action: WorkflowAction,
    ) -> bool: ...


_ACTION_CAPABILITIES: dict[WorkflowAction, frozenset[Capability]] = {
    WorkflowAction.READ: frozenset({Capability.WORKFLOW_READ}),
    WorkflowAction.EDIT: frozenset(
        {
            Capability.WORKFLOW_READ,
            Capability.WORKFLOW_EDIT,
        }
    ),
    WorkflowAction.PUBLISH: frozenset(
        {
            Capability.WORKFLOW_READ,
            Capability.WORKFLOW_PUBLISH,
        }
    ),
    WorkflowAction.EXECUTE: frozenset(
        {
            Capability.WORKFLOW_READ,
            Capability.WORKFLOW_EXECUTE,
        }
    ),
    WorkflowAction.CODE_USE: frozenset(
        {
            Capability.WORKFLOW_READ,
            Capability.WORKFLOW_CODE_USE,
        }
    ),
    WorkflowAction.HTTP_USE: frozenset(
        {
            Capability.WORKFLOW_READ,
            Capability.WORKFLOW_HTTP_USE,
        }
    ),
    WorkflowAction.HTTP_WRITE: frozenset(
        {
            Capability.WORKFLOW_READ,
            Capability.WORKFLOW_HTTP_USE,
            Capability.WORKFLOW_HTTP_WRITE,
        }
    ),
    WorkflowAction.CREDENTIAL_GRANT: frozenset(
        {
            Capability.WORKFLOW_READ,
            Capability.WORKFLOW_CREDENTIAL_GRANT,
        }
    ),
    WorkflowAction.RUN_READ_OWN: frozenset(
        {
            Capability.WORKFLOW_READ,
            Capability.WORKFLOW_RUN_READ_OWN,
        }
    ),
    WorkflowAction.RUN_CANCEL_OWN: frozenset(
        {
            Capability.WORKFLOW_READ,
            Capability.WORKFLOW_RUN_CANCEL_OWN,
        }
    ),
    WorkflowAction.RETRY: frozenset(
        {
            Capability.WORKFLOW_READ,
            Capability.WORKFLOW_EXECUTE,
            Capability.WORKFLOW_RUN_READ_OWN,
        }
    ),
}

assert set(_ACTION_CAPABILITIES) == set(WorkflowAction)
assert all(Capability.WORKFLOW_READ in required for required in _ACTION_CAPABILITIES.values())


class ProjectWorkflowCapabilityPolicy:
    @staticmethod
    def required_capabilities(action: WorkflowAction) -> frozenset[Capability]:
        if type(action) is not WorkflowAction:
            raise TypeError("WorkflowAction is required")
        return _ACTION_CAPABILITIES[action]

    def allows(
        self,
        context: ProjectContext,
        action: WorkflowAction,
    ) -> bool:
        if type(context) is not ProjectContext or type(action) is not WorkflowAction or type(context.capabilities) is not frozenset or any(type(capability) is not Capability for capability in context.capabilities):
            return False
        return self.required_capabilities(action) <= context.capabilities


class WorkflowAuthorizationService:
    def __init__(
        self,
        *,
        policy: WorkflowCapabilityPolicy | None = None,
        revalidator: PrivateWorkRevalidator | None = None,
    ) -> None:
        self._policy = policy if policy is not None else ProjectWorkflowCapabilityPolicy()
        self._revalidator = revalidator if revalidator is not None else PrivateWorkRevalidator()

    async def require(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        action: WorkflowAction,
        *,
        lock: bool,
    ) -> ProjectContext:
        if type(action) is not WorkflowAction:
            raise TypeError("WorkflowAction is required")
        try:
            current = await self._revalidator.require(
                session,
                context,
                lock=lock,
            )
        except PrivateWorkNotFound:
            raise WorkflowNotFound(context.request_id) from None
        except PrivateWorkForbidden:
            raise WorkflowForbidden(context.request_id) from None
        except PrivateWorkUnavailable:
            raise WorkflowUnavailable(context.request_id) from None
        if not self._policy.allows(current, action):
            raise WorkflowForbidden(context.request_id)
        return current


__all__ = [
    "ProjectWorkflowCapabilityPolicy",
    "WorkflowAction",
    "WorkflowAuthorizationService",
    "WorkflowCapabilityPolicy",
]
