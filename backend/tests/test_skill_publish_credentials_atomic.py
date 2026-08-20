from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets import skill_service as skill_service_module
from app.shared_assets.contexts import SystemAssetGovernanceContext
from app.shared_assets.errors import (
    AssetForbidden,
    SkillCredentialBindingsIncomplete,
    SkillCredentialSelectionStale,
)
from app.shared_assets.models import SkillArchiveFile, VersionRelation
from app.shared_assets.skill_credential_repository import EligibleSkillCredentialRecord
from app.shared_assets.skill_repository import SkillVersionRecord
from deerflow.persistence.shared_assets import SkillRow, SkillVersionFileRow, SkillVersionRow


def _actor() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="req-atomic-skill-activation",
    )


def _candidate(
    actor: ProjectContext,
    *,
    status: str,
) -> tuple[SkillRow, SkillVersionRecord]:
    skill_id = uuid.uuid4()
    content = b"""---
name: atomic-secret-skill
description: Atomic secret skill
required-secrets:
  - name: API_KEY
    optional: false
---

Use the configured secret.
"""
    files = (SkillArchiveFile("SKILL.md", content, "text/markdown"),)
    preview = skill_service_module._analyze_skill_files(  # noqa: SLF001
        files,
        actor.request_id,
    )
    version_id = uuid.uuid4()
    created_at = datetime.now(UTC)
    asset = SkillRow(
        id=skill_id,
        scope="project",
        project_id=actor.project_id,
        slug="atomic-secret-skill",
        display_name="Atomic secret skill",
        status=status,
        current_version_id=None,
        revision=8,
        created_by_user_id=str(actor.user_id),
        created_at=created_at,
        updated_at=created_at,
    )
    row = SkillVersionRow(
        id=version_id,
        skill_id=skill_id,
        version_number=1,
        description=preview.description,
        frontmatter=dict(preview.frontmatter),
        compatibility=preview.compatibility,
        secret_requirements=[{"name": requirement.name, "optional": requirement.optional} for requirement in preview.secret_requirements],
        scan_decision=preview.scan_decision,
        scan_summary=dict(preview.scan_summary),
        supersedes_version_id=None,
        payload_checksum=preview.checksum,
        created_by_user_id=str(actor.user_id),
        created_at=created_at,
    )
    file_rows = tuple(
        SkillVersionFileRow(
            skill_version_id=version_id,
            path=file.path,
            media_type=file.media_type,
            size_bytes=len(file.content),
            sha256=view.sha256,
            content=file.content,
        )
        for file, view in zip(files, preview.file_views, strict=True)
    )
    return asset, SkillVersionRecord(row, file_rows)


@dataclass
class _Store:
    asset: SkillRow
    version: SkillVersionRecord
    selected: EligibleSkillCredentialRecord
    envelope_active: bool = True
    config: object | None = None
    bindings: tuple[object, ...] = ()
    governance: list[str] | None = None
    locks: list[str] | None = None

    def __post_init__(self) -> None:
        self.governance = []
        self.locks = []


class _Transaction:
    def __init__(self, session: _Session) -> None:
        self._session = session
        self._snapshot = None

    async def __aenter__(self):
        store = self._session.store
        self._snapshot = (
            store.asset.current_version_id,
            store.asset.revision,
            copy.copy(store.config),
            tuple(copy.copy(item) for item in store.bindings),
            tuple(store.governance or ()),
        )

    async def __aexit__(self, exc_type, _exc, _traceback):
        if exc_type is None:
            self._session.commit_count += 1
            return False
        assert self._snapshot is not None
        (
            self._session.store.asset.current_version_id,
            self._session.store.asset.revision,
            self._session.store.config,
            self._session.store.bindings,
            governance,
        ) = self._snapshot
        assert self._session.store.governance is not None
        self._session.store.governance[:] = governance
        self._session.rollback_count += 1
        return False


class _Session:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.commit_count = 0
        self.rollback_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def begin(self):
        return _Transaction(self)

    async def flush(self) -> None:
        return None


