from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from support.m4_private_threads import seed_m4_thread_database

from app.audit.models import (
    AuditAction,
    AuditActor,
    AuditMetadataRejected,
    AuditOutcome,
    AuditTarget,
    AuditTargetKind,
)
from app.audit.service import AuditService
from app.quotas.models import QuotaExceeded, QuotaSourceRef
from app.quotas.service import QuotaService
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.audit.model import AuditLogRow


def _keyring() -> AuditHmacKeyring:
    return AuditHmacKeyring(
        active_key_id="release-audit-v1",
        _keys={"release-audit-v1": b"a" * 32},
    )


def _source_ref(payload: bytes) -> QuotaSourceRef:
    return QuotaSourceRef(
        key_id="release-quota-v1",
        hmac_hex=hmac.new(b"q" * 32, payload, hashlib.sha256).hexdigest(),
    )


async def _child_state(
    process: asyncio.subprocess.Process,
    expected: str,
    *,
    timeout: float = 10,
) -> dict[str, object]:
    assert process.stdout is not None
    deadline = asyncio.get_running_loop().time() + timeout
    lines: list[str] = []
    while asyncio.get_running_loop().time() < deadline:
        remaining = deadline - asyncio.get_running_loop().time()
        line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
        if not line:
            stderr = b""
            if process.stderr is not None:
                stderr = await process.stderr.read()
            raise AssertionError(f"scheduler child exited before {expected}: {stderr.decode(errors='replace')}")
        decoded = line.decode().strip()
        lines.append(decoded)
        payload = json.loads(decoded)
        if payload.get("state") == expected:
            return payload
    raise AssertionError(f"scheduler child did not reach {expected}: {lines}")


async def _stop_child(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=5)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_scheduler_lock_takeover_uses_a_different_process_and_session(
    migrated_postgres_database_url: str,
) -> None:
    child_path = Path(__file__).parent / "support" / "m6_scheduler_ownership_child.py"
    command = (sys.executable, str(child_path), str(migrated_postgres_database_url))
    environment = os.environ.copy()
    backend_root = str(Path(__file__).parent.parent)
    environment["PYTHONPATH"] = os.pathsep.join(value for value in (backend_root, environment.get("PYTHONPATH", "")) if value)
    first = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    second: asyncio.subprocess.Process | None = None
    try:
        first_owned = await _child_state(first, "owned")
        second = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        await _child_state(second, "contended")

        first.kill()
        await asyncio.wait_for(first.wait(), timeout=5)
        second_owned = await _child_state(second, "owned")

        assert first_owned["backend_pid"] != second_owned["backend_pid"]
        assert first.returncode == -signal.SIGKILL
    finally:
        await _stop_child(first)
        if second is not None:
            await _stop_child(second)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_quota_race_and_audit_redaction_are_fail_closed(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quota = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    secret = f"release-secret-{uuid.uuid4()}"
    audit = AuditService(seed.factory, _keyring())
    try:
        held = await quota.reserve_new_session(
            seed.owner_a,
            "concurrent_runs",
            1,
            "release:held",
        )
        contenders = await asyncio.gather(
            *(
                quota.reserve_new_session(
                    seed.owner_a,
                    "concurrent_runs",
                    1,
                    f"release:race:{index}",
                )
                for index in range(5)
            ),
            return_exceptions=True,
        )
        accepted = [item for item in contenders if not isinstance(item, Exception)]
        rejected = [item for item in contenders if isinstance(item, Exception)]
        assert len(accepted) == 2
        assert len(rejected) == 3
        assert all(isinstance(item, QuotaExceeded) for item in rejected)
        assert sum(item.threshold_crossed for item in (held, *accepted)) == 1

        actor = AuditActor.user(seed.owner_a.user_id)
        target = AuditTarget(
            kind=AuditTargetKind.RUN,
            authority_id=uuid.uuid4(),
            project_id=seed.owner_a.project_id,
        )
        async with seed.factory() as session, session.begin():
            with pytest.raises(AuditMetadataRejected) as captured:
                await audit.append(
                    session,
                    actor,
                    AuditAction.RUN_ADMITTED,
                    target,
                    AuditOutcome.SUCCESS,
                    {
                        "job_type": "private_run",
                        "non_interactive": False,
                        "token": secret,
                    },
                )
            assert secret not in str(captured.value)
            assert secret not in repr(captured.value)
            await audit.append(
                session,
                actor,
                AuditAction.RUN_ADMITTED,
                target,
                AuditOutcome.SUCCESS,
                {"job_type": "private_run", "non_interactive": False},
            )

        async with seed.factory() as session:
            rows = (
                await session.execute(
                    select(AuditLogRow).where(
                        AuditLogRow.project_id == seed.owner_a.project_id,
                    )
                )
            ).scalars()
            encoded = json.dumps(
                [row.metadata_json for row in rows],
                sort_keys=True,
            )
        assert secret not in encoded
    finally:
        await seed.engine.dispose()


def test_cross_platform_release_runner_requires_url_and_fails_a_real_child_skip(tmp_path: Path) -> None:
    backend_tests = Path(__file__).parent
    runner = backend_tests / "support" / "release_gate_plugin.py"
    child = tmp_path / "child"
    child.mkdir()
    (child / "conftest.py").write_text(
        f"import sys\nsys.path.insert(0, {str(backend_tests)!r})\nfrom support.release_gate_plugin import pytest_sessionfinish\n",
        encoding="utf-8",
    )
    (child / "test_skip.py").write_text(
        "import pytest\n\ndef test_release_skip():\n    pytest.skip('release evidence unavailable')\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("POSTGRES_TEST_URL", None)
    missing_url = subprocess.run(
        [sys.executable, str(runner), "test_skip.py", "-q"],
        cwd=child,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert missing_url.returncode != 0
    assert "POSTGRES_TEST_URL is required for the PostgreSQL release gate" in (missing_url.stdout + missing_url.stderr)

    environment["POSTGRES_TEST_URL"] = "postgresql://release.invalid/postgres"
    environment["DEER_FLOW_RELEASE_GATE_LABEL"] = "M1-M7"
    result = subprocess.run(
        [sys.executable, str(runner), "test_skip.py", "-q"],
        cwd=child,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "M1-M7 release stats: collected=1 passed=0 failed=0 skipped=1" in output


def test_removed_database_migration_clis_do_not_return() -> None:
    backend = Path(__file__).parent.parent
    root_makefile = backend.parent.joinpath("Makefile").read_text(encoding="utf-8")
    backend_makefile = backend.joinpath("Makefile").read_text(encoding="utf-8")
    for stem in (
        "migrate_sqlite_to_postgres",
        "migrate_assets",
        "migrate_private_work",
        "migrate_automations",
        "migrate_reliability",
    ):
        assert not backend.joinpath("scripts", f"{stem}.py").exists()
    for target in (
        "migrate-sqlite:",
        "migrate-assets:",
        "migrate-private-work:",
        "migrate-automations:",
        "migrate-reliability:",
    ):
        assert target not in root_makefile
        assert target not in backend_makefile
