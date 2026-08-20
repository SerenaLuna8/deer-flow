from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import (
    AssetConflict,
    AssetNotFound,
    SkillCredentialBindingsIncomplete,
    SkillCredentialSelectionStale,
)
from app.shared_assets.skill_credential_repository import (
    EligibleSkillCredentialRecord,
    SkillCredentialTarget,
)
from app.shared_assets.skill_credential_service import (
    SkillCredentialBindingInput,
    SkillCredentialBindingService,
    prepare_skill_credential_bindings_in_transaction,
)


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="req-skill-credential-service",
    )


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def begin(self):
        return self


def _target(
    actor: ProjectContext,
    *,
    status: str,
    current_version_id: uuid.UUID | None,
    version_id: uuid.UUID | None = None,
) -> SkillCredentialTarget:
    skill_id = uuid.uuid4()
    selected_version_id = version_id or uuid.uuid4()
    return SkillCredentialTarget(
        SimpleNamespace(
            id=skill_id,
            scope="project",
            project_id=actor.project_id,
            status=status,
            revision=8,
            current_version_id=current_version_id,
        ),
        SimpleNamespace(
            id=selected_version_id,
            skill_id=skill_id,
            version_number=2,
            supersedes_version_id=current_version_id,
            revoked_at=None,
            payload_checksum="a" * 64,
            frontmatter={"secrets-autonomous": True},
            secret_requirements=[
                {"name": "API_KEY", "optional": False},
                {"name": "OPTIONAL_TOKEN", "optional": True},
            ],
        ),
    )


def _eligible(
    actor: ProjectContext,
    *,
    env: list[str],
) -> EligibleSkillCredentialRecord:
    credential_id = uuid.uuid4()
    version_id = uuid.uuid4()
    return EligibleSkillCredentialRecord(
        SimpleNamespace(
            id=credential_id,
            scope="project",
            project_id=actor.project_id,
            status="active",
            is_delete=False,
            current_version_id=version_id,
            display_name="Project secret",
        ),
        SimpleNamespace(
            id=version_id,
            credential_id=credential_id,
            version_number=3,
            status="active",
            payload_schema={"env": env},
        ),
    )


@pytest.mark.asyncio
async def test_get_for_version_builds_read_only_candidate_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context()
    previous_version_id = uuid.uuid4()
    target = _target(
        actor,
        status="active",
        current_version_id=previous_version_id,
    )
    target.version.frontmatter = {}
    eligible = _eligible(actor, env=["API_KEY"])
    previous_config = SimpleNamespace(
        revision=4,
        skill_version_id=previous_version_id,
    )
    previous_binding = SimpleNamespace(
        skill_version_id=previous_version_id,
        config_revision=4,
        secret_name="API_KEY",
        credential_id=eligible.credential.id,
        credential_version_id=uuid.uuid4(),
    )

    class _Repository:
        def __init__(self, _session):
            self.lock_project = AsyncMock()

        async def lock_configurable_project_skill_version(
            self,
            _actor,
            _skill_id,
            _version_id,
            *,
            read=False,
        ):
            assert read is True
            return target

        async def get_config(self, _actor, _skill_id, version_id, **_kwargs):
            return previous_config if version_id == previous_version_id else None

        async def active_bindings(self, _actor, _skill_id, version_id, **_kwargs):
            return (previous_binding,) if version_id == previous_version_id else ()

        async def eligible_credentials(self, _actor):
            return (eligible,)

    monkeypatch.setattr(
        "app.shared_assets.skill_credential_service.SkillCredentialRepository",
        _Repository,
    )
    service = SkillCredentialBindingService(lambda: _Session())
    monkeypatch.setattr(
        service,
        "_is_project_candidate",
        AsyncMock(return_value=True),
    )

    plan = await service.get_for_version(actor, target.asset.id, target.version.id)

    assert plan.skill_id == target.asset.id
    assert plan.skill_version_id == target.version.id
    assert plan.revision == 8
    assert plan.payload_checksum == "a" * 64
    assert plan.binding_revision == 0
    assert plan.secrets_autonomous is True
    assert plan.ready is False
    assert plan.required_count == 1
    assert plan.configured_required_count == 0
    assert plan.invalid_count == 0
    assert plan.requirements[0].mapping_status == "missing"
    assert plan.requirements[1].mapping_status == "missing"