class _SkillRepository:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def get_project_asset(self, actor, asset_id, *, for_update=False):
        assert for_update is True
        assert actor.project_id == self.session.store.asset.project_id
        assert asset_id == self.session.store.asset.id
        assert self.session.store.locks is not None
        self.session.store.locks.append("skill")
        return self.session.store.asset

    async def ensure_project_skill_runtime_name_available(self, actor, asset):
        assert actor.project_id == asset.project_id

    async def get_project_version(
        self,
        actor,
        asset_id,
        version_id,
        *,
        for_update=False,
    ):
        assert for_update is True
        assert actor.project_id == self.session.store.asset.project_id
        assert asset_id == self.session.store.asset.id
        assert version_id == self.session.store.version.row.id
        assert self.session.store.locks is not None
        self.session.store.locks.append("version+files")
        return self.session.store.version

    async def get_project_version_history(self, actor, asset_id):
        assert actor.project_id == self.session.store.asset.project_id
        assert asset_id == self.session.store.asset.id
        return (self.session.store.version,)

    async def get_override_asset(self, actor, asset_id, *, for_update=False):
        return await self.get_project_asset(
            actor,
            asset_id,
            for_update=for_update,
        )

    async def get_override_version(
        self,
        actor,
        asset_id,
        version_id,
        *,
        for_update=False,
    ):
        return await self.get_project_version(
            actor,
            asset_id,
            version_id,
            for_update=for_update,
        )

    async def get_override_version_history(self, actor, asset_id):
        return await self.get_project_version_history(actor, asset_id)


class _CredentialRepository:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def get_config(self, *_args, **_kwargs):
        assert self.session.store.locks is not None
        self.session.store.locks.append("config")
        return self.session.store.config

    async def active_bindings(self, *_args, **_kwargs):
        assert self.session.store.locks is not None
        self.session.store.locks.append("bindings")
        return self.session.store.bindings

    async def lock_selected_credentials(self, _actor, version_ids):
        assert self.session.store.locks is not None
        self.session.store.locks.append("credential+version")
        selected = self.session.store.selected
        return {selected.version.id: selected} if selected.version.id in version_ids else {}

    async def lock_active_envelopes(self, version_ids):
        assert self.session.store.locks is not None
        self.session.store.locks.append("envelope")
        selected_id = self.session.store.selected.version.id
        if self.session.store.envelope_active and selected_id in version_ids:
            return frozenset({selected_id})
        return frozenset()

    async def create_config(self, actor, target):
        config = SimpleNamespace(
            project_id=actor.project_id,
            skill_id=target.asset.id,
            skill_version_id=target.version.id,
            revision=1,
        )
        self.session.store.config = config
        return config

    async def replace_bindings(
        self,
        actor,
        config,
        target,
        bindings,
        *,
        now,
        existing,
        new_revision,
    ):
        del now, existing
        config.revision = new_revision
        created = tuple(
            SimpleNamespace(
                id=uuid.uuid4(),
                project_id=actor.project_id,
                skill_id=target.asset.id,
                skill_version_id=target.version.id,
                secret_name=name,
                source_env_field_name=source_env_field_name,
                credential_id=record.credential.id,
                credential_version_id=record.version.id,
                config_revision=new_revision,
            )
            for name, source_env_field_name, record in bindings
        )
        self.session.store.bindings = created
        assert self.session.store.locks is not None
        self.session.store.locks.append("binding-write")
        return created


class _GovernanceSink:
    def __init__(self, *, fail_action: str | None = None) -> None:
        self.fail_action = fail_action

    async def append_project(self, session: _Session, **kwargs) -> None:
        action = kwargs["action"]
        assert session.store.governance is not None
        session.store.governance.append(action)
        if action == self.fail_action:
            raise RuntimeError("audit failed")

    async def append_override(self, session: _Session, **kwargs) -> None:
        await self.append_project(session, **kwargs)


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str,
    envelope_active: bool = True,
    fail_action: str | None = None,
):
    actor = _actor()
    asset, version = _candidate(actor, status=status)
    credential_id = uuid.uuid4()
    credential_version_id = uuid.uuid4()
    selected = EligibleSkillCredentialRecord(
        SimpleNamespace(
            id=credential_id,
            scope="project",
            project_id=actor.project_id,
            status="active",
            is_delete=False,
            current_version_id=credential_version_id,
        ),
        SimpleNamespace(
            id=credential_version_id,
            credential_id=credential_id,
            status="active",
            payload_schema={"env": ["API_KEY"]},
        ),
    )
    store = _Store(
        asset=asset,
        version=version,
        selected=selected,
        envelope_active=envelope_active,
    )
    session = _Session(store)
    monkeypatch.setattr(skill_service_module, "SkillRepository", _SkillRepository)
    monkeypatch.setattr(
        skill_service_module,
        "SkillCredentialRepository",
        _CredentialRepository,
    )
    service = skill_service_module.SkillService(
        lambda: session,
        governance_sink=_GovernanceSink(fail_action=fail_action),
    )
    return actor, store, session, service


