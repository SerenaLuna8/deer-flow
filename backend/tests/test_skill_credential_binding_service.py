from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import (
    AssetForbidden,
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
    current_published_version_id: uuid.UUID | None,
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
            version=8,
            current_published_version_id=current_published_version_id,
        ),
        SimpleNamespace(
            id=selected_version_id,
            skill_id=skill_id,
            workflow_status="draft",
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
async def test_get_for_version_builds_exact_draft_publish_plan_with_explicit_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _context()
    previous_version_id = uuid.uuid4()
    target = _target(
        actor,
        status="active",
        current_published_version_id=previous_version_id,
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

    plan = await service.get_for_version(actor, target.asset.id, target.version.id)

    assert plan.skill_id == target.asset.id
    assert plan.skill_version_id == target.version.id
    assert plan.asset_version == 8
    assert plan.payload_checksum == "a" * 64
    assert plan.binding_revision == 0
    assert plan.secrets_autonomous is True
    assert plan.requirements[0].suggested_credential_version_id == eligible.version.id
    assert plan.requirements[0].eligible_credentials[0].credential_version_id == eligible.version.id
    assert plan.requirements[1].suggested_credential_version_id is None


@pytest.mark.asyncio
async def test_editor_cannot_open_exact_version_publish_plan() -> None:
    actor = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="req-editor-publish-plan",
    )

    def unexpected_session():
        raise AssertionError("permission denial must happen before storage access")

    with pytest.raises(AssetForbidden):
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
    published_version_id = uuid.uuid4()
    target = _target(
        actor,
        status="active",
        current_published_version_id=published_version_id,
        version_id=published_version_id,
    )
    target.version.workflow_status = "published"
    config = SimpleNamespace(revision=2)
    existing = (
        SimpleNamespace(
            skill_version_id=published_version_id,
            config_revision=2,
            secret_name="API_KEY",
        ),
    )

    class _Repository:
        replace_bindings = AsyncMock()
        create_config = AsyncMock()

        def __init__(self, _session):
            self.lock_project = AsyncMock()

        async def lock_configurable_current_published_skill(self, *_args, **_kwargs):
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
            expected_revision=2,
        )

    _Repository.replace_bindings.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_credential_disappearance_collapses_to_stable_stale_error() -> None:
    actor = _context()
    target = _target(
        actor,
        status="active",
        current_published_version_id=uuid.uuid4(),
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
