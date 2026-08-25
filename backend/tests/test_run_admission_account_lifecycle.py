from __future__ import annotations

import uuid

import pytest

from app.private_work.account_private_lifecycle import (
    AccountPrivateLifecycleClosed,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkForbidden
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()


class _SessionFactory:
    def __call__(self) -> _Session:
        return _Session()


class _Revalidator:
    def __init__(self, current: ProjectContext) -> None:
        self.current = current
        self.called = False

    async def require(self, *_args: object, **_kwargs: object) -> ProjectContext:
        self.called = True
        return self.current


class _ClosedLifecycle:
    def __init__(self) -> None:
        self.called = False

    async def require_active_after_membership(
        self,
        _session: object,
        owner_user_id: uuid.UUID | str,
    ) -> None:
        self.called = True
        assert owner_user_id == OWNER_ID
        raise AccountPrivateLifecycleClosed


class _MustNotResolve:
    async def resolve_run_asset_closure_in_session(self, *_args: object) -> None:
        raise AssertionError("asset resolution must follow the account lifecycle guard")


class _MustNotPersist:
    async def list_asset_facts_in_session(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        raise AssertionError("persistence must follow the account lifecycle guard")


OWNER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
PROJECT_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
MEMBERSHIP_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


@pytest.mark.asyncio
async def test_run_admission_checks_account_lifecycle_after_membership_guard() -> None:
    project = ProjectContext(
        user_id=OWNER_ID,
        project_id=PROJECT_ID,
        membership_id=MEMBERSHIP_ID,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="account-lifecycle-run-admission",
    )
    context = PrivateWorkContext.from_project(project)
    revalidator = _Revalidator(project)
    lifecycle = _ClosedLifecycle()
    service = PrivateRunAdmissionService(
        _SessionFactory(),  # type: ignore[arg-type]
        resolver=_MustNotResolve(),  # type: ignore[arg-type]
        revalidator=revalidator,  # type: ignore[arg-type]
        snapshots=_MustNotPersist(),  # type: ignore[arg-type]
        account_private_lifecycle=lifecycle,  # type: ignore[arg-type]
    )

    with pytest.raises(PrivateWorkForbidden):
        await service.admit(
            context,
            str(uuid.uuid4()),
            PrivateRunCreate(run_id=str(uuid.uuid4())),
        )

    assert revalidator.called
    assert lifecycle.called