def _seed_persisted_binding(
    store: _Store,
    *,
    source_env_field_name: str = "API_KEY",
) -> None:
    store.config = SimpleNamespace(
        revision=1,
        skill_version_id=store.version.row.id,
    )
    store.bindings = (
        SimpleNamespace(
            id=uuid.uuid4(),
            project_id=store.asset.project_id,
            skill_id=store.asset.id,
            skill_version_id=store.version.row.id,
            config_revision=1,
            secret_name="API_KEY",
            source_env_field_name=source_env_field_name,
            credential_id=store.selected.credential.id,
            credential_version_id=store.selected.version.id,
        ),
    )


@pytest.mark.asyncio
async def test_skill_activation_validates_persisted_mapping_and_moves_current_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, store, session, service = _harness(monkeypatch, status="active")
    _seed_persisted_binding(store)

    result = await service.activate_version(
        actor,
        store.asset.id,
        store.version.row.id,
        expected_asset_version=8,
        expected_payload_checksum=store.version.row.payload_checksum,
        expected_binding_revision=1,
    )

    assert result.relation is VersionRelation.CURRENT
    assert store.asset.current_version_id == store.version.row.id
    assert store.config is not None and store.config.revision == 1
    assert store.bindings[0].secret_name == "API_KEY"
    assert store.bindings[0].source_env_field_name == "API_KEY"
    assert store.locks == [
        "skill",
        "version+files",
        "config",
        "bindings",
        "credential+version",
        "envelope",
    ]
    assert store.governance == ["skill.version.activate"]
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_skill_activation_missing_required_binding_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, store, session, service = _harness(monkeypatch, status="active")
    original_pointer = store.asset.current_version_id

    with pytest.raises(SkillCredentialBindingsIncomplete):
        await service.activate_version(
            actor,
            store.asset.id,
            store.version.row.id,
            expected_asset_version=8,
            expected_payload_checksum=store.version.row.payload_checksum,
            expected_binding_revision=0,
        )

    assert store.asset.current_version_id == original_pointer
    assert store.config is None
    assert store.bindings == ()
    assert store.governance == []
    assert session.rollback_count == 1


@pytest.mark.asyncio
async def test_admin_project_override_cannot_activate_without_required_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, store, session, service = _harness(monkeypatch, status="active")
    admin_override = SystemAssetGovernanceContext(
        user_id=actor.user_id,
        project_id=actor.project_id,
        request_id=actor.request_id,
    )
    original_pointer = store.asset.current_version_id

    with pytest.raises(SkillCredentialBindingsIncomplete):
        await service.activate_version(
            admin_override,
            store.asset.id,
            store.version.row.id,
            expected_asset_version=8,
            expected_payload_checksum=store.version.row.payload_checksum,
            expected_binding_revision=0,
        )

    assert store.asset.current_version_id == original_pointer
    assert store.config is None
    assert store.bindings == ()
    assert store.governance == []
    assert store.locks == ["skill", "version+files", "config", "bindings"]
    assert session.rollback_count == 1


