from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_local_execution_readiness import (
    probe_local_execution_readiness,
)


class _SchemaProbe:
    def __init__(self) -> None:
        self.calls = 0

    async def require_ready(self, _session) -> None:
        self.calls += 1


class _Session:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.statements: list[tuple[str, object]] = []

    async def scalar(self, statement, params=None):
        self.statements.append((str(statement), params))
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize(("result", "expected"), [(True, True), (False, False)])
async def test_execution_readiness_uses_db_clock_and_exact_private_run_capability(
    result: bool,
    expected: bool,
) -> None:
    session = _Session(result)
    schema = _SchemaProbe()

    assert (
        await probe_local_execution_readiness(
            session,  # type: ignore[arg-type]
            worker_fresh_for_seconds=60,
            schema_probe=schema,  # type: ignore[arg-type]
        )
        is expected
    )
    assert schema.calls == 1
    statement, params = session.statements[0]
    assert "clock_timestamp()" in statement
    assert "capabilities_json" in statement
    assert "private_run" in statement
    assert "draining=false" in statement
    assert "count(*) = :expected_worker_count" in statement
    assert "min(max_concurrent_jobs)" in statement
    assert "max(max_concurrent_jobs)" in statement
    assert params == {
        "expected_worker_capacity": 8,
        "expected_worker_count": 1,
        "worker_fresh_for_seconds": 60,
    }


def test_serve_requires_five_child_readback_and_execution_readiness_before_success() -> None:
    source = (Path(__file__).resolve().parents[2] / "scripts" / "serve.sh").read_text()
    child_readback = source.index("readback_required_children")
    readiness = source.index("check_local_execution_readiness.py")
    success = source.index("ActWeave is running!")
    assert child_readback < readiness < success
    assert '"Gateway" "Worker" "Scheduler" "Frontend" "Nginx"' in source
    assert ("env PYTHONPATH=. uv run python scripts/check_local_execution_readiness.py --timeout-seconds 30") in source
