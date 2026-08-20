from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypeVar

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
    env_fields: tuple[str, ...]


@dataclass(frozen=True)
class SkillCredentialRequirementView:
    name: str
    optional: bool
    configured: bool
    mapping_status: Literal["missing", "configured", "invalid"]
    credential_id: uuid.UUID | None
    credential_version_id: uuid.UUID | None
    credential_display_name: str | None
    credential_version_number: int | None
    source_env_field_name: str | None
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
    mapping_status: Literal["missing", "configured", "invalid"]


@dataclass(frozen=True)
class SkillPublishPlanView:
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    asset_version: int
    payload_checksum: str
    binding_revision: int
    secrets_autonomous: bool
    ready: bool
    required_count: int
    configured_required_count: int
    invalid_count: int
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
                active_by_name[name].source_env_field_name,
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
    records: list[tuple[str, str, EligibleSkillCredentialRecord]] = []
    for item in normalized:
        record = selected.get(item.credential_version_id)
        if record is None:
            raise SkillCredentialSelectionStale(actor.request_id)
        prior = existing_by_name.get(item.name)
        if not changed and (prior is None or prior.credential_id != record.credential.id):
            raise SkillCredentialBindingInvalid(actor.request_id)
        validate_selected_credential(
            record,
            item.source_env_field_name,
            active_envelope=record.version.id in active_envelopes,
            request_id=actor.request_id,
        )
        records.append((item.name, item.source_env_field_name, record))

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
            expose_credentials = Capability.MCP_CREDENTIALS_APPROVE in actor.capabilities
            eligible = await repository.eligible_credentials(actor)
            return self._view(
                actor,
                target,
                config,
                bindings,
                eligible,
                expose_credentials=expose_credentials,
            )

        return await self._execute(actor, operation)

    async def get_exact(
        self,
        actor: ProjectContext,
        skill_id: uuid.UUID,
        skill_version_id: uuid.UUID,
    ) -> SkillCredentialBindingSetView:
        """Read one exact version's secret-free mapping state.

        Credential identities and source field metadata are governance metadata,
        so ordinary Skill readers receive only completion status. Approvers see
        the selectable Credential versions and their declared ``env`` fields.
        """

        self._require_capability(actor, Capability.SHARED_ASSETS_READ)
        skill_id = self._validate_skill_id(actor, skill_id)
        skill_version_id = self._validate_skill_id(actor, skill_version_id)

        async def operation(
            repository: SkillCredentialRepository,
        ) -> SkillCredentialBindingSetView:
            await repository.lock_project(actor, read=True)
            target = await repository.lock_configurable_exact_skill_version(
                actor,
                skill_id,
                skill_version_id,
                read=True,
            )
            if target.asset.scope == "system" and not (target.version.workflow_status == "published" and target.asset.current_published_version_id == target.version.id):
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
            expose_credentials = Capability.MCP_CREDENTIALS_APPROVE in actor.capabilities
            eligible = await repository.eligible_credentials(actor)
            return self._view(
                actor,
                target,
                config,
                bindings,
                eligible,
                expose_credentials=expose_credentials,
            )

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
            eligible = await repository.eligible_credentials(actor)
            return self._publish_plan_view(
                actor,
                target,
                config,
                bindings,
                eligible,
            )

        return await self._execute(actor, operation)

    async def replace(
        self,
        actor: ProjectContext,
        skill_id: uuid.UUID,
        bindings: Sequence[SkillCredentialBindingInput],
        *,
        expected_skill_version_id: uuid.UUID,
        expected_revision: int,
    ) -> SkillCredentialBindingSetView:
        self._require_capability(actor, Capability.MCP_CREDENTIALS_APPROVE)
        skill_id = self._validate_skill_id(actor, skill_id)
        expected_version_id = self._validate_skill_id(
            actor,
            expected_skill_version_id,
        )
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
            if target.version.id != expected_version_id:
                raise SkillCredentialSelectionStale(actor.request_id)
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
                expose_credentials=True,
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

    async def replace_for_version(
        self,
        actor: ProjectContext,
        skill_id: uuid.UUID,
        skill_version_id: uuid.UUID,
        bindings: Sequence[SkillCredentialBindingInput],
        *,
        expected_revision: int,
    ) -> SkillCredentialBindingSetView:
        """CAS-replace mappings for an exact draft or current-published version."""

        self._require_capability(actor, Capability.MCP_CREDENTIALS_APPROVE)
        skill_id = self._validate_skill_id(actor, skill_id)
        skill_version_id = self._validate_skill_id(actor, skill_version_id)
        expected = self._validate_expected_revision(actor, expected_revision)
        normalized = normalize_binding_inputs(
            bindings,
            request_id=actor.request_id,
        )

        async def operation(
            repository: SkillCredentialRepository,
        ) -> SkillCredentialBindingSetView:
            await repository.lock_project(actor)
            target = await repository.lock_configurable_exact_skill_version(
                actor,
                skill_id,
                skill_version_id,
            )
            workflow_status = target.version.workflow_status
            current_published_id = target.asset.current_published_version_id
            project_writable = target.asset.scope == "project" and (workflow_status == "draft" or (workflow_status == "published" and current_published_id == target.version.id))
            system_writable = target.asset.scope == "system" and workflow_status == "published" and current_published_id == target.version.id
            if not project_writable and not system_writable:
                raise AssetConflict(actor.request_id)
            prepared = await prepare_skill_credential_bindings_in_transaction(
                repository,
                actor,
                target,
                normalized,
                expected_revision=expected,
                require_complete=(workflow_status == "published" and target.asset.status == "active"),
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
                expose_credentials=True,
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
        *,
        expose_credentials: bool,
    ) -> SkillCredentialBindingSetView:
        requirements = SkillCredentialBindingService._requirements(
            actor,
            target,
        )
        eligible_with_fields = tuple((item, SkillCredentialBindingService._env_fields(item)) for item in eligible)
        eligible_with_fields = tuple((item, fields) for item, fields in eligible_with_fields if fields)
        eligible_by_version = {item.version.id: item for item, _fields in eligible_with_fields}
        fields_by_version = {item.version.id: fields for item, fields in eligible_with_fields}
        active_by_name = {row.secret_name: row for row in bindings if config is not None and row.skill_version_id == target.version.id and row.config_revision == config.revision}
        views: list[SkillCredentialRequirementView] = []
        for name, optional in requirements:
            binding = active_by_name.get(name)
            source_env_field_name = binding.source_env_field_name if binding is not None else None
            selected = eligible_by_version.get(binding.credential_version_id) if binding is not None else None
            configured = bool(selected is not None and isinstance(source_env_field_name, str) and source_env_field_name in fields_by_version.get(selected.version.id, ()))
            mapping_status: Literal["missing", "configured", "invalid"] = "configured" if configured else "invalid" if binding is not None else "missing"
            views.append(
                SkillCredentialRequirementView(
                    name=name,
                    optional=optional,
                    configured=binding is not None,
                    mapping_status=mapping_status,
                    credential_id=(binding.credential_id if expose_credentials and binding is not None else None),
                    credential_version_id=(binding.credential_version_id if expose_credentials and binding is not None else None),
                    credential_display_name=(selected.credential.display_name if expose_credentials and selected is not None else None),
                    credential_version_number=(selected.version.version_number if expose_credentials and selected is not None else None),
                    source_env_field_name=(source_env_field_name if expose_credentials else None),
                    eligible_credentials=(
                        tuple(
                            EligibleSkillCredentialView(
                                credential_id=item.credential.id,
                                credential_version_id=item.version.id,
                                display_name=item.credential.display_name,
                                version_number=item.version.version_number,
                                env_fields=fields,
                            )
                            for item, fields in eligible_with_fields
                        )
                        if expose_credentials
                        else ()
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
        bindings: Sequence[ProjectSkillCredentialBindingRow],
        eligible: Sequence[EligibleSkillCredentialRecord],
    ) -> SkillPublishPlanView:
        requirements = SkillCredentialBindingService._requirements(actor, target)
        frontmatter = target.version.frontmatter
        if not isinstance(frontmatter, Mapping):
            raise SkillCredentialBindingInvalid(actor.request_id)
        secrets_autonomous = frontmatter.get("secrets-autonomous", True)
        if not isinstance(secrets_autonomous, bool):
            raise SkillCredentialBindingInvalid(actor.request_id)
        eligible_by_version = {item.version.id: item for item in eligible}
        active_by_name = {row.secret_name: row for row in bindings if config is not None and row.config_revision == config.revision and row.skill_version_id == target.version.id}
        views: list[SkillCredentialPublishRequirementView] = []
        for name, optional in requirements:
            binding = active_by_name.get(name)
            record = eligible_by_version.get(binding.credential_version_id) if binding is not None else None
            configured = bool(
                record is not None
                and credential_is_eligible(
                    record,
                    binding.source_env_field_name,
                )
            )
            mapping_status: Literal["missing", "configured", "invalid"] = "configured" if configured else "invalid" if binding is not None else "missing"
            views.append(
                SkillCredentialPublishRequirementView(
                    name=name,
                    optional=optional,
                    mapping_status=mapping_status,
                )
            )
        required_count = sum(not optional for _name, optional in requirements)
        configured_required_count = sum(not view.optional and view.mapping_status == "configured" for view in views)
        invalid_count = sum(view.mapping_status == "invalid" for view in views)
        return SkillPublishPlanView(
            skill_id=target.asset.id,
            skill_version_id=target.version.id,
            asset_version=target.asset.version,
            payload_checksum=target.version.payload_checksum,
            binding_revision=0 if config is None else config.revision,
            secrets_autonomous=secrets_autonomous,
            ready=(configured_required_count == required_count and invalid_count == 0),
            required_count=required_count,
            configured_required_count=configured_required_count,
            invalid_count=invalid_count,
            requirements=tuple(views),
        )

    @staticmethod
    def _env_fields(
        record: EligibleSkillCredentialRecord,
    ) -> tuple[str, ...]:
        env = record.version.payload_schema.get("env")
        if not isinstance(env, list) or not all(isinstance(item, str) for item in env):
            return ()
        return tuple(dict.fromkeys(field for field in env if credential_is_eligible(record, field)))

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
