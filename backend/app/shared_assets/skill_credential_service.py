from __future__ import annotations

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
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import (
    AssetConflict,
    AssetForbidden,
    AssetNotFound,
    AssetStorageUnavailable,
    AssetValidationFailed,
    SharedAssetError,
    SkillCredentialBindingInvalid,
    SkillCredentialSelectionStale,
)
from app.shared_assets.governance_events import SharedAssetGovernanceEventSink
from app.shared_assets.skill_credential_policy import (
    SkillCredentialBindingInput,
    credential_is_eligible,
    normalize_binding_inputs,
    parse_secret_requirements,
    require_complete_bindings,
    require_declared_binding_names,
    validate_selected_credential,
)
from app.shared_assets.skill_credential_repository import (
    EligibleSkillCredentialRecord,
    SkillCredentialRepository,
    SkillCredentialTarget,
)
from deerflow.persistence.shared_assets import (
    ProjectSkillCredentialBindingRow,
    ProjectSkillCredentialConfigRow,
)

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


@dataclass(frozen=True)
class SkillCredentialPublishRequirementView:
    name: str
    optional: bool
    suggested_credential_version_id: uuid.UUID | None
    eligible_credentials: tuple[EligibleSkillCredentialView, ...]


@dataclass(frozen=True)
class SkillPublishPlanView:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    asset_version: int
    payload_checksum: str
    binding_revision: int
    secrets_autonomous: bool
    requirements: tuple[SkillCredentialPublishRequirementView, ...]


@dataclass(frozen=True)
class PreparedSkillCredentialBindings:
    config: ProjectSkillCredentialConfigRow | None
    bindings: tuple[ProjectSkillCredentialBindingRow, ...]
    changed: bool


