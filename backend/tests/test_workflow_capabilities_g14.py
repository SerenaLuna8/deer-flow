from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.projects.capabilities import (
    PROJECT_ROLE_CAPABILITIES,
    WORKFLOW_CAPABILITIES,
    Capability,
    capabilities_for,
)
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.workflows.authorization import (
    ProjectWorkflowCapabilityPolicy,
    WorkflowAction,
    WorkflowAuthorizationService,
)
from app.workflows.errors import (
    WorkflowForbidden,
    WorkflowNotFound,
    WorkflowUnavailable,
)

EXPECTED_WORKFLOW_CAPABILITIES = frozenset(
    {
        Capability.WORKFLOW_READ,
        Capability.WORKFLOW_EDIT,
        Capability.WORKFLOW_PUBLISH,
        Capability.WORKFLOW_EXECUTE,
        Capability.WORKFLOW_CODE_USE,
        Capability.WORKFLOW_HTTP_USE,
        Capability.WORKFLOW_HTTP_WRITE,
        Capability.WORKFLOW_CREDENTIAL_GRANT,
        Capability.WORKFLOW_RUN_READ_OWN,
        Capability.WORKFLOW_RUN_CANCEL_OWN,
    }
)

EXPECTED_WORKFLOW_ROLE_MATRIX = {
    ProjectRole.ADMIN: EXPECTED_WORKFLOW_CAPABILITIES,
    ProjectRole.EDITOR: EXPECTED_WORKFLOW_CAPABILITIES - {Capability.WORKFLOW_CREDENTIAL_GRANT},
    ProjectRole.RUNNER: frozenset(
        {
            Capability.WORKFLOW_READ,
            Capability.WORKFLOW_EXECUTE,
            Capability.WORKFLOW_CODE_USE,
            Capability.WORKFLOW_HTTP_USE,
            Capability.WORKFLOW_RUN_READ_OWN,
            Capability.WORKFLOW_RUN_CANCEL_OWN,
        }
    ),
    ProjectRole.VIEWER: frozenset({Capability.WORKFLOW_READ}),
    ProjectRole.CHANNEL_GUEST: frozenset(),
}


def _project_context(role: ProjectRole, *, request_id: str = "req-g14") -> ProjectContext:
    return ProjectContext(
        user_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        project_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        membership_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        role=role,
        capabilities=capabilities_for(role),
        membership_version=7,
        request_id=request_id,
    )


class _Revalidator:
    def __init__(
        self,
        result: ProjectContext | Exception,
    ) -> None:
        self.result = result
        self.calls: list[tuple[PrivateWorkContext, bool]] = []

    async def require(
        self,
        _session: object,
        context: PrivateWorkContext,
        *,
        lock: bool,
    ) -> ProjectContext:
        self.calls.append((context, lock))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_workflow_capability_enum_and_role_matrix_are_exact() -> None:
    assert WORKFLOW_CAPABILITIES == EXPECTED_WORKFLOW_CAPABILITIES
    assert set(PROJECT_ROLE_CAPABILITIES) == set(ProjectRole)
    for role, expected in EXPECTED_WORKFLOW_ROLE_MATRIX.items():
        actual = capabilities_for(role) & WORKFLOW_CAPABILITIES
        assert actual == expected


def test_frontend_closed_workflow_capability_enum_matches_backend() -> None:
    frontend_types = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "core" / "projects" / "types.ts").read_text(encoding="utf-8")
    declaration = re.search(
        r"export const WORKFLOW_CAPABILITIES = \[(?P<values>.*?)\] as const;",
        frontend_types,
        flags=re.DOTALL,
    )
    assert declaration is not None
    frontend_values = re.findall(r'"([^"]+)"', declaration.group("values"))
    assert frontend_values == [
        "workflow.read",
        "workflow.edit",
        "workflow.publish",
        "workflow.execute",
        "workflow.code.use",
        "workflow.http.use",
        "workflow.http.write",
        "workflow.credential.grant",
        "workflow.run.read_own",
        "workflow.run.cancel_own",
    ]
    assert set(frontend_values) == {capability.value for capability in WORKFLOW_CAPABILITIES}


def test_every_non_read_workflow_capability_implies_read_in_role_matrix() -> None:
    for role, capabilities in PROJECT_ROLE_CAPABILITIES.items():
        non_read = (capabilities & WORKFLOW_CAPABILITIES) - {Capability.WORKFLOW_READ}
        if non_read:
            assert Capability.WORKFLOW_READ in capabilities, role


