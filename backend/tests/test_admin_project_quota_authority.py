"""System-admin project quota authority rejects fabricated contexts."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.audit.models import SystemAuditContext
from app.quotas.models import ProjectQuotaLimits, QuotaForbidden
from app.quotas.service import QuotaService
from deerflow.config.quota_config import QuotaConfig


def _service() -> QuotaService:
    return QuotaService(
        session_factory=SimpleNamespace(),  # type: ignore[arg-type]
        config=QuotaConfig(),
        source_ref_hasher=lambda payload: SimpleNamespace(key_id="k", hmac_hex="00"),
    )


def _fabricated_system_context() -> SystemAuditContext:
    context = object.__new__(SystemAuditContext)
    object.__setattr__(context, "user_id", uuid.uuid4())
    object.__setattr__(context, "request_id", "fabricated-request")
    return context


@pytest.mark.asyncio
async def test_system_admin_set_limits_rejects_unissued_context() -> None:
    service = _service()
    with pytest.raises(QuotaForbidden):
        await service.set_limits_as_system_admin(
            SimpleNamespace(),  # type: ignore[arg-type]
            _fabricated_system_context(),
            uuid.uuid4(),
            ProjectQuotaLimits(
                member_limit=1,
                storage_bytes_limit=None,
                concurrent_run_limit=None,
                mcp_calls_daily_limit=None,
            ),
            expected_version=0,
        )


@pytest.mark.asyncio
async def test_system_admin_read_usage_rejects_unissued_context() -> None:
    service = _service()
    with pytest.raises(QuotaForbidden):
        await service.read_usage_as_system_admin(
            SimpleNamespace(),  # type: ignore[arg-type]
            _fabricated_system_context(),
            uuid.uuid4(),
        )
