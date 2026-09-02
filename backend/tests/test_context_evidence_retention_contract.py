"""Offline ordering and scope gates for Context Evidence retention."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from app.private_work import thread_repository as thread_repository_module
from app.private_work.retention_purge import purge_private_scope
from app.private_work.thread_repository import PrivateThreadRepository
from deerflow.runtime.private_scope import PrivateResourceScope

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _Result:
    rowcount = 0

    def scalar_one_or_none(self):
        return "thread"

    def scalars(self):
        return self

    def all(self):
        return []

    def one_or_none(self):
        return None

    def __iter__(self):
        return iter(())


class _RecordingSession:
    def __init__(self) -> None:
        self.statements: list[object] = []
        self.events: list[str] = []

    async def execute(self, statement, parameters=None):
        self.statements.append(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.events.append(sql)
        return _Result()

    async def scalar(self, statement, parameters=None):
        self.statements.append(statement)
        if "clock_timestamp" in str(statement):
            return datetime.now(UTC)
        return None


def _sql(statement: object) -> str:
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).split()).lower()


@pytest.mark.asyncio
async def test_bulk_retention_authorizes_and_deletes_exact_context_scope_before_threads() -> None:
    session = _RecordingSession()
    project_id = uuid.uuid4()
    owner_user_id = str(uuid.uuid4())

    await purge_private_scope(
        session,  # type: ignore[arg-type]
        project_id=project_id,
        owner_user_id=owner_user_id,
    )

    statements = [_sql(statement) for statement in session.statements]
    authority_insert = next(index for index, sql in enumerate(statements) if "insert into pg_temp.context_evidence_retention_authority" in sql)
    evidence_delete = next(index for index, sql in enumerate(statements) if sql.startswith("delete from context_evidence "))
    head_delete = next(index for index, sql in enumerate(statements) if sql.startswith("delete from context_projection_heads "))
    sequence_delete = next(index for index, sql in enumerate(statements) if sql.startswith("delete from context_evidence_sequences "))
    thread_delete = next(index for index, sql in enumerate(statements) if sql.startswith("delete from threads_meta "))

    authority_sql = statements[authority_insert]
    assert "select thread.project_id, thread.owner_user_id, thread.thread_id" in authority_sql
    assert "thread.project_id=" in authority_sql
    assert "thread.owner_user_id=" in authority_sql
    assert authority_insert < evidence_delete < head_delete < sequence_delete < thread_delete
    assert all("disable trigger" not in sql for sql in statements)


@pytest.mark.asyncio
async def test_compensated_create_purges_context_before_thread_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _RecordingSession()
    scope = PrivateResourceScope(
        project_id=str(uuid.uuid4()),
        owner_user_id=str(uuid.uuid4()),
        membership_version=1,
    )
    events = session.events

    class _ContextEvidenceRepository:
        def __init__(self, bound_session) -> None:
            assert bound_session is session

        async def purge_thread(self, _scope) -> None:
            events.append("context-purged")

    monkeypatch.setattr(
        thread_repository_module,
        "ContextEvidenceRepository",
        _ContextEvidenceRepository,
        raising=False,
    )
    now = datetime.now(UTC)
    await PrivateThreadRepository(session).purge_compensated_create(  # type: ignore[arg-type]
        scope=scope,
        thread_id="thread",
        expected_created_at=now,
        expected_deleted_at=now,
    )

    context_index = events.index("context-purged")
    thread_delete_index = next(index for index, event in enumerate(events) if event.lower().startswith("delete from threads_meta"))
    assert context_index < thread_delete_index


def test_frontend_has_no_single_run_delete_call_or_ui_mutation_path() -> None:
    router_source = (_REPOSITORY_ROOT / "backend/app/gateway/routers/private_work_routes/runs.py").read_text(encoding="utf-8")
    assert "async def delete_private_run(" in router_source

    sdk_run_delete = re.compile(
        r"\.runs\s*(?:\.\s*delete|\[\s*['\"]delete['\"]\s*\])\s*\(",
    )
    run_delete_symbol = re.compile(
        r"(?:delete|Delete|remove|Remove)(?:Private)?Run(?:s)?",
    )
    exact_run_path = re.compile(
        r"`[^`\n]*/threads/\$\{[^}]+\}/runs/\$\{[^}]+\}`",
    )
    delete_method = re.compile(r"method\s*:\s*['\"]DELETE['\"]")
    violations: list[str] = []
    frontend_source = _REPOSITORY_ROOT / "frontend/src"
    for path in sorted(frontend_source.rglob("*")):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        source = path.read_text(encoding="utf-8")
        if sdk_run_delete.search(source) or run_delete_symbol.search(source) or (exact_run_path.search(source) and delete_method.search(source)):
            violations.append(str(path.relative_to(_REPOSITORY_ROOT)))

    assert violations == []
