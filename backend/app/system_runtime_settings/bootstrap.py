"""Idempotent setup-time bootstrap for the complete runtime-policy catalog."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap_identities import BUILTIN_MODEL_EMAIL, BUILTIN_MODEL_USER_ID, BUILTIN_MODEL_USERNAME
from app.system_runtime_settings.models import (
    RuntimePolicySection,
    default_policy_value,
)
from app.system_runtime_settings.repository import (
    SystemRuntimePolicyRepository,
    SystemRuntimePolicyRepositoryInvariant,
)
from app.system_runtime_settings.validation import (
    RuntimePolicyInvalid,
    canonical_policy_payload,
)
from deerflow.persistence.projects import ProjectMembershipRow
from deerflow.persistence.system_runtime_settings import (
    SystemRuntimePolicyCatalogStateRow,
    SystemRuntimePolicyRow,
    SystemRuntimePolicyVersionRow,
)
from deerflow.persistence.user import UserRow

_BOOTSTRAP_LOCK_KEY = 0x0DEE_12F1_5254_504C
_ID_NAMESPACE = uuid.UUID("e80287de-83d9-5d3a-a4c8-df0eeaa2a955")


class SystemRuntimePolicyBootstrapConflict(RuntimeError):
    def __init__(self) -> None:
        super().__init__("SYSTEM_RUNTIME_POLICY_BOOTSTRAP_CONFLICT")


class SystemRuntimePolicyBootstrapStorageUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("SYSTEM_RUNTIME_POLICY_BOOTSTRAP_STORAGE_UNAVAILABLE")


def _version_id(section: RuntimePolicySection) -> uuid.UUID:
    return uuid.uuid5(_ID_NAMESPACE, f"{section.value}:version:1")


async def _ensure_bootstrap_principal(session: AsyncSession) -> None:
    principal_id = str(BUILTIN_MODEL_USER_ID)
    principal = await session.get(UserRow, principal_id, with_for_update=True)
    if principal is None:
        session.add(
            UserRow(
                id=principal_id,
                email=BUILTIN_MODEL_EMAIL,
                username=BUILTIN_MODEL_USERNAME,
                password_hash=None,
                system_role="user",
                oauth_provider=None,
                oauth_id=None,
                needs_setup=False,
                token_version=0,
            )
        )
        await session.flush()
    elif (
        principal.email != BUILTIN_MODEL_EMAIL
        or principal.username != BUILTIN_MODEL_USERNAME
        or principal.password_hash is not None
        or principal.system_role != "user"
        or principal.oauth_provider is not None
        or principal.oauth_id is not None
        or principal.needs_setup
        or principal.token_version != 0
    ):
        raise SystemRuntimePolicyBootstrapConflict
    membership = await session.scalar(select(ProjectMembershipRow.id).where(ProjectMembershipRow.user_id == principal_id).limit(1))
    if membership is not None:
        raise SystemRuntimePolicyBootstrapConflict


async def _validate_existing_catalog(
    repository: SystemRuntimePolicyRepository,
    state: SystemRuntimePolicyCatalogStateRow,
) -> None:
    try:
        rows = await repository.list_current()
        for policy, version in rows:
            section = RuntimePolicySection(policy.section)
            canonical = canonical_policy_payload(section, dict(version.value))
            if (
                policy.current_version_id != version.id
                or int(policy.revision) != int(version.version_number)
                or int(policy.revision) < 1
                or int(version.schema_version) != canonical.schema_version
                or version.payload_checksum != canonical.checksum
                or int(state.revision) < int(policy.revision)
            ):
                raise SystemRuntimePolicyBootstrapConflict
    except (
        RuntimePolicyInvalid,
        SystemRuntimePolicyRepositoryInvariant,
        TypeError,
        ValueError,
    ):
        raise SystemRuntimePolicyBootstrapConflict from None


async def bootstrap_system_runtime_policies(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Seed v1 defaults or validate a complete previously bootstrapped catalog."""

    if not callable(session_factory):
        raise TypeError("session factory is required")
    try:
        async with session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _BOOTSTRAP_LOCK_KEY},
            )
            repository = SystemRuntimePolicyRepository(session)
            state = await repository.catalog_state(for_update=True)
            policy_count = int(
                await session.scalar(
                    select(func.count()).select_from(SystemRuntimePolicyRow),
                )
                or 0
            )
            version_count = int(
                await session.scalar(
                    select(func.count()).select_from(SystemRuntimePolicyVersionRow),
                )
                or 0
            )
            if policy_count or version_count:
                if policy_count != len(RuntimePolicySection) or version_count < len(RuntimePolicySection):
                    raise SystemRuntimePolicyBootstrapConflict
                await _validate_existing_catalog(repository, state)
                return int(state.revision)
            if int(state.revision) != 1:
                raise SystemRuntimePolicyBootstrapConflict

            await _ensure_bootstrap_principal(session)
            actor_id = str(BUILTIN_MODEL_USER_ID)
            policies: list[SystemRuntimePolicyRow] = []
            versions: list[SystemRuntimePolicyVersionRow] = []
            for section in RuntimePolicySection:
                canonical = canonical_policy_payload(
                    section,
                    default_policy_value(section),
                )
                version_id = _version_id(section)
                policies.append(
                    SystemRuntimePolicyRow(
                        section=section.value,
                        current_version_id=version_id,
                        revision=1,
                        updated_by_user_id=actor_id,
                    )
                )
                versions.append(
                    SystemRuntimePolicyVersionRow(
                        id=version_id,
                        section=section.value,
                        version_number=1,
                        schema_version=canonical.schema_version,
                        value=canonical.value,
                        payload_checksum=canonical.checksum,
                        created_by_user_id=actor_id,
                    )
                )
            session.add_all(policies)
            session.add_all(versions)
            state.updated_by_user_id = actor_id
            await session.flush()
            return int(state.revision)
    except SystemRuntimePolicyBootstrapConflict:
        raise
    except (DBAPIError, IntegrityError, RuntimeError):
        raise SystemRuntimePolicyBootstrapStorageUnavailable from None


__all__ = [
    "SystemRuntimePolicyBootstrapConflict",
    "SystemRuntimePolicyBootstrapStorageUnavailable",
    "bootstrap_system_runtime_policies",
]
