from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.private_work.execution_approval import _asset_closure


class _Rows:
    def __init__(self, rows: tuple[object, ...]) -> None:
        self._rows = rows

    def scalars(self) -> tuple[object, ...]:
        return self._rows


class _ClosureSession:
    def __init__(self, source_env_field_name: str) -> None:
        self._results = iter(
            (
                _Rows(()),
                _Rows(()),
                _Rows(
                    (
                        SimpleNamespace(
                            skill_id=uuid.UUID(
                                "30000000-0000-0000-0000-000000000001",
                            ),
                            skill_version_id=uuid.UUID(
                                "30000000-0000-0000-0000-000000000002",
                            ),
                            secret_name="TARGET_API_KEY",
                            source_env_field_name=source_env_field_name,
                            skill_credential_binding_id=uuid.UUID(
                                "30000000-0000-0000-0000-000000000003",
                            ),
                            binding_revision=7,
                            credential_id=uuid.UUID(
                                "30000000-0000-0000-0000-000000000004",
                            ),
                            credential_version_id=uuid.UUID(
                                "30000000-0000-0000-0000-000000000005",
                            ),
                        ),
                    ),
                ),
                _Rows(()),
                _Rows(()),
            ),
        )

    async def execute(self, _statement: object) -> _Rows:
        return next(self._results)


@pytest.mark.asyncio
async def test_host_approval_asset_closure_detects_skill_source_field_drift() -> None:
    values = {
        "project_id": uuid.uuid4(),
        "owner_user_id": str(uuid.uuid4()),
        "run_id": "source-field-closure-run",
    }

    provider_token = await _asset_closure(
        _ClosureSession("PROVIDER_TOKEN"),  # type: ignore[arg-type]
        **values,
    )
    rotated_field = await _asset_closure(
        _ClosureSession("ROTATED_PROVIDER_TOKEN"),  # type: ignore[arg-type]
        **values,
    )

    assert provider_token[2][0][2:4] == (
        "TARGET_API_KEY",
        "PROVIDER_TOKEN",
    )
    assert rotated_field[2][0][2:4] == (
        "TARGET_API_KEY",
        "ROTATED_PROVIDER_TOKEN",
    )
    assert provider_token != rotated_field