async def prepare_skill_credential_bindings_in_transaction(
    repository: SkillCredentialRepository,
    actor: ProjectContext | SystemAssetGovernanceContext,
    target: SkillCredentialTarget,
    bindings: Sequence[SkillCredentialBindingInput] | None,
    *,
    expected_revision: int | None,
    require_complete: bool,
) -> PreparedSkillCredentialBindings:
    """Validate and optionally replace exact-version bindings in one transaction.

    The caller already holds Project -> Skill -> SkillVersion locks. This helper
    continues the global order with config/bindings -> Credentials -> versions
    -> active envelopes, and never commits independently.
    """

    if bindings is not None and not isinstance(actor, ProjectContext):
        raise AssetForbidden(actor.request_id)

    config = await repository.get_config(
        actor,
        target.asset.id,
        target.version.id,
        for_update=True,
    )
    existing = await repository.active_bindings(
        actor,
        target.asset.id,
        target.version.id,
        for_update=True,
    )
    current_revision = 0 if config is None else config.revision
    if expected_revision is not None and current_revision != expected_revision:
        raise SkillCredentialSelectionStale(actor.request_id)

    requirements = parse_secret_requirements(
        target.version.secret_requirements,
        request_id=actor.request_id,
    )
    changed = bindings is not None
    if changed:
        if expected_revision is None:
            raise SkillCredentialBindingInvalid(actor.request_id)
        normalized = normalize_binding_inputs(
            bindings,
            request_id=actor.request_id,
        )
        require_declared_binding_names(
            requirements,
            normalized,
            request_id=actor.request_id,
        )
    else:
        if config is None and existing:
            raise SkillCredentialBindingInvalid(actor.request_id)
        active_by_name: dict[str, ProjectSkillCredentialBindingRow] = {}
        for row in existing:
            if config is None or row.skill_version_id != target.version.id or row.config_revision != config.revision or row.secret_name in active_by_name:
                raise SkillCredentialBindingInvalid(actor.request_id)
            active_by_name[row.secret_name] = row
        normalized = tuple(
            SkillCredentialBindingInput(
                name,
                active_by_name[name].credential_version_id,
            )
            for name in sorted(active_by_name)
        )
        require_declared_binding_names(
            requirements,
            normalized,
            request_id=actor.request_id,
        )

    if require_complete:
        require_complete_bindings(
            requirements,
            configured_names=frozenset(item.name for item in normalized),
            request_id=actor.request_id,
        )

    try:
        selected = await repository.lock_selected_credentials(
            actor,
            tuple(item.credential_version_id for item in normalized),
        )
    except AssetNotFound:
        raise SkillCredentialSelectionStale(actor.request_id) from None
    active_envelopes = await repository.lock_active_envelopes(
        tuple(item.credential_version_id for item in normalized),
    )
    existing_by_name = {row.secret_name: row for row in existing}
    records: list[tuple[str, EligibleSkillCredentialRecord]] = []
    for item in normalized:
        record = selected.get(item.credential_version_id)
        if record is None:
            raise SkillCredentialSelectionStale(actor.request_id)
        prior = existing_by_name.get(item.name)
        if not changed and (prior is None or prior.credential_id != record.credential.id):
            raise SkillCredentialBindingInvalid(actor.request_id)
        validate_selected_credential(
            record,
            item.name,
            active_envelope=record.version.id in active_envelopes,
            request_id=actor.request_id,
        )
        records.append((item.name, record))

    if not changed:
        return PreparedSkillCredentialBindings(
            config=config,
            bindings=tuple(existing),
            changed=False,
        )
    if not isinstance(actor, ProjectContext):
        raise AssetForbidden(actor.request_id)
    if config is None:
        config = await repository.create_config(actor, target)
        new_revision = 1
    else:
        new_revision = config.revision + 1
    created = await repository.replace_bindings(
        actor,
        config,
        target,
        tuple(records),
        now=datetime.now(UTC),
        existing=existing,
        new_revision=new_revision,
    )
    return PreparedSkillCredentialBindings(
        config=config,
        bindings=tuple(created),
        changed=True,
    )


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

    async def get_for_version(
        self,
        actor: ProjectContext,
        skill_id: uuid.UUID,
        skill_version_id: uuid.UUID,
    ) -> SkillPublishPlanView:
        self._require_capability(
            actor,
            Capability.SHARED_ASSETS_MANAGE_BINDINGS,
        )
        skill_id = self._validate_skill_id(actor, skill_id)
        skill_version_id = self._validate_skill_id(actor, skill_version_id)

        async def operation(
            repository: SkillCredentialRepository,
        ) -> SkillPublishPlanView:
            await repository.lock_project(actor, read=True)
            target = await repository.lock_configurable_project_skill_version(
                actor,
                skill_id,
                skill_version_id,
                read=True,
            )
            if target.version.workflow_status != "draft":
                raise AssetConflict(actor.request_id)
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
            suggestion_config = config
            suggestion_bindings = bindings
            current_id = target.asset.current_published_version_id
            if config is None and current_id is not None and current_id != target.version.id:
                suggestion_config = await repository.get_config(
                    actor,
                    skill_id,
                    current_id,
                )
                suggestion_bindings = await repository.active_bindings(
                    actor,
                    skill_id,
                    current_id,
                )
            eligible = await repository.eligible_credentials(actor)
            return self._publish_plan_view(
                actor,
                target,
                config,
                suggestion_config,
                suggestion_bindings,
                eligible,
            )

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
        normalized = normalize_binding_inputs(
            bindings,
            request_id=actor.request_id,
        )

        async def operation(
            repository: SkillCredentialRepository,
        ) -> SkillCredentialBindingSetView:
            await repository.lock_project(actor)
            target = await repository.lock_configurable_current_published_skill(
                actor,
                skill_id,
            )
            prepared = await prepare_skill_credential_bindings_in_transaction(
                repository,
                actor,
                target,
                normalized,
                expected_revision=expected,
                require_complete=target.asset.status == "active",
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
                prepared.config,
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
        ] = {name: tuple(item for item in eligible if credential_is_eligible(item, name)) for name, _optional in requirements}
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
    def _publish_plan_view(
        actor: ProjectContext,
        target: SkillCredentialTarget,
        config: ProjectSkillCredentialConfigRow | None,
        suggestion_config: ProjectSkillCredentialConfigRow | None,
        suggestion_bindings: Sequence[ProjectSkillCredentialBindingRow],
        eligible: Sequence[EligibleSkillCredentialRecord],
    ) -> SkillPublishPlanView:
        requirements = SkillCredentialBindingService._requirements(actor, target)
        frontmatter = target.version.frontmatter
        if not isinstance(frontmatter, Mapping):
            raise SkillCredentialBindingInvalid(actor.request_id)
        secrets_autonomous = frontmatter.get("secrets-autonomous", True)
        if not isinstance(secrets_autonomous, bool):
            raise SkillCredentialBindingInvalid(actor.request_id)
        eligible_by_name = {name: tuple(item for item in eligible if credential_is_eligible(item, name)) for name, _optional in requirements}
        suggested_by_name = {row.secret_name: row for row in suggestion_bindings if suggestion_config is not None and row.config_revision == suggestion_config.revision and row.skill_version_id == suggestion_config.skill_version_id}
        views: list[SkillCredentialPublishRequirementView] = []
        for name, optional in requirements:
            options = eligible_by_name[name]
            prior = suggested_by_name.get(name)
            suggestion = next(
                (option.version.id for option in options if prior is not None and option.credential.id == prior.credential_id),
                None,
            )
            views.append(
                SkillCredentialPublishRequirementView(
                    name=name,
                    optional=optional,
                    suggested_credential_version_id=suggestion,
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
        return SkillPublishPlanView(
            skill_id=target.asset.id,
            skill_version_id=target.version.id,
            asset_version=target.asset.version,
            payload_checksum=target.version.payload_checksum,
            binding_revision=0 if config is None else config.revision,
            secrets_autonomous=secrets_autonomous,
            requirements=tuple(views),
        )

    @staticmethod
    def _requirements(
        actor: ProjectContext,
        target: SkillCredentialTarget,
    ) -> tuple[tuple[str, bool], ...]:
        return parse_secret_requirements(
            target.version.secret_requirements,
            request_id=actor.request_id,
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
