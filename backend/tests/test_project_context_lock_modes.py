from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.projects.context import resolve_project_context_in_transaction


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _Session:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.results = [
            PROJECT_ID,
            SimpleNamespace(
                id=MEMBERSHIP_ID,
                role="admin",
                version=3,
            ),
        ]

    async def execute(self, statement: object) -> _Result:
        self.statements.append(
            str(
                statement.compile(  # type: ignore[attr-defined]
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
        )
        return _Result(self.results.pop(0))


USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
PROJECT_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
MEMBERSHIP_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lock_mode", "suffix"),
    [
        ("share", "FOR SHARE"),
        ("update", "FOR UPDATE"),
    ],
)
async def test_project_context_uses_explicit_project_then_membership_lock_mode(
    lock_mode: str,
    suffix: str,
) -> None:
    session = _Session()

    context = await resolve_project_context_in_transaction(
        session,  # type: ignore[arg-type]
        USER_ID,
        PROJECT_ID,
        "project-lock-mode",
        lock_mode=lock_mode,  # type: ignore[arg-type]
    )

    assert context.project_id == PROJECT_ID
    assert context.membership_id == MEMBERSHIP_ID
    assert len(session.statements) == 2
    assert session.statements[0].endswith(f"{suffix} OF projects")
    assert session.statements[1].endswith(f"{suffix} OF project_memberships")


@pytest.mark.asyncio
async def test_project_context_rejects_ambiguous_legacy_and_explicit_locks() -> None:
    with pytest.raises(TypeError, match="lock and lock_mode"):
        await resolve_project_context_in_transaction(
            _Session(),  # type: ignore[arg-type]
            USER_ID,
            PROJECT_ID,
            "project-lock-mode-conflict",
            lock=True,
            lock_mode="share",
        )
