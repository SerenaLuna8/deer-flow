from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.quotas import service as quota_service_module
from app.quotas.models import QuotaSourceRef, QuotaUnavailable
from app.quotas.service import QuotaService
from app.quotas.system_policy import SystemQuotaPolicyReader
from app.system_runtime_settings import (
    QuotaPolicyValue,
    RuntimePolicySection,
)
from app.system_runtime_settings.materializer import (
    SystemRuntimePolicyMaterializer,
)
from deerflow.config.quota_config import QuotaConfig


def _source_ref(_payload: bytes) -> QuotaSourceRef:
    return QuotaSourceRef(key_id="quota-test", hmac_hex="a" * 64)


@pytest.mark.parametrize(
    "script_name",
    ("import_project_skills.py", "reconcile_usage.py"),
)
def test_operator_quota_scripts_bind_the_database_policy_reader(
    script_name: str,
) -> None:
    source = (Path(__file__).resolve().parents[1] / "scripts" / script_name).read_text(encoding="utf-8")
    assert "current_policy_reader=SystemQuotaPolicyReader()" in source


@pytest.mark.anyio
async def test_system_quota_reader_materializes_in_the_caller_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_session = object()
    materialize = AsyncMock(
        return_value=QuotaPolicyValue(
            default_member_limit=11,
            default_storage_bytes_limit=12,
            default_concurrent_run_limit=2,
            default_mcp_calls_daily_limit=13,
            warning_threshold=0.6,
        ),
    )
    monkeypatch.setattr(
        SystemRuntimePolicyMaterializer,
        "materialize_current_in_session",
        materialize,
    )

    config = await SystemQuotaPolicyReader().read_current_quotas(
        caller_session,
    )

    assert config == QuotaConfig(
        default_member_limit=11,
        default_storage_bytes_limit=12,
        default_concurrent_run_limit=2,
        default_mcp_calls_daily_limit=13,
        warning_threshold=0.6,
    )
    materialize.assert_awaited_once_with(
        caller_session,
        RuntimePolicySection.QUOTAS,
    )


@pytest.mark.anyio
async def test_effective_limit_reads_current_policy_in_the_caller_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_session = object()
    values = iter(
        (
            QuotaConfig(default_concurrent_run_limit=2),
            QuotaConfig(default_concurrent_run_limit=5),
        )
    )
    calls: list[object] = []

    class CurrentPolicyReader:
        async def read_current_quotas(self, session):
            calls.append(session)
            return next(values)

    class Repository:
        def __init__(self, session) -> None:
            assert session is caller_session

        async def policy(self, _project_id):
            return None

    monkeypatch.setattr(quota_service_module, "QuotaRepository", Repository)
    service = QuotaService(
        object(),
        QuotaConfig(default_concurrent_run_limit=99),
        source_ref_hasher=_source_ref,
        current_policy_reader=CurrentPolicyReader(),
    )

    first = await service.effective_limit(
        caller_session,
        "91a8bb0e-3d35-4a09-901a-0cab36ec14b2",
        "concurrent_runs",
    )
    second = await service.effective_limit(
        caller_session,
        "91a8bb0e-3d35-4a09-901a-0cab36ec14b2",
        "concurrent_runs",
    )

    assert (first, second) == (2, 5)
    assert calls == [caller_session, caller_session]


@pytest.mark.anyio
async def test_current_quota_policy_failure_never_falls_back_to_startup_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailablePolicyReader:
        async def read_current_quotas(self, _session):
            raise RuntimeError("sensitive database detail")

    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def policy(self, _project_id):
            return SimpleNamespace(concurrent_run_limit=None)

    monkeypatch.setattr(quota_service_module, "QuotaRepository", Repository)
    service = QuotaService(
        object(),
        QuotaConfig(default_concurrent_run_limit=99),
        source_ref_hasher=_source_ref,
        current_policy_reader=UnavailablePolicyReader(),
    )

    with pytest.raises(QuotaUnavailable) as raised:
        await service.effective_limit(
            object(),
            "91a8bb0e-3d35-4a09-901a-0cab36ec14b2",
            "concurrent_runs",
        )

    assert "sensitive database detail" not in str(raised.value)
