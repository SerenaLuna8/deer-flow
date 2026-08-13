"""Adapter from the system runtime-policy catalog to Automation admission."""

from __future__ import annotations

import asyncio
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.system_runtime_settings import (
    AutomationsPolicyValue,
    RuntimePolicySection,
)
from app.system_runtime_settings.materializer import (
    SystemRuntimePolicyMaterializer,
)


class AutomationsPolicyUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Automations policy unavailable")


class AutomationsPolicyPort(Protocol):
    async def read_current(
        self,
        session: AsyncSession,
    ) -> AutomationsPolicyValue: ...


class SystemAutomationsPolicyReader:
    """Materialize current Automation policy inside the business transaction."""

    async def read_current(
        self,
        session: AsyncSession,
    ) -> AutomationsPolicyValue:
        try:
            policy = await SystemRuntimePolicyMaterializer.materialize_current_in_session(
                session,
                RuntimePolicySection.AUTOMATIONS,
            )
            if type(policy) is not AutomationsPolicyValue:
                raise TypeError
            return policy
        except asyncio.CancelledError:
            raise
        except Exception:
            raise AutomationsPolicyUnavailable from None


async def current_automations_policy(
    session: AsyncSession,
    reader: AutomationsPolicyPort | None,
    *,
    fallback: AutomationsPolicyValue,
) -> AutomationsPolicyValue:
    if reader is None:
        return fallback
    return await reader.read_current(session)


__all__ = [
    "AutomationsPolicyPort",
    "AutomationsPolicyUnavailable",
    "SystemAutomationsPolicyReader",
    "current_automations_policy",
]
