from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import OperationalError

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.quotas.models import QuotaExceeded
from app.shared_assets import skill_service as skill_service_module
from app.shared_assets.errors import (
    AssetConflict,
    AssetNotFound,
    AssetStorageQuotaExceeded,
    AssetStorageUnavailable,
    SkillCredentialBindingsIncomplete,
    SkillPublishBaseStale,
    SkillRuntimeNameConflict,
)
from app.shared_assets.models import SkillArchiveFile, WorkflowStatus
from app.shared_assets.skill_credential_closure import SkillCredentialClosureInvalid
from app.shared_assets.skill_repository import SkillVersionRecord
from deerflow.persistence.shared_assets import SkillRow, SkillVersionFileRow, SkillVersionRow


def _admin_context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=1,
        request_id="req-skill-lifecycle",
    )


def _files(name: str, *, body: str = "Use reviewed inputs.\n") -> tuple[SkillArchiveFile, ...]:
    content = f"---\nname: {name}\ndescription: Safe demo skill\n---\n\n{body}".encode()
    return (SkillArchiveFile("SKILL.md", content, "text/markdown"),)


def _files_with_required_secret(
    name: str,
    *,
    body: str = "Use the mapped credential.\n",
) -> tuple[SkillArchiveFile, ...]:
    content = (f"---\nname: {name}\ndescription: Credential demo skill\nrequired-secrets:\n  - name: TARGET_API_KEY\n    optional: false\n---\n\n{body}").encode()
    return (SkillArchiveFile("SKILL.md", content, "text/markdown"),)


def _seed_asset(store: _Store, actor: ProjectContext, *, slug: str) -> SkillRow:
    now = datetime.now(UTC)
    asset = SkillRow(
        id=uuid.uuid4(),
        scope="project",
        project_id=actor.project_id,
        slug=slug,
        display_name=slug.replace("-", " ").title(),
        status="suspended",
        current_published_version_id=None,
        version=1,
        created_by_user_id=str(actor.user_id),
        created_at=now,
        updated_at=now,
    )
    store.assets[asset.id] = asset
    return asset


@dataclass(frozen=True)
class _Reservation:
    project_id: uuid.UUID
    version_id: uuid.UUID
    size: int


class _Store:
    def __init__(self) -> None:
        self.assets: dict[uuid.UUID, SkillRow] = {}
        self.versions: dict[uuid.UUID, SkillVersionRecord] = {}
        self.reservations: list[_Reservation] = []
        self.reservation_attempts: list[_Reservation] = []
        self.governance: list[dict[str, object]] = []
        self.persist_attempts = 0
        self.fail_create_version = False
        self.system_skill_name_conflict = False


@dataclass(frozen=True)
class _StoreSnapshot:
    assets: dict[uuid.UUID, SkillRow]
    asset_state: dict[uuid.UUID, tuple[str, uuid.UUID | None, int]]
    versions: dict[uuid.UUID, SkillVersionRecord]
    version_status: dict[uuid.UUID, str]
    reservations: tuple[_Reservation, ...]
    governance: tuple[dict[str, object], ...]


def _snapshot(store: _Store) -> _StoreSnapshot:
    return _StoreSnapshot(
        assets=dict(store.assets),
        asset_state={
            asset_id: (
                asset.status,
                asset.current_published_version_id,
                asset.version,
            )
            for asset_id, asset in store.assets.items()
        },
        versions=dict(store.versions),
        version_status={version_id: record.row.workflow_status for version_id, record in store.versions.items()},
        reservations=tuple(store.reservations),
        governance=tuple(store.governance),
    )


def _restore(store: _Store, snapshot: _StoreSnapshot) -> None:
    store.assets.clear()
    store.assets.update(snapshot.assets)
    for asset_id, (status, current_version_id, version) in snapshot.asset_state.items():
        asset = store.assets[asset_id]
        asset.status = status
        asset.current_published_version_id = current_version_id
        asset.version = version

    store.versions.clear()
    store.versions.update(snapshot.versions)
    for version_id, workflow_status in snapshot.version_status.items():
        store.versions[version_id].row.workflow_status = workflow_status

    store.reservations[:] = snapshot.reservations
    store.governance[:] = snapshot.governance