@pytest.mark.asyncio
async def test_admin_project_override_activates_with_valid_existing_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, store, session, service = _harness(monkeypatch, status="active")
    admin_override = SystemAssetGovernanceContext(
        user_id=actor.user_id,
        project_id=actor.project_id,
        request_id=actor.request_id,
    )
    store.config = SimpleNamespace(revision=1)
    store.bindings = (
        SimpleNamespace(
            skill_version_id=store.version.row.id,
            config_revision=1,
            secret_name="API_KEY",
            source_env_field_name="API_KEY",
            credential_id=store.selected.credential.id,
            credential_version_id=store.selected.version.id,
        ),
    )

    result = await service.activate_version(
        admin_override,
        store.asset.id,
        store.version.row.id,
        expected_asset_version=8,
        expected_payload_checksum=store.version.row.payload_checksum,
        expected_binding_revision=1,
    )

    assert result.relation is VersionRelation.CURRENT
    assert store.asset.current_version_id == store.version.row.id
    assert store.config.revision == 1
    assert store.locks == [
        "skill",
        "version+files",
        "config",
        "bindings",
        "credential+version",
        "envelope",
    ]
    assert store.governance == ["skill.version.activate"]
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_global_system_governance_cannot_manually_activate_system_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, store, session, service = _harness(monkeypatch, status="active")
    system_admin = SystemAssetGovernanceContext(
        user_id=actor.user_id,
        project_id=None,
        request_id=actor.request_id,
    )

    with pytest.raises(AssetForbidden):
        await service.activate_version(
            system_admin,
            store.asset.id,
            store.version.row.id,
            expected_asset_version=8,
            expected_payload_checksum=store.version.row.payload_checksum,
            expected_binding_revision=0,
        )

    assert session.commit_count == 0
    assert session.rollback_count == 0
    assert store.locks == []


@pytest.mark.asyncio
async def test_suspended_skill_activation_rejects_incomplete_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, store, session, service = _harness(monkeypatch, status="suspended")

    with pytest.raises(SkillCredentialBindingsIncomplete):
        await service.activate_version(
            actor,
            store.asset.id,
            store.version.row.id,
            expected_asset_version=8,
            expected_payload_checksum=store.version.row.payload_checksum,
            expected_binding_revision=0,
        )

    assert store.config is None
    assert store.governance == []
    assert session.rollback_count == 1


@pytest.mark.asyncio
async def test_activation_requires_edit_but_not_credential_approve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, store, session, service = _harness(monkeypatch, status="suspended")
    editor = ProjectContext(
        user_id=actor.user_id,
        project_id=actor.project_id,
        membership_id=actor.membership_id,
        role=actor.role,
        capabilities=frozenset({Capability.SHARED_ASSETS_EDIT}),
        membership_version=actor.membership_version,
        request_id=actor.request_id,
    )
    _seed_persisted_binding(store)

    result = await service.activate_version(
        editor,
        store.asset.id,
        store.version.row.id,
        expected_asset_version=8,
        expected_payload_checksum=store.version.row.payload_checksum,
        expected_binding_revision=1,
    )

    assert result.relation is VersionRelation.CURRENT
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_activation_stale_envelope_and_audit_failure_roll_back_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor, store, session, service = _harness(
        monkeypatch,
        status="active",
        envelope_active=False,
    )
    original_pointer = store.asset.current_version_id
    _seed_persisted_binding(store)

    with pytest.raises(SkillCredentialSelectionStale):
        await service.activate_version(
            actor,
            store.asset.id,
            store.version.row.id,
            expected_asset_version=8,
            expected_payload_checksum=store.version.row.payload_checksum,
            expected_binding_revision=1,
        )

    assert store.asset.current_version_id == original_pointer
    assert store.config is not None
    assert len(store.bindings) == 1

    actor2, store2, session2, service2 = _harness(
        monkeypatch,
        status="active",
        fail_action="skill.version.activate",
    )
    pointer2 = store2.asset.current_version_id
    _seed_persisted_binding(store2)
    with pytest.raises(RuntimeError, match="audit failed"):
        await service2.activate_version(
            actor2,
            store2.asset.id,
            store2.version.row.id,
            expected_asset_version=8,
            expected_payload_checksum=store2.version.row.payload_checksum,
            expected_binding_revision=1,
        )

    assert store2.asset.current_version_id == pointer2
    assert store2.config is not None
    assert len(store2.bindings) == 1
    assert store2.governance == []
    assert session2.rollback_count == 1
