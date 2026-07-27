from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
)
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.skill_credential_repository import (
    EligibleSkillCredentialRecord,
    SkillCredentialRepository,
    SkillCredentialTarget,
)
from deerflow.persistence.shared_assets import (
    ProjectSkillCredentialBindingRow,
    ProjectSkillCredentialConfigRow,
)

_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CONFLICT_CONSTRAINTS = frozenset(
    {
        "pk_project_skill_credential_configs",
        "uq_project_skill_credential_configs_revision",
        "uq_project_skill_credential_bindings_active_name",
    }
)
_T = TypeVar("_T")


def _constraint_name(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        value = getattr(current, "constraint_name", None)
        if isinstance(value, str):
            return value
        current = getattr(current, "orig", None) or getattr(
            current,
            "__cause__",
            None,
        )
    return None


@dataclass(frozen=True)
class SkillCredentialBindingInput:
    name: str
    credential_version_id: uuid.UUID


@dataclass(frozen=True)
class EligibleSkillCredentialView:
    credential_id: uuid.UUID
    credential_version_id: uuid.UUID
    display_name: str
    version_number: int


@dataclass(frozen=True)
class SkillCredentialRequirementView:
    name: str
    optional: bool
    configured: bool
    credential_id: uuid.UUID | None
    credential_version_id: uuid.UUID | None
    credential_display_name: str | None
    credential_version_number: int | None
    eligible_credentials: tuple[EligibleSkillCredentialView, ...]


@dataclass(frozen=True)
class SkillCredentialBindingSetView:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    revision: int
    requirements: tuple[SkillCredentialRequirementView, ...]


class SkillCredentialBindingService:
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        governance_sink: SharedAssetGovernanceEventSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._governance_sink = governance_sink or SharedAssetGovernanceEventSink()

    async def get(
        self,
        actor: ProjectContext,
        skill_id: uuid.UUID,
    ) -> SkillCredentialBindingSetView:
        self._require_capability(actor, Capability.SHARED_ASSETS_READ)
        skill_id = self._validate_skill_id(actor, skill_id)

        async def operation(
            repository: SkillCredentialRepository,
        ) -> SkillCredentialBindingSetView:
            await repository.lock_project(actor, read=True)
            target = await repository.lock_configurable_current_published_skill(
                actor,
                skill_id,
                read=True,
            )
            config = await repository.get_config(
                actor,
                skill_id,
                target.version.id,
            )
            bindings = await repository.active_bindings(
                actor,
                skill_id,
                target.version.id,
            )
            eligible = await repository.eligible_credentials(actor)
            return self._view(actor, target, config, bindings, eligible)

        return await self._execute(actor, operation)

    async def replace(
        self,
        actor: ProjectContext,
        skill_id: uuid.UUID,
        bindings: Sequence[SkillCredentialBindingInput],
        *,
        expected_revision: int,
    ) -> SkillCredentialBindingSetView:
        self._require_capability(actor, Capability.MCP_CREDENTIALS_APPROVE)
        skill_id = self._validate_skill_id(actor, skill_id)
        expected = self._validate_expected_revision(actor, expected_revision)
        normalized = self._validate_bindings(actor, bindings)

        async def operation(
            repository: SkillCredentialRepository,
        ) -> SkillCredentialBindingSetView:
            await repository.lock_project(actor)
            target = await repository.lock_configurable_current_published_skill(
                actor,
                skill_id,
            )
            config = await repository.get_config(
                actor,
                skill_id,
                target.version.id,
                for_update=True,
            )
            current_revision = 0 if config is None else config.revision
            if current_revision != expected:
                raise AssetConflict(actor.request_id)

            requirements = self._requirements(actor, target)
            requirement_names = {name for name, _optional in requirements}
            if any(item.name not in requirement_names for item in normalized):
                raise AssetValidationFailed(actor.request_id)

            selected = await repository.lock_selected_credentials(
                actor,
                tuple(item.credential_version_id for item in normalized),
            )
            records: list[tuple[str, EligibleSkillCredentialRecord]] = []
            for item in normalized:
                record = selected.get(item.credential_version_id)
                if record is None or not self._credential_is_eligible(
                    record,
                    item.name,
                ):
                    raise AssetValidationFailed(actor.request_id)
                if not await repository.active_envelope_exists(
                    record.version.id,
                ):
                    raise AssetValidationFailed(actor.request_id)
                records.append((item.name, record))

            existing = await repository.active_bindings(
                actor,
                skill_id,
                target.version.id,
                for_update=True,
            )
            if config is None:
                config = await repository.create_config(actor, target)
                new_revision = 1
            else:
                new_revision = config.revision + 1
            await repository.replace_bindings(
                actor,
                config,
                target,
                tuple(records),
                now=datetime.now(UTC),
                existing=existing,
                new_revision=new_revision,
            )
            eligible = await repository.eligible_credentials(actor)
            current_bindings = await repository.active_bindings(
                actor,
                skill_id,
                target.version.id,
            )
            return self._view(
                actor,
                target,
                config,
                current_bindings,
                eligible,
            )

        return await self._execute(
            actor,
            operation,
            governance=lambda session, result: self._record_governance(
                session,
                actor,
                result,
            ),
        )

    async def _execute(
        self,
        actor: ProjectContext,
        operation: Callable[[SkillCredentialRepository], Awaitable[_T]],
        governance: Callable[[AsyncSession, _T], Awaitable[None]] | None = None,
    ) -> _T:
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    result = await operation(
                        SkillCredentialRepository(session),
                    )
                    if governance is not None:
                        await governance(session, result)
                    return result
        except SharedAssetError:
            raise
        except IntegrityError as exc:
            if _constraint_name(exc) in _CONFLICT_CONSTRAINTS:
                raise AssetConflict(actor.request_id) from None
            raise AssetStorageUnavailable(actor.request_id) from None
        except (DBAPIError, SATimeoutError):
            raise AssetStorageUnavailable(actor.request_id) from None

    async def _record_governance(
        self,
        session: AsyncSession,
        actor: ProjectContext,
        result: SkillCredentialBindingSetView,
    ) -> None:
        await self._governance_sink.append_project(
            session,
            actor=actor.user_id,
            project_id=actor.project_id,
            asset_id=result.skill_id,
            version_id=result.skill_version_id,
            action="skill.credential_bindings.configure",
            request_id=actor.request_id,
            asset_kind="skill",
        )

    @staticmethod
    def _view(
        actor: ProjectContext,
        target: SkillCredentialTarget,
        config: ProjectSkillCredentialConfigRow | None,
        bindings: Sequence[ProjectSkillCredentialBindingRow],
        eligible: Sequence[EligibleSkillCredentialRecord],
    ) -> SkillCredentialBindingSetView:
        requirements = SkillCredentialBindingService._requirements(
            actor,
            target,
        )
        eligible_by_name: dict[
            str,
            tuple[EligibleSkillCredentialRecord, ...],
        ] = {
            name: tuple(
                item
                for item in eligible
                if SkillCredentialBindingService._credential_is_eligible(
                    item,
                    name,
                )
            )
            for name, _optional in requirements
        }
        active_by_name = {row.secret_name: row for row in bindings if config is not None and row.skill_version_id == target.version.id and row.config_revision == config.revision}
        views: list[SkillCredentialRequirementView] = []
        for name, optional in requirements:
            options = eligible_by_name[name]
            option_by_version = {item.version.id: item for item in options}
            binding = active_by_name.get(name)
            selected = option_by_version.get(binding.credential_version_id) if binding is not None else None
            views.append(
                SkillCredentialRequirementView(
                    name=name,
                    optional=optional,
                    configured=selected is not None,
                    credential_id=(selected.credential.id if selected is not None else None),
                    credential_version_id=(selected.version.id if selected is not None else None),
                    credential_display_name=(selected.credential.display_name if selected is not None else None),
                    credential_version_number=(selected.version.version_number if selected is not None else None),
                    eligible_credentials=tuple(
                        EligibleSkillCredentialView(
                            credential_id=item.credential.id,
                            credential_version_id=item.version.id,
                            display_name=item.credential.display_name,
                            version_number=item.version.version_number,
                        )
                        for item in options
                    ),
                )
            )
        return SkillCredentialBindingSetView(
            skill_id=target.asset.id,
            skill_version_id=target.version.id,
            revision=0 if config is None else config.revision,
            requirements=tuple(views),
        )

    @staticmethod
    def _requirements(
        actor: ProjectContext,
        target: SkillCredentialTarget,
    ) -> tuple[tuple[str, bool], ...]:
        request_id = actor.request_id
        raw = target.version.secret_requirements
        if not isinstance(raw, list):
            raise AssetValidationFailed(request_id)
        requirements: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping) or set(item) - {"name", "optional"} or not isinstance(item.get("name"), str) or not isinstance(item.get("optional", False), bool):
                raise AssetValidationFailed(request_id)
            name = item["name"]
            optional = item.get("optional", False)
            if _ENV_NAME_PATTERN.fullmatch(name) is None or name in seen:
                raise AssetValidationFailed(request_id)
            seen.add(name)
            requirements.append((name, optional))
        return tuple(requirements)

    @staticmethod
    def _credential_is_eligible(
        record: EligibleSkillCredentialRecord,
        name: str,
    ) -> bool:
        env = record.version.payload_schema.get("env")
        return (
            record.credential.scope == "project"
            and record.credential.status == "active"
            and record.credential.current_version_id == record.version.id
            and record.version.status == "active"
            and isinstance(env, list)
            and all(isinstance(item, str) for item in env)
            and name in env
        )

    @staticmethod
    def _require_capability(
        actor: ProjectContext,
        capability: Capability,
    ) -> None:
        if isinstance(actor, ProjectContext) and capability in actor.capabilities:
            return
        raise AssetForbidden(getattr(actor, "request_id", "unknown"))

    @staticmethod
    def _validate_skill_id(
        actor: ProjectContext,
        skill_id: uuid.UUID,
    ) -> uuid.UUID:
        if not isinstance(skill_id, uuid.UUID):
            raise AssetValidationFailed(actor.request_id)
        return skill_id

    @staticmethod
    def _validate_expected_revision(
        actor: ProjectContext,
        expected: int,
    ) -> int:
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise AssetConflict(actor.request_id)
        return expected

    @staticmethod
    def _validate_bindings(
        actor: ProjectContext,
        bindings: Sequence[SkillCredentialBindingInput],
    ) -> tuple[SkillCredentialBindingInput, ...]:
        try:
            normalized = tuple(bindings)
        except TypeError:
            raise AssetValidationFailed(actor.request_id) from None
        if len(normalized) > 256:
            raise AssetValidationFailed(actor.request_id)
        seen: set[str] = set()
        for item in normalized:
            if not isinstance(item, SkillCredentialBindingInput) or _ENV_NAME_PATTERN.fullmatch(item.name) is None or not isinstance(item.credential_version_id, uuid.UUID) or item.name in seen:
                raise AssetValidationFailed(actor.request_id)
            seen.add(item.name)
        return normalized