@pytest.mark.parametrize(
    ("action", "required"),
    [
        (WorkflowAction.READ, {Capability.WORKFLOW_READ}),
        (
            WorkflowAction.EDIT,
            {Capability.WORKFLOW_READ, Capability.WORKFLOW_EDIT},
        ),
        (
            WorkflowAction.PUBLISH,
            {Capability.WORKFLOW_READ, Capability.WORKFLOW_PUBLISH},
        ),
        (
            WorkflowAction.EXECUTE,
            {Capability.WORKFLOW_READ, Capability.WORKFLOW_EXECUTE},
        ),
        (
            WorkflowAction.CODE_USE,
            {Capability.WORKFLOW_READ, Capability.WORKFLOW_CODE_USE},
        ),
        (
            WorkflowAction.HTTP_USE,
            {Capability.WORKFLOW_READ, Capability.WORKFLOW_HTTP_USE},
        ),
        (
            WorkflowAction.HTTP_WRITE,
            {
                Capability.WORKFLOW_READ,
                Capability.WORKFLOW_HTTP_USE,
                Capability.WORKFLOW_HTTP_WRITE,
            },
        ),
        (
            WorkflowAction.CREDENTIAL_GRANT,
            {
                Capability.WORKFLOW_READ,
                Capability.WORKFLOW_CREDENTIAL_GRANT,
            },
        ),
        (
            WorkflowAction.RUN_READ_OWN,
            {
                Capability.WORKFLOW_READ,
                Capability.WORKFLOW_RUN_READ_OWN,
            },
        ),
        (
            WorkflowAction.RUN_CANCEL_OWN,
            {
                Capability.WORKFLOW_READ,
                Capability.WORKFLOW_RUN_CANCEL_OWN,
            },
        ),
        (
            WorkflowAction.RETRY,
            {
                Capability.WORKFLOW_READ,
                Capability.WORKFLOW_EXECUTE,
                Capability.WORKFLOW_RUN_READ_OWN,
            },
        ),
    ],
)
def test_project_capability_adapter_requires_the_exact_action_closure(
    action: WorkflowAction,
    required: set[Capability],
) -> None:
    policy = ProjectWorkflowCapabilityPolicy()
    admin = _project_context(ProjectRole.ADMIN)
    assert policy.required_capabilities(action) == frozenset(required)
    assert policy.allows(admin, action)

    for removed in required:
        restricted = ProjectContext(
            user_id=admin.user_id,
            project_id=admin.project_id,
            membership_id=admin.membership_id,
            role=admin.role,
            capabilities=admin.capabilities - {removed},
            membership_version=admin.membership_version,
            request_id=admin.request_id,
        )
        assert not policy.allows(restricted, action), (action, removed)


def test_http_write_and_retry_do_not_accidentally_require_unrelated_capabilities() -> None:
    policy = ProjectWorkflowCapabilityPolicy()
    editor = _project_context(ProjectRole.EDITOR)
    runner = _project_context(ProjectRole.RUNNER)

    assert policy.allows(editor, WorkflowAction.HTTP_WRITE)
    assert not policy.allows(runner, WorkflowAction.HTTP_WRITE)
    assert policy.allows(runner, WorkflowAction.RETRY)
    assert Capability.WORKFLOW_RUN_CANCEL_OWN not in policy.required_capabilities(WorkflowAction.RETRY)
    assert Capability.WORKFLOW_CREDENTIAL_GRANT not in policy.required_capabilities(WorkflowAction.HTTP_WRITE)


@pytest.mark.asyncio
async def test_authorization_revalidates_server_issued_context_before_policy() -> None:
    issued_project = _project_context(ProjectRole.RUNNER)
    context = PrivateWorkContext.from_project(issued_project)
    current = _project_context(ProjectRole.VIEWER)
    revalidator = _Revalidator(current)
    service = WorkflowAuthorizationService(revalidator=revalidator)  # type: ignore[arg-type]

    with pytest.raises(WorkflowForbidden) as caught:
        await service.require(  # type: ignore[arg-type]
            object(),
            context,
            WorkflowAction.EXECUTE,
            lock=True,
        )
    assert caught.value.request_id == "req-g14"
    assert revalidator.calls == [(context, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (PrivateWorkNotFound("req-g14"), WorkflowNotFound),
        (PrivateWorkForbidden("req-g14"), WorkflowForbidden),
        (PrivateWorkUnavailable("req-g14"), WorkflowUnavailable),
    ],
)
async def test_authorization_preserves_not_found_forbidden_and_unavailable_semantics(
    source: Exception,
    expected: type[Exception],
) -> None:
    project = _project_context(ProjectRole.ADMIN)
    context = PrivateWorkContext.from_project(project)
    revalidator = _Revalidator(source)
    service = WorkflowAuthorizationService(revalidator=revalidator)  # type: ignore[arg-type]

    with pytest.raises(expected):
        await service.require(  # type: ignore[arg-type]
            object(),
            context,
            WorkflowAction.READ,
            lock=False,
        )
    assert revalidator.calls == [(context, False)]


def test_authorization_rejects_non_closed_action_values() -> None:
    policy = ProjectWorkflowCapabilityPolicy()
    context = _project_context(ProjectRole.ADMIN)
    with pytest.raises(TypeError, match="WorkflowAction"):
        policy.required_capabilities("execute")  # type: ignore[arg-type]
    assert policy.allows(context, "execute") is False  # type: ignore[arg-type]


def test_capability_policy_rejects_fabricated_context_and_capability_shapes() -> None:
    policy = ProjectWorkflowCapabilityPolicy()
    valid = _project_context(ProjectRole.RUNNER)

    raw_string_capabilities = ProjectContext(
        user_id=valid.user_id,
        project_id=valid.project_id,
        membership_id=valid.membership_id,
        role=valid.role,
        capabilities=frozenset(
            {
                Capability.WORKFLOW_READ.value,
                Capability.WORKFLOW_EXECUTE.value,
            }
        ),  # type: ignore[arg-type]
        membership_version=valid.membership_version,
        request_id=valid.request_id,
    )
    mutable_capabilities = ProjectContext(
        user_id=valid.user_id,
        project_id=valid.project_id,
        membership_id=valid.membership_id,
        role=valid.role,
        capabilities=set(valid.capabilities),  # type: ignore[arg-type]
        membership_version=valid.membership_version,
        request_id=valid.request_id,
    )

    class ProjectContextSubclass(ProjectContext):
        pass

    subclass_context = ProjectContextSubclass(**vars(valid))

    class FakeProjectContext:
        capabilities = valid.capabilities

    assert policy.allows(valid, WorkflowAction.EXECUTE)
    assert not policy.allows(raw_string_capabilities, WorkflowAction.EXECUTE)
    assert not policy.allows(mutable_capabilities, WorkflowAction.EXECUTE)
    assert not policy.allows(subclass_context, WorkflowAction.EXECUTE)
    assert not policy.allows(FakeProjectContext(), WorkflowAction.EXECUTE)  # type: ignore[arg-type]