@pytest.mark.asyncio
async def test_editor_can_open_candidate_activation_readiness() -> None:
    actor = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="req-editor-activation-readiness",
    )

    def unexpected_session():
        raise AssertionError("permission denial must happen before storage access")

    with pytest.raises(AssertionError, match="permission denial"):
        await SkillCredentialBindingService(unexpected_session).get_for_version(
            actor,
            uuid.uuid4(),
            uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_replace_rejects_removing_required_binding_from_active_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context()
    current_version_id = uuid.uuid4()
    target = _target(
        actor,
        status="active",
        current_version_id=current_version_id,
        version_id=current_version_id,
    )
    config = SimpleNamespace(revision=2)
    existing = (
        SimpleNamespace(
            skill_version_id=current_version_id,
            config_revision=2,
            secret_name="API_KEY",
        ),
    )

    class _Repository:
        replace_bindings = AsyncMock()
        create_config = AsyncMock()

        def __init__(self, _session):
            self.lock_project = AsyncMock()

        async def lock_configurable_current_skill(self, *_args, **_kwargs):
            return target

        async def get_config(self, *_args, **_kwargs):
            return config

        async def active_bindings(self, *_args, **_kwargs):
            return existing

        async def lock_selected_credentials(self, *_args, **_kwargs):
            return {}

        async def lock_active_envelopes(self, *_args, **_kwargs):
            return frozenset()

    monkeypatch.setattr(
        "app.shared_assets.skill_credential_service.SkillCredentialRepository",
        _Repository,
    )
    service = SkillCredentialBindingService(lambda: _Session())

    with pytest.raises(SkillCredentialBindingsIncomplete):
        await service.replace(
            actor,
            target.asset.id,
            (),
            expected_skill_version_id=current_version_id,
            expected_revision=2,
        )

    _Repository.replace_bindings.assert_not_awaited()


@pytest.mark.asyncio
async def test_replace_rejects_stale_current_version_before_binding_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context()
    current_version_id = uuid.uuid4()
    stale_version_id = uuid.uuid4()
    target = _target(
        actor,
        status="active",
        current_version_id=current_version_id,
        version_id=current_version_id,
    )

    class _Repository:
        lock_project = AsyncMock()
        lock_configurable_current_skill = AsyncMock(
            return_value=target,
        )
        get_config = AsyncMock()
        active_bindings = AsyncMock()
        create_config = AsyncMock()
        replace_bindings = AsyncMock()
        eligible_credentials = AsyncMock()

        def __init__(self, _session):
            pass

    monkeypatch.setattr(
        "app.shared_assets.skill_credential_service.SkillCredentialRepository",
        _Repository,
    )
    governance_sink = SimpleNamespace(append_project=AsyncMock())
    service = SkillCredentialBindingService(
        lambda: _Session(),
        governance_sink=governance_sink,
    )

    with pytest.raises(SkillCredentialSelectionStale) as exc_info:
        await service.replace(
            actor,
            target.asset.id,
            (),
            expected_skill_version_id=stale_version_id,
            expected_revision=0,
        )

    assert exc_info.value.code == "SKILL_CREDENTIAL_SELECTION_STALE"
    repository = _Repository
    repository.lock_project.assert_awaited_once_with(actor)
    repository.lock_configurable_current_skill.assert_awaited_once_with(
        actor,
        target.asset.id,
    )
    repository.get_config.assert_not_awaited()
    repository.active_bindings.assert_not_awaited()
    repository.create_config.assert_not_awaited()
    repository.replace_bindings.assert_not_awaited()
    repository.eligible_credentials.assert_not_awaited()
    governance_sink.append_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_credential_disappearance_collapses_to_stable_stale_error() -> None:
    actor = _context()
    target = _target(
        actor,
        status="active",
        current_version_id=uuid.uuid4(),
    )
    missing_version_id = uuid.uuid4()
    repository = SimpleNamespace(
        get_config=AsyncMock(return_value=None),
        active_bindings=AsyncMock(return_value=()),
        lock_selected_credentials=AsyncMock(
            side_effect=AssetNotFound(actor.request_id),
        ),
        lock_active_envelopes=AsyncMock(),
    )

    with pytest.raises(SkillCredentialSelectionStale) as exc_info:
        await prepare_skill_credential_bindings_in_transaction(
            repository,
            actor,
            target,
            (SkillCredentialBindingInput("API_KEY", missing_version_id),),
            expected_revision=0,
            require_complete=True,
        )

    assert exc_info.value.code == "SKILL_CREDENTIAL_SELECTION_STALE"
    repository.lock_active_envelopes.assert_not_awaited()


def test_exact_version_view_supports_alias_mapping_and_redacts_for_non_approver() -> None:
    actor = _context()
    target = _target(
        actor,
        status="suspended",
        current_version_id=None,
    )
    eligible = _eligible(actor, env=["DB_DATABASE", "DB_PASSWORD"])
    config = SimpleNamespace(
        revision=2,
        skill_version_id=target.version.id,
    )
    binding = SimpleNamespace(
        skill_version_id=target.version.id,
        config_revision=2,
        secret_name="API_KEY",
        source_env_field_name="DB_DATABASE",
        credential_id=eligible.credential.id,
        credential_version_id=eligible.version.id,
    )

    privileged = SkillCredentialBindingService._view(  # noqa: SLF001
        actor,
        target,
        config,
        (binding,),
        (eligible,),
        expose_credentials=True,
    )
    requirement = privileged.requirements[0]
    assert requirement.configured is True
    assert requirement.mapping_status == "configured"
    assert requirement.source_env_field_name == "DB_DATABASE"
    assert requirement.eligible_credentials[0].env_fields == (
        "DB_DATABASE",
        "DB_PASSWORD",
    )

    redacted = SkillCredentialBindingService._view(  # noqa: SLF001
        actor,
        target,
        config,
        (binding,),
        (eligible,),
        expose_credentials=False,
    )
    redacted_requirement = redacted.requirements[0]
    assert redacted_requirement.configured is True
    assert redacted_requirement.mapping_status == "configured"
    assert redacted_requirement.credential_id is None
    assert redacted_requirement.credential_version_id is None
    assert redacted_requirement.source_env_field_name is None
    assert redacted_requirement.eligible_credentials == ()


@pytest.mark.asyncio
async def test_exact_get_keeps_current_system_skill_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context()
    target = _target(
        actor,
        status="active",
        current_version_id=None,
    )
    target.asset.scope = "system"
    target.asset.project_id = None
    target.asset.current_version_id = target.version.id
    eligible = _eligible(actor, env=["API_KEY"])

    class _Repository:
        def __init__(self, _session):
            self.lock_project = AsyncMock()

        async def lock_configurable_exact_skill_version(
            self,
            _actor,
            skill_id,
            version_id,
            *,
            read=False,
        ):
            assert read is True
            assert (skill_id, version_id) == (target.asset.id, target.version.id)
            return target

        async def get_config(self, *_args, **_kwargs):
            return None

        async def active_bindings(self, *_args, **_kwargs):
            return ()

        async def eligible_credentials(self, *_args, **_kwargs):
            return (eligible,)

    monkeypatch.setattr(
        "app.shared_assets.skill_credential_service.SkillCredentialRepository",
        _Repository,
    )

    view = await SkillCredentialBindingService(lambda: _Session()).get_exact(
        actor,
        target.asset.id,
        target.version.id,
    )

    assert view.skill_version_id == target.version.id
    assert view.requirements[0].mapping_status == "missing"
    assert view.requirements[0].eligible_credentials[0].env_fields == ("API_KEY",)


@pytest.mark.asyncio
async def test_exact_replace_writes_current_system_skill_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context()
    target = _target(
        actor,
        status="active",
        current_version_id=None,
    )
    target.asset.scope = "system"
    target.asset.project_id = None
    target.asset.current_version_id = target.version.id
    eligible = _eligible(actor, env=["PROVIDER_TOKEN"])
    config = SimpleNamespace(
        revision=1,
        skill_version_id=target.version.id,
    )
    created = SimpleNamespace(
        skill_version_id=target.version.id,
        config_revision=1,
        secret_name="API_KEY",
        source_env_field_name="PROVIDER_TOKEN",
        credential_id=eligible.credential.id,
        credential_version_id=eligible.version.id,
    )

    class _Repository:
        lock_project = AsyncMock()
        lock_configurable_exact_skill_version = AsyncMock(
            return_value=target,
        )
        get_config = AsyncMock(return_value=None)
        active_bindings = AsyncMock(side_effect=[(), (created,)])
        lock_selected_credentials = AsyncMock(
            return_value={eligible.version.id: eligible},
        )
        lock_active_envelopes = AsyncMock(
            return_value=frozenset({eligible.version.id}),
        )
        create_config = AsyncMock(return_value=config)
        replace_bindings = AsyncMock(return_value=(created,))
        eligible_credentials = AsyncMock(return_value=(eligible,))

        def __init__(self, _session):
            pass

    monkeypatch.setattr(
        "app.shared_assets.skill_credential_service.SkillCredentialRepository",
        _Repository,
    )
    governance_sink = SimpleNamespace(append_project=AsyncMock())
    service = SkillCredentialBindingService(
        lambda: _Session(),
        governance_sink=governance_sink,
    )

    view = await service.replace_for_version(
        actor,
        target.asset.id,
        target.version.id,
        (
            SkillCredentialBindingInput(
                "API_KEY",
                eligible.version.id,
                "PROVIDER_TOKEN",
            ),
        ),
        expected_revision=0,
    )

    assert view.skill_version_id == target.version.id
    assert view.revision == 1
    assert view.requirements[0].configured is True
    assert view.requirements[0].mapping_status == "configured"
    assert view.requirements[0].source_env_field_name == "PROVIDER_TOKEN"
    _Repository.lock_project.assert_awaited_once_with(actor)
    _Repository.lock_configurable_exact_skill_version.assert_awaited_once_with(
        actor,
        target.asset.id,
        target.version.id,
    )
    _Repository.create_config.assert_awaited_once_with(actor, target)
    records = _Repository.replace_bindings.await_args.args[3]
    assert records == (("API_KEY", "PROVIDER_TOKEN", eligible),)
    governance_sink.append_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_exact_replace_rejects_historical_system_skill_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context()
    target = _target(
        actor,
        status="active",
        current_version_id=uuid.uuid4(),
    )
    target.asset.scope = "system"
    target.asset.project_id = None

    class _Repository:
        lock_project = AsyncMock()
        lock_configurable_exact_skill_version = AsyncMock(
            return_value=target,
        )
        get_config = AsyncMock()
        active_bindings = AsyncMock()
        create_config = AsyncMock()
        replace_bindings = AsyncMock()
        eligible_credentials = AsyncMock()

        def __init__(self, _session):
            pass

    monkeypatch.setattr(
        "app.shared_assets.skill_credential_service.SkillCredentialRepository",
        _Repository,
    )
    governance_sink = SimpleNamespace(append_project=AsyncMock())
    service = SkillCredentialBindingService(
        lambda: _Session(),
        governance_sink=governance_sink,
    )

    with pytest.raises(AssetConflict):
        await service.replace_for_version(
            actor,
            target.asset.id,
            target.version.id,
            (),
            expected_revision=0,
        )

    _Repository.lock_project.assert_awaited_once_with(actor)
    _Repository.lock_configurable_exact_skill_version.assert_awaited_once_with(
        actor,
        target.asset.id,
        target.version.id,
    )
    _Repository.get_config.assert_not_awaited()
    _Repository.active_bindings.assert_not_awaited()
    _Repository.create_config.assert_not_awaited()
    _Repository.replace_bindings.assert_not_awaited()
    _Repository.eligible_credentials.assert_not_awaited()
    governance_sink.append_project.assert_not_awaited()
