"""Adapter from the system runtime-policy catalog to quota enforcement."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.quotas.models import QuotaUnavailable
from app.system_runtime_settings import (
    QuotaPolicyValue,
    RuntimePolicySection,
)
from app.system_runtime_settings.materializer import (
    SystemRuntimePolicyMaterializer,
)
from deerflow.config.quota_config import QuotaConfig


class SystemQuotaPolicyReader:
    """Materialize current quota defaults inside the business transaction."""

    async def read_current_quotas(
        self,
        session: AsyncSession,
    ) -> QuotaConfig:
        try:
            policy = await SystemRuntimePolicyMaterializer.materialize_current_in_session(
                session,
                RuntimePolicySection.QUOTAS,
            )
            if type(policy) is not QuotaPolicyValue:
                raise TypeError
            return QuotaConfig.model_validate(
                policy.model_dump(mode="python"),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise QuotaUnavailable from None


__all__ = ["SystemQuotaPolicyReader"]
