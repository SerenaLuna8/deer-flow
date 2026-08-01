"""Exact database runtime catalog fixtures for real process tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import resolve_system_audit_context
from app.audit.service import AuditService
from app.reliability.owner_refs import AuditHmacKeyring
from app.system_runtime_settings.bootstrap import (
    bootstrap_system_runtime_policies,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.system_settings.models import CreateSystemModel, SystemModelView
from app.system_settings.service import SystemModelCatalogService
from deerflow.persistence.user.model import UserRow


@dataclass(frozen=True, slots=True)
class ProcessRuntimeCatalog:
    """Services and exact model version admitted by a process test."""

    model_catalog: SystemModelCatalogService
    runtime_policy: SystemRuntimePolicyService
    model: SystemModelView


async def seed_process_runtime_catalog(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    logical_name: str,
) -> ProcessRuntimeCatalog:
    """Seed one credential-free model plus the setup-time runtime policies."""

    await bootstrap_system_runtime_policies(session_factory)
    admin_id = uuid.uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            UserRow(
                id=str(admin_id),
                email=f"process-runtime-{admin_id}@example.com",
                password_hash=None,
                system_role="system_admin",
                oauth_provider=None,
                oauth_id=None,
                needs_setup=False,
                token_version=0,
            )
        )
        await session.flush()

    admin = resolve_system_audit_context(
        SimpleNamespace(id=admin_id, system_role="system_admin"),
        request_id=f"process-runtime-{logical_name}",
    )
    model_catalog = SystemModelCatalogService(session_factory)
    model = await model_catalog.create_model(
        admin,
        CreateSystemModel(
            logical_name=logical_name,
            display_name=f"Process fixture {logical_name}",
            description="Credential-free exact model for process-boundary tests",
            status="active",
            provider_adapter="codex_cli",
            provider_model=f"provider/{logical_name}",
            settings={},
            supports_thinking=False,
            supports_reasoning_effort=False,
            supports_vision=False,
            credential_id=None,
            credential_version_id=None,
            credential_env_key=None,
        ),
    )
    runtime_policy = SystemRuntimePolicyService(
        session_factory,
        AuditService(
            session_factory,
            AuditHmacKeyring.from_environment(),
        ),
    )
    return ProcessRuntimeCatalog(
        model_catalog=model_catalog,
        runtime_policy=runtime_policy,
        model=model,
    )


__all__ = [
    "ProcessRuntimeCatalog",
    "seed_process_runtime_catalog",
]
