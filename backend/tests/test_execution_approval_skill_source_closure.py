from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.private_work.execution_approval_worker import _asset_closure


class _Rows:
    def __init__(self, rows: tuple[object, ...]) -> None:
        self._rows = rows

    def scalars(self) -> tuple[object, ...]:
        return self._rows

    def all(self) -> tuple[object, ...]:
        return self._rows


class _ClosureSession:
    def __init__(self, generation_id: uuid.UUID) -> None:
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
                            secret_revision=7,
                            secret_generation_id=generation_id,
                            secret_generation_digest="a" * 64,
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
async def test_host_approval_asset_closure_detects_skill_generation_drift() -> None:
    values = {
        "project_id": uuid.uuid4(),
        "owner_user_id": str(uuid.uuid4()),
        "run_id": "source-field-closure-run",
    }

    first_generation = uuid.UUID("30000000-0000-0000-0000-000000000003")
    second_generation = uuid.UUID("30000000-0000-0000-0000-000000000004")
    first = await _asset_closure(
        _ClosureSession(first_generation),  # type: ignore[arg-type]
        **values,
    )
    replaced = await _asset_closure(
        _ClosureSession(second_generation),  # type: ignore[arg-type]
        **values,
    )

    assert first[2][0][2:] == (
        "TARGET_API_KEY",
        7,
        str(first_generation),
        "a" * 64,
    )
    assert replaced[2][0][2:] == (
        "TARGET_API_KEY",
        7,
        str(second_generation),
        "a" * 64,
    )
    assert first != replaced