class _Transaction:
    def __init__(self, session: _Session) -> None:
        self._session = session
        self._snapshot: _StoreSnapshot | None = None

    async def __aenter__(self) -> None:
        self._snapshot = _snapshot(self._session.store)

    async def __aexit__(self, exc_type, _exc, _traceback) -> None:
        assert self._snapshot is not None
        if exc_type is None:
            self._session.commit_count += 1
            return
        _restore(self._session.store, self._snapshot)
        self._session.rollback_count += 1


class _Session:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction(self)

    async def flush(self) -> None:
        self.flush_count += 1


class _Repository:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.store = session.store

    async def create_project_asset(
        self,
        context: ProjectContext,
        command: skill_service_module.CreateSkill,
    ) -> SkillRow:
        now = datetime.now(UTC)
        row = SkillRow(
            id=uuid.uuid4(),
            scope="project",
            project_id=context.project_id,
            slug=command.slug,
            display_name=command.display_name,
            status="suspended",
            current_published_version_id=None,
            version=1,
            created_by_user_id=str(context.user_id),
            created_at=now,
            updated_at=now,
        )
        self.store.assets[row.id] = row
        await self.session.flush()
        return row

    async def list_project_visible(
        self,
        context: ProjectContext,
    ) -> tuple[SkillRow, ...]:
        return tuple(row for row in self.store.assets.values() if row.project_id == context.project_id)

    async def get_project_asset(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> SkillRow:
        del for_update
        row = self.store.assets.get(asset_id)
        if row is None or row.project_id != context.project_id:
            raise AssetNotFound(context.request_id)
        return row

    async def next_project_version_number(
        self,
        context: ProjectContext,
        asset: SkillRow,
    ) -> int:
        if asset.project_id != context.project_id:
            raise AssetNotFound(context.request_id)
        return 1 + max(
            (record.row.version_number for record in self.store.versions.values() if record.row.skill_id == asset.id),
            default=0,
        )

    async def ensure_project_skill_runtime_name_available(
        self,
        context: ProjectContext,
        asset: SkillRow,
    ) -> None:
        if asset.project_id != context.project_id:
            raise AssetNotFound(context.request_id)
        if self.store.system_skill_name_conflict:
            raise SkillRuntimeNameConflict(context.request_id)

    async def create_project_version(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        row: SkillVersionRow,
        files: tuple[SkillVersionFileRow, ...],
    ) -> SkillVersionRecord:
        asset = self.store.assets.get(asset_id)
        if asset is None or asset.project_id != context.project_id:
            raise AssetNotFound(context.request_id)
        self.store.persist_attempts += 1
        if self.store.fail_create_version:
            raise OperationalError(
                "INSERT INTO skill_versions",
                {},
                RuntimeError("database unavailable"),
            )
        if row.created_at is None:
            row.created_at = datetime.now(UTC)
        record = SkillVersionRecord(row=row, files=tuple(files))
        self.store.versions[row.id] = record
        return record

    async def get_project_version(
        self,
        context: ProjectContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> SkillVersionRecord:
        del for_update
        record = self.store.versions.get(version_id)
        if record is None or record.row.skill_id != asset_id:
            raise AssetNotFound(context.request_id)
        asset = self.store.assets.get(asset_id)
        if asset is None or asset.project_id != context.project_id:
            raise AssetNotFound(context.request_id)
        return record

    get_override_asset = get_project_asset
    next_override_version_number = next_project_version_number
    create_override_version = create_project_version


class _Quota:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure

    async def reserve_skill_version(
        self,
        session: _Session,
        project_id: uuid.UUID,
        *,
        version_id: uuid.UUID,
        size: int,
    ) -> None:
        reservation = _Reservation(project_id, version_id, size)
        session.store.reservation_attempts.append(reservation)
        if self.failure is not None:
            raise self.failure
        session.store.reservations.append(reservation)


class _SkillCredentialRepository:
    def __init__(self, _session: _Session) -> None:
        pass

    async def get_config(self, *_args: object, **_kwargs: object):
        return None

    async def active_bindings(self, *_args: object, **_kwargs: object):
        return ()

    async def lock_selected_credentials(self, *_args: object, **_kwargs: object):
        return {}

    async def lock_active_envelopes(self, *_args: object, **_kwargs: object):
        return frozenset()


class _GovernanceSink:
    async def append_project(self, session: _Session, **kwargs: object) -> None:
        session.store.governance.append(dict(kwargs))

    append_override = append_project


@dataclass(frozen=True)
class _Harness:
    store: _Store
    session: _Session
    quota: _Quota
    service: skill_service_module.SkillService


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    store = _Store()
    session = _Session(store)
    quota = _Quota()
    monkeypatch.setattr(skill_service_module, "SkillRepository", _Repository)
    monkeypatch.setattr(
        skill_service_module,
        "SkillCredentialRepository",
        _SkillCredentialRepository,
    )
    service = skill_service_module.SkillService(
        lambda: session,
        governance_sink=_GovernanceSink(),
        quota=quota,
    )
    return _Harness(store, session, quota, service)


def _persisted_payload(record: SkillVersionRecord) -> tuple[object, ...]:
    row = record.row
    return (
        row.id,
        row.skill_id,
        row.version_number,
        row.description,
        dict(row.frontmatter),
        row.compatibility,
        tuple(dict(item) for item in row.secret_requirements),
        row.scan_decision,
        dict(row.scan_summary),
        row.supersedes_version_id,
        row.payload_checksum,
        row.created_by_user_id,
        tuple(
            (
                file.path,
                file.media_type,
                file.size_bytes,
                file.sha256,
                bytes(file.content),
            )
            for file in record.files
        ),
    )


async def _create_draft(
    harness: _Harness,
    actor: ProjectContext,
    slug: str,
) -> tuple[SkillRow, SkillVersionRecord]:
    asset = _seed_asset(harness.store, actor, slug=slug)
    version = await harness.service.create_version_from_archive(
        actor,
        asset.id,
        _files(slug),
        expected_asset_version=1,
    )
    return asset, harness.store.versions[version.id]


@pytest.mark.asyncio
async def test_initial_archive_create_allows_required_secret_without_binding(
    harness: _Harness,
) -> None:
    actor = _admin_context()

    result = await harness.service.import_project_archives_atomic(
        actor,
        (
            skill_service_module.ProjectSkillArchiveImport(
                files=_files_with_required_secret("archive-required-skill"),
            ),
        ),
        execute=True,
    )

    assert result.created_count == 1
    created = next(iter(harness.store.assets.values()))
    version = harness.store.versions[created.current_published_version_id]
    assert created.status == "suspended"
    assert created.version == 3
    assert version.row.workflow_status == WorkflowStatus.PUBLISHED.value
    assert version.row.secret_requirements == [
        {"name": "TARGET_API_KEY", "optional": False},
    ]


@pytest.mark.asyncio
async def test_builder_preview_create_allows_required_secret_without_binding(
    harness: _Harness,
) -> None:
    actor = _admin_context()
    preview = await harness.service.preview_archive(
        actor,
        _files_with_required_secret("builder-required-skill"),
    )

    result = await harness.service.create_project_from_preview_in_session(
        harness.session,
        actor,
        skill_service_module.CreateSkill(
            slug="builder-required-skill",
            display_name="Builder Required Skill",
        ),
        preview,
    )

    created = harness.store.assets[result.asset.id]
    assert created.status == "suspended"
    assert created.current_published_version_id == result.version.id
    assert result.version.workflow_status is WorkflowStatus.PUBLISHED
    assert result.version.secret_requirements == (
        skill_service_module.SkillSecretRequirementView(
            name="TARGET_API_KEY",
            optional=False,
        ),
    )


@pytest.mark.asyncio
async def test_archive_replace_does_not_reuse_incomplete_create_exception(
    harness: _Harness,
) -> None:
    actor = _admin_context()
    original = _files_with_required_secret("archive-replace-required")
    await harness.service.import_project_archives_atomic(
        actor,
        (skill_service_module.ProjectSkillArchiveImport(files=original),),
        execute=True,
    )
    created = next(iter(harness.store.assets.values()))
    original_version_id = created.current_published_version_id

    with pytest.raises(SkillCredentialBindingsIncomplete):
        await harness.service.import_project_archives_atomic(
            actor,
            (
                skill_service_module.ProjectSkillArchiveImport(
                    files=_files_with_required_secret(
                        "archive-replace-required",
                        body="Use the mapped credential in revision two.\n",
                    ),
                ),
            ),
            execute=True,
            replace=True,
        )

    assert created.current_published_version_id == original_version_id
    assert created.version == 3
    assert tuple(harness.store.versions) == (original_version_id,)


@pytest.mark.asyncio
async def test_publish_preserves_version_payload_history_and_moves_current_pointer(
    harness: _Harness,
) -> None:
    actor = _admin_context()
    asset, first_draft = await _create_draft(
        harness,
        actor,
        "history-skill",
    )
    first = await harness.service.publish(
        actor,
        asset.id,
        first_draft.row.id,
        expected_asset_version=2,
        expected_payload_checksum=first_draft.row.payload_checksum,
        expected_binding_revision=0,
    )
    assert first.workflow_status is WorkflowStatus.PUBLISHED

    second = await harness.service.create_version_from_archive(
        actor,
        asset.id,
        _files("history-skill", body="Use reviewed inputs for revision two.\n"),
        expected_asset_version=3,
    )
    first_record = harness.store.versions[first.id]
    second_record = harness.store.versions[second.id]
    first_payload_before = _persisted_payload(first_record)
    second_payload_before = _persisted_payload(second_record)

    published = await harness.service.publish(
        actor,
        asset.id,
        second.id,
        expected_asset_version=4,
        expected_payload_checksum=second_record.row.payload_checksum,
        expected_binding_revision=0,
    )

    persisted_asset = harness.store.assets[asset.id]
    assert published.id == second.id
    assert published.workflow_status is WorkflowStatus.PUBLISHED
    assert persisted_asset.current_published_version_id == second.id
    assert persisted_asset.version == 5
    assert first_record.row.workflow_status == WorkflowStatus.PUBLISHED.value
    assert _persisted_payload(first_record) == first_payload_before
    assert _persisted_payload(second_record) == second_payload_before


@pytest.mark.asyncio
async def test_suspended_skill_can_publish_activate_and_suspend(
    harness: _Harness,
) -> None:
    actor = _admin_context()
    created, draft = await _create_draft(
        harness,
        actor,
        "toggle-skill",
    )

    published = await harness.service.publish(
        actor,
        created.id,
        draft.row.id,
        expected_asset_version=2,
        expected_payload_checksum=draft.row.payload_checksum,
        expected_binding_revision=0,
    )
    activated = await harness.service.activate(
        actor,
        created.id,
        expected_asset_version=3,
    )
    suspended = await harness.service.suspend(
        actor,
        created.id,
        expected_asset_version=4,
    )

    assert published.workflow_status is WorkflowStatus.PUBLISHED
    assert activated.status == "active"
    assert activated.version == 4
    assert suspended.status == "suspended"
    assert suspended.version == 5
    assert [event["action"] for event in harness.store.governance] == [
        "skill.version.create",
        "skill.publish",
        "skill.activate",
        "skill.suspend",
    ]


@pytest.mark.asyncio
async def test_activation_reports_incomplete_required_credential_bindings(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _admin_context()
    created, draft = await _create_draft(
        harness,
        actor,
        "credential-gated-skill",
    )
    await harness.service.publish(
        actor,
        created.id,
        draft.row.id,
        expected_asset_version=2,
        expected_payload_checksum=draft.row.payload_checksum,
        expected_binding_revision=0,
    )
    draft.row.secret_requirements = [{"name": "API_KEY", "optional": False}]

    async def reject_incomplete(*_args: object, **_kwargs: object) -> None:
        raise SkillCredentialClosureInvalid("incomplete")

    monkeypatch.setattr(
        skill_service_module,
        "lock_skill_credential_closure",
        reject_incomplete,
    )

    with pytest.raises(SkillCredentialBindingsIncomplete):
        await harness.service.activate(
            actor,
            created.id,
            expected_asset_version=3,
        )

    assert harness.store.assets[created.id].status == "suspended"
    assert harness.store.assets[created.id].version == 3


@pytest.mark.asyncio
async def test_project_skill_activation_rejects_enabled_system_name_conflict(
    harness: _Harness,
) -> None:
    actor = _admin_context()
    created, draft = await _create_draft(
        harness,
        actor,
        "conflicting-skill",
    )
    await harness.service.publish(
        actor,
        created.id,
        draft.row.id,
        expected_asset_version=2,
        expected_payload_checksum=draft.row.payload_checksum,
        expected_binding_revision=0,
    )
    harness.store.system_skill_name_conflict = True

    with pytest.raises(SkillRuntimeNameConflict):
        await harness.service.activate(
            actor,
            created.id,
            expected_asset_version=3,
        )

    assert harness.store.assets[created.id].status == "suspended"
    assert harness.store.assets[created.id].version == 3
    assert harness.session.rollback_count == 1


@pytest.mark.asyncio
async def test_stale_publish_expected_version_has_no_side_effects(
    harness: _Harness,
) -> None:
    actor = _admin_context()
    created, draft = await _create_draft(
        harness,
        actor,
        "stale-skill",
    )
    reservations_before = tuple(harness.store.reservations)
    reservation_attempts_before = tuple(harness.store.reservation_attempts)
    governance_before = tuple(harness.store.governance)
    commits_before = harness.session.commit_count
    rollbacks_before = harness.session.rollback_count

    with pytest.raises(AssetConflict) as exc_info:
        await harness.service.publish(
            actor,
            created.id,
            draft.row.id,
            expected_asset_version=1,
            expected_payload_checksum=draft.row.payload_checksum,
            expected_binding_revision=0,
        )

    assert exc_info.value.request_id == actor.request_id
    persisted_asset = harness.store.assets[created.id]
    assert persisted_asset.version == 2
    assert persisted_asset.current_published_version_id is None
    assert draft.row.workflow_status == WorkflowStatus.DRAFT.value
    assert tuple(harness.store.reservations) == reservations_before
    assert tuple(harness.store.reservation_attempts) == reservation_attempts_before
    assert tuple(harness.store.governance) == governance_before
    assert harness.session.commit_count == commits_before
    assert harness.session.rollback_count == rollbacks_before + 1


@pytest.mark.asyncio
async def test_quota_failure_stops_version_persistence_without_side_effects(
    harness: _Harness,
) -> None:
    actor = _admin_context()
    asset = _seed_asset(harness.store, actor, slug="quota-skill")
    harness.quota.failure = QuotaExceeded("storage_bytes", 1)

    with pytest.raises(AssetStorageQuotaExceeded) as exc_info:
        await harness.service.create_version_from_archive(
            actor,
            asset.id,
            _files(asset.slug),
            expected_asset_version=1,
        )

    assert exc_info.value.request_id == actor.request_id
    assert asset.version == 1
    assert harness.store.versions == {}
    assert harness.store.persist_attempts == 0
    assert len(harness.store.reservation_attempts) == 1
    assert harness.store.reservations == []
    assert harness.store.governance == []
    assert harness.session.commit_count == 0
    assert harness.session.rollback_count == 1


@pytest.mark.asyncio
async def test_version_persistence_failure_rolls_back_quota_and_governance(
    harness: _Harness,
) -> None:
    actor = _admin_context()
    asset = _seed_asset(harness.store, actor, slug="atomic-skill")
    harness.store.fail_create_version = True

    with pytest.raises(AssetStorageUnavailable) as exc_info:
        await harness.service.create_version_from_archive(
            actor,
            asset.id,
            _files(asset.slug),
            expected_asset_version=1,
        )

    assert exc_info.value.request_id == actor.request_id
    assert harness.store.persist_attempts == 1
    assert len(harness.store.reservation_attempts) == 1
    assert harness.store.assets == {asset.id: asset}
    assert asset.version == 1
    assert harness.store.versions == {}
    assert harness.store.reservations == []
    assert harness.store.governance == []
    assert harness.session.commit_count == 0
    assert harness.session.rollback_count == 1


@pytest.mark.asyncio
async def test_publish_lineage_guard_requires_ack_when_live_pointer_moved(
    harness: _Harness,
) -> None:
    actor = _admin_context()
    asset, first_draft = await _create_draft(
        harness,
        actor,
        "lineage-skill",
    )
    first = await harness.service.publish(
        actor,
        asset.id,
        first_draft.row.id,
        expected_asset_version=2,
        expected_payload_checksum=first_draft.row.payload_checksum,
        expected_binding_revision=0,
    )
    stale = await harness.service.create_version_from_archive(
        actor,
        asset.id,
        _files("lineage-skill", body="Stale fork from the first published version.\n"),
        expected_asset_version=3,
    )
    live = await harness.service.create_version_from_archive(
        actor,
        asset.id,
        _files("lineage-skill", body="Newer live successor of the first version.\n"),
        expected_asset_version=4,
    )
    published_live = await harness.service.publish(
        actor,
        asset.id,
        live.id,
        expected_asset_version=5,
        expected_payload_checksum=harness.store.versions[live.id].row.payload_checksum,
        expected_binding_revision=0,
    )

    with pytest.raises(SkillPublishBaseStale) as exc_info:
        await harness.service.publish(
            actor,
            asset.id,
            stale.id,
            expected_asset_version=6,
            expected_payload_checksum=harness.store.versions[stale.id].row.payload_checksum,
            expected_binding_revision=0,
        )

    persisted = harness.store.assets[asset.id]
    assert exc_info.value.request_id == actor.request_id
    assert persisted.current_published_version_id == published_live.id
    assert harness.store.versions[stale.id].row.workflow_status == WorkflowStatus.DRAFT.value

    published_stale = await harness.service.publish(
        actor,
        asset.id,
        stale.id,
        expected_asset_version=6,
        acknowledge_stale_base=True,
        expected_payload_checksum=harness.store.versions[stale.id].row.payload_checksum,
        expected_binding_revision=0,
    )
    assert published_stale.id == stale.id
    assert published_stale.workflow_status is WorkflowStatus.PUBLISHED
    assert harness.store.assets[asset.id].current_published_version_id == stale.id
    assert first.workflow_status is WorkflowStatus.PUBLISHED


@pytest.mark.asyncio
async def test_create_project_version_from_preview_pins_supersedes_without_publishing(
    harness: _Harness,
) -> None:
    actor = _admin_context()
    asset, first_draft = await _create_draft(
        harness,
        actor,
        "revise-skill",
    )
    published = await harness.service.publish(
        actor,
        asset.id,
        first_draft.row.id,
        expected_asset_version=2,
        expected_payload_checksum=first_draft.row.payload_checksum,
        expected_binding_revision=0,
    )
    preview = await harness.service.preview_archive(
        actor,
        _files("revise-skill", body="Revision draft from the published base.\n"),
    )

    created = await harness.service.create_project_version_from_preview_in_session(
        harness.session,
        actor,
        asset.id,
        preview,
        supersedes_version_id=published.id,
    )

    persisted = harness.store.assets[asset.id]
    record = harness.store.versions[created.id]
    assert created.workflow_status is WorkflowStatus.DRAFT
    assert created.supersedes_version_id == published.id
    assert record.row.supersedes_version_id == published.id
    assert persisted.current_published_version_id == published.id
    assert persisted.status == "suspended"
