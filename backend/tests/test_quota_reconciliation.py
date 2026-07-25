from __future__ import annotations

import uuid

import pytest

from app.quotas.reconciliation import QuotaReconciler


@pytest.mark.asyncio
async def test_storage_reconciliation_counts_private_files_and_all_project_skill_versions() -> None:
    class Session:
        def __init__(self) -> None:
            self.statements: list[str] = []

        async def scalar(self, statement):
            sql = str(statement)
            self.statements.append(sql)
            if "skill_version_files" in sql:
                assert "skills.scope" in sql
                assert "skills.project_id" in sql
                assert "workflow_status" not in sql
                assert "skills.status" not in sql
                return 37
            if "FROM files" in sql:
                return 100
            raise AssertionError(f"unexpected authoritative storage query: {sql}")

    session = Session()
    reconciler = object.__new__(QuotaReconciler)

    expected = await reconciler._expected(
        session,  # type: ignore[arg-type]
        uuid.uuid4(),
        "storage_bytes",
        "lifetime",
    )

    assert expected == 137
    assert len(session.statements) == 2
