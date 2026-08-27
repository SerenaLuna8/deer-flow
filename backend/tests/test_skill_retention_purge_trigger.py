"""Private retention never cleans archived Project Skill packages."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.private_work import retention_purge as retention_module
from app.private_work.retention_purge import (
    RetentionCandidate,
    RetentionPurgeRepository,
)


def _repository() -> RetentionPurgeRepository:
    return object.__new__(RetentionPurgeRepository)


def _install_private_purge_spies(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, uuid.UUID]],
) -> None:
    async def ignore(*_args: object, **_kwargs: object) -> None:
        return None

    async def purge_private_scope(
        _session: object,
        *,
        project_id: uuid.UUID,
        **_kwargs: object,
    ) -> None:
        events.append(("private", project_id))

    monkeypatch.setattr(retention_module, "_purge_execution_approvals", ignore)
    monkeypatch.setattr(retention_module, "release_private_storage_quota", ignore)
    monkeypatch.setattr(retention_module, "purge_private_scope", purge_private_scope)


@pytest.mark.asyncio
async def test_former_owner_retention_purges_only_the_private_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    events: list[tuple[str, uuid.UUID]] = []
    _install_private_purge_spies(monkeypatch, events)
    candidate = RetentionCandidate.former_owner(
        project_id=project_id,
        owner_user_id=str(uuid.uuid4()),
        membership_id=uuid.uuid4(),
        activation_generation=1,
        retention_until=datetime.now(UTC),
        idempotency_key="former-owner-private-purge",
        request_id="former-owner-private-purge",
    )

    await _repository().physically_purge(
        SimpleNamespace(),  # type: ignore[arg-type]
        candidate,
        quota=SimpleNamespace(),  # type: ignore[arg-type]
        approval_audit=SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert events == [("private", project_id)]


@pytest.mark.asyncio
async def test_account_retention_purges_only_each_private_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_ids = (uuid.uuid4(), uuid.uuid4())
    events: list[tuple[str, uuid.UUID]] = []
    _install_private_purge_spies(monkeypatch, events)
    candidate = RetentionCandidate.account(
        owner_user_id=str(uuid.uuid4()),
        project_ids=project_ids,
        account_private_generation=1,
        retention_until=datetime.now(UTC),
        idempotency_key="account-private-purge",
        request_id="account-private-purge",
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
    )

    await _repository().physically_purge(
        session,  # type: ignore[arg-type]
        candidate,
        quota=SimpleNamespace(),  # type: ignore[arg-type]
        approval_audit=SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert events == [("private", project_id) for project_id in candidate.project_ids]
