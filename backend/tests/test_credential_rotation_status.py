from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.credential_repository import CredentialRepository
from app.shared_assets.credential_service import CredentialService
from app.shared_assets.keyring import CredentialKeyring


class _Result:
    def one(self) -> tuple[int, int]:
        return (7, 5)


class _Session:
    def __init__(self) -> None:
        self.execute = AsyncMock(return_value=_Result())


def _actor() -> SystemAssetGovernanceContext:
    return SystemAssetGovernanceContext(
        user_id=uuid.uuid4(),
        request_id="rotation-status-request",
    )


@pytest.mark.asyncio
async def test_rotation_status_repository_matches_rotate_script_eligibility() -> None:
    session = _Session()
    repository = CredentialRepository(session)  # type: ignore[arg-type]

    assert await repository.rotation_status(_actor(), active_key_id="active-key") == (7, 5)

    statement = session.execute.await_args.args[0]
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql = str(compiled)
    assert "credentials.status = 'active'" in sql
    assert "credential_versions.status != 'revoked'" in sql
    assert "credential_envelopes.is_active IS true" in sql
    assert "credential_envelopes.key_id = 'active-key'" in sql


@pytest.mark.asyncio
async def test_rotation_status_service_returns_only_aggregate_counts() -> None:
    keyring = CredentialKeyring(active_key_id="active-key", _keys={"active-key": b"x" * 32})
    service = CredentialService(lambda: None, keyring=keyring)
    service._execute = AsyncMock(return_value=(7, 5))  # type: ignore[method-assign]

    status = await service.rotation_status(_actor())

    assert status.eligible_total == 7
    assert status.current == 5
    assert status.pending == 2
    assert status.status == "pending"
    assert set(vars(status)) == {"eligible_total", "current", "pending", "status"}
