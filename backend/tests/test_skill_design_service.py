from __future__ import annotations

import dataclasses
import importlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import (
    AssetConflict,
    AssetNotFound,
    AssetStorageQuotaExceeded,
    AssetValidationFailed,
)
from app.shared_assets.models import AssetScope, SkillArchiveFile, WorkflowStatus
from app.shared_assets.skill_design_generation import (
    MAX_SKILL_DESIGN_BRIEF_CHARS,
    CandidateResult,
    ClarificationQuestion,
    NeedsClarificationResult,
    SkillDesignGeneratedFile,
)
from app.shared_assets.skill_design_repository import PinnedSkillCreator
from app.shared_assets.skill_repository import SkillRepository
from app.shared_assets.skill_service import (
    ProjectSkillArchiveCreateResult,
    SkillAssetView,
    SkillService,
    SkillVersionView,
)


def _editor_context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="req-skill-design",
    )


def test_skill_design_persistence_models_are_registered_and_pin_skill_creator() -> None:
    import deerflow.persistence.models  # noqa: F401
    from deerflow.persistence.base import Base

    assert {
        "skill_design_sessions",
        "skill_design_operations",
        "skill_design_draft_files",
    } <= set(Base.metadata.tables)
    session = Base.metadata.tables["skill_design_sessions"]
    assert {
        "project_id",
        "owner_user_id",
        "slug",
        "status",
        "revision",
        "messages_json",
        "draft_checksum",
        "validation_json",
        "validated_draft_checksum",
        "skill_creator_skill_id",
        "skill_creator_version_id",
        "skill_creator_payload_checksum",
        "created_skill_id",
        "created_skill_version_id",
    } <= set(session.columns.keys())
    assert {constraint.name for constraint in session.constraints if constraint.name is not None} >= {
        "fk_skill_design_sessions_membership",
        "fk_skill_design_sessions_skill_creator_version",
        "fk_skill_design_sessions_created_skill_project",
        "fk_skill_design_sessions_created_skill_version",
        "uq_skill_design_sessions_private_scope",
        "uq_skill_design_sessions_create_idempotency",
    }
    operation = Base.metadata.tables["skill_design_operations"]
    assert {constraint.name for constraint in operation.constraints if constraint.name is not None} >= {
        "fk_skill_design_operations_session",
        "uq_skill_design_operations_idempotency",
    }
    draft = Base.metadata.tables["skill_design_draft_files"]
    assert {constraint.name for constraint in draft.constraints if constraint.name is not None} >= {
        "fk_skill_design_draft_files_session",
        "ck_skill_design_draft_files_safe_path",
        "ck_skill_design_draft_files_content_size",
    }


def test_skill_design_exposes_frozen_revision_checksum_contracts() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")

    assert module.SkillDesignStatus.INTERVIEWING == "interviewing"
    assert module.SkillDesignStatus.DRAFT_READY == "draft_ready"
    assert module.SkillDesignStatus.VALIDATED == "validated"
    assert module.SkillDesignStatus.COMPLETED == "completed"
    for contract in (
        module.CreateSkillDesignSession,
        module.SkillDesignDraftUpdateTurn,
        module.ValidateSkillDesignSession,
        module.CommitSkillDesignSession,
        module.SkillDesignSessionView,
    ):
        assert dataclasses.is_dataclass(contract)
        assert contract.__dataclass_params__.frozen is True


@pytest.mark.asyncio
async def test_invalid_skill_design_create_is_rejected_before_storage_or_model_call() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")

    class ExplodingSessionFactory:
        def __call__(self):
            raise AssertionError("invalid input must not open persistence")

    service = module.SkillDesignService(ExplodingSessionFactory())
    with pytest.raises(AssetValidationFailed) as captured:
        await service.create(
            _editor_context(),
            module.CreateSkillDesignSession(
                slug="Not Valid",
                display_name="Skill",
                idempotency_key="idem-1",
            ),
        )
    assert captured.value.request_id == "req-skill-design"


@pytest.mark.asyncio
async def test_secret_like_display_name_is_rejected_before_storage() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")

    class ExplodingSessionFactory:
        def __call__(self):
            raise AssertionError("unsafe display name must not open persistence")

    service = module.SkillDesignService(ExplodingSessionFactory())
    with pytest.raises(AssetValidationFailed):
        await service.create(
            _editor_context(),
            module.CreateSkillDesignSession(
                slug="safe-slug",
                display_name="api_key=supersecret123",
                idempotency_key="unsafe-display",
            ),
        )


def test_skill_design_generation_transcript_keeps_prior_requirements_and_clarification() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    context = _editor_context()
    transcript = module.SkillDesignService._conversation_brief(
        context,
        [
            {
                "role": "assistant",
                "content": "请描述这个 Skill 要解决的问题。",
            },
            {
                "role": "user",
                "content": "从合并的拉取请求生成发布说明。",
            },
            {
                "role": "assistant",
                "content": "拉取请求来自哪个系统？",
            },
            {
                "role": "user",
                "content": "来自 GitHub，并按标签分组。",
            },
        ],
    )

    assert "user: 从合并的拉取请求生成发布说明。" in transcript
    assert "assistant: 拉取请求来自哪个系统？" in transcript
    assert transcript.endswith("user: 来自 GitHub，并按标签分组。")
    assert len(transcript) <= MAX_SKILL_DESIGN_BRIEF_CHARS


def test_skill_design_generation_transcript_is_bounded_and_prioritizes_latest_turn() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    latest = "LATEST-ANSWER-" + ("z" * 200)
    transcript = module.SkillDesignService._conversation_brief(
        _editor_context(),
        [
            {"role": "user", "content": "old-" + ("x" * 20_000)},
            {"role": "assistant", "content": "question-" + ("y" * 4_000)},
            {"role": "user", "content": latest},
        ],
    )

    assert len(transcript) <= MAX_SKILL_DESIGN_BRIEF_CHARS
    assert transcript.endswith(f"user: {latest}")


def test_skill_design_message_history_is_bounded_before_append() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    row = SimpleNamespace(messages_json=[{"role": "user", "content": f"message-{index}"} for index in range(128)])

    with pytest.raises(AssetValidationFailed):
        module.SkillDesignService._append_row_message(
            _editor_context(),
            row,
            "assistant",
            "overflow",
        )
    assert len(row.messages_json) == 128


def test_skill_design_progress_tracks_generation_validation_and_failure() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")

    def statuses(status):
        return [item["status"] for item in module.SkillDesignService._progress_json(status)]

    assert statuses(module.SkillDesignStatus.GENERATING) == [
        "completed",
        "running",
        "pending",
    ]
    assert statuses(module.SkillDesignStatus.DRAFT_READY) == [
        "completed",
        "completed",
        "pending",
    ]
    assert statuses(module.SkillDesignStatus.VALIDATED) == [
        "completed",
        "completed",
        "completed",
    ]
    assert statuses(module.SkillDesignStatus.COMPLETED) == [
        "completed",
        "completed",
        "completed",
    ]
    assert statuses(module.SkillDesignStatus.FAILED) == [
        "completed",
        "failed",
        "pending",
    ]


class _FakeSkillDatabase:
    def __init__(self) -> None:
        self.sessions: dict[uuid.UUID, object] = {}
        self.operations: dict[tuple[str, str], object] = {}
        self.files: dict[uuid.UUID, tuple[SkillArchiveFile, ...]] = {}
        self.active_transactions = 0
        self.existing_slugs: set[str] = set()
        self.existing_display_names: set[str] = set()
        self.creator = PinnedSkillCreator(
            skill_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            payload_checksum="c" * 64,
            skill_md_content="# Skill Creator\n\nUse progressive disclosure.",
        )

    def __call__(self):
        return _FakeSession(self)


class _FakeTransaction:
    def __init__(self, database: _FakeSkillDatabase) -> None:
        self.database = database

    async def __aenter__(self):
        self.database.active_transactions += 1

    async def __aexit__(self, exc_type, exc, traceback):
        self.database.active_transactions -= 1


class _FakeSession:
    def __init__(self, database: _FakeSkillDatabase) -> None:
        self.database = database

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    def begin(self):
        return _FakeTransaction(self.database)

    async def flush(self):
        return None

    async def execute(self, statement):
        del statement
        for operation in self.database.operations.values():
            if operation.status == "in_progress":
                operation.status = "failed"
                operation.public_error_code = "SKILL_DESIGN_GENERATION_INTERRUPTED"
        return None


class _FakeSkillDesignRepository:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.database = session.database

    async def lock_session_create_scope(self, context):
        del context

    async def count_incomplete(self, context):
        return sum(row.project_id == context.project_id and row.owner_user_id == str(context.user_id) and row.status not in {"completed", "cancelled"} for row in self.database.sessions.values())

    async def get_by_create_idempotency(
        self,
        context,
        idempotency_key_hash,
        *,
        for_update=False,
    ):
        del for_update
        return next(
            (row for row in self.database.sessions.values() if row.project_id == context.project_id and row.owner_user_id == str(context.user_id) and row.create_idempotency_key_hash == idempotency_key_hash),
            None,
        )

    async def create(self, context, row):
        assert row.project_id == context.project_id
        self.database.sessions[row.id] = row
        self.database.files[row.id] = ()
        return row

    async def get(self, context, session_id, *, for_update=False):
        del for_update
        row = self.database.sessions.get(session_id)
        if row is None or row.project_id != context.project_id or row.owner_user_id != str(context.user_id):
            raise AssetNotFound(context.request_id)
        return row

    async def list_incomplete(self, context, *, limit=20):
        return tuple(row for row in self.database.sessions.values() if row.project_id == context.project_id and row.owner_user_id == str(context.user_id) and row.status not in {"completed", "cancelled"})[:limit]

    async def get_operation(
        self,
        context,
        *,
        operation_kind,
        idempotency_key_hash,
        for_update=False,
    ):
        del context, for_update
        return self.database.operations.get((operation_kind, idempotency_key_hash))

    async def create_operation(self, context, row):
        assert row.project_id == context.project_id
        self.database.operations[(row.operation_kind, row.idempotency_key_hash)] = row
        return row

    async def load_draft_files(
        self,
        context,
        session_id,
        *,
        for_update=False,
    ):
        del for_update
        await self.get(context, session_id)
        return self.database.files.get(session_id, ())

    async def replace_draft_files(self, context, session_id, files):
        await self.get(context, session_id, for_update=True)
        self.database.files[session_id] = tuple(sorted(files, key=lambda item: item.path))

    async def clear_draft_files(self, context, session_id):
        await self.get(context, session_id, for_update=True)
        self.database.files[session_id] = ()

    async def project_skill_name_exists(
        self,
        context,
        *,
        slug,
        display_name,
    ):
        del context
        return slug.casefold() in self.database.existing_slugs or display_name.casefold() in self.database.existing_display_names

    async def resolve_current_skill_creator(self, context):
        del context
        return self.database.creator

    async def load_pinned_skill_creator(self, context, row):
        del context
        creator = self.database.creator
        assert row.skill_creator_skill_id == creator.skill_id
        assert row.skill_creator_version_id == creator.version_id
        assert row.skill_creator_payload_checksum == creator.payload_checksum
        return creator


def _candidate_for(slug: str) -> CandidateResult:
    return CandidateResult(
        files=(
            SkillDesignGeneratedFile(
                path="SKILL.md",
                media_type="text/markdown",
                content=(f"---\nname: {slug}\ndescription: Generate concise release notes.\n---\n\n# Workflow\n\nSummarize merged changes by label."),
            ),
        ),
        summary="候选 Skill 已生成。",
    )


class _CandidateSkillGenerator:
    def __init__(self, database: _FakeSkillDatabase) -> None:
        self.database = database
        self.calls: list[tuple[object, str]] = []

    async def generate(self, request, *, skill_creator_content):
        assert self.database.active_transactions == 0
        self.calls.append((request, skill_creator_content))
        return _candidate_for(request.skill_slug)


class _ConversationSkillGenerator(_CandidateSkillGenerator):
    async def generate(self, request, *, skill_creator_content):
        assert self.database.active_transactions == 0
        self.calls.append((request, skill_creator_content))
        if len(self.calls) == 1:
            return NeedsClarificationResult(
                questions=(
                    ClarificationQuestion(
                        id="source",
                        prompt="合并记录来自哪个系统？",
                        reason="需要确定稳定的数据来源。",
                        kind="single_select",
                        required=True,
                        options=("GitHub", "GitLab"),
                    ),
                )
            )
        return _candidate_for(request.skill_slug)


class _SecretCandidateGenerator(_CandidateSkillGenerator):
    async def generate(self, request, *, skill_creator_content):
        assert self.database.active_transactions == 0
        self.calls.append((request, skill_creator_content))
        return CandidateResult(
            files=(
                SkillDesignGeneratedFile(
                    path="SKILL.md",
                    media_type="text/markdown",
                    content=(f"---\nname: {request.skill_slug}\ndescription: Unsafe candidate.\n---\n\napi_key=supersecret123"),
                ),
            ),
            summary="Unsafe candidate.",
        )


class _FakeSkillService:
    def __init__(self, database: _FakeSkillDatabase) -> None:
        self.database = database
        self.pure = SkillService(database)
        self.create_calls: list[tuple[object, object, object, object]] = []
        self.assets: dict[uuid.UUID, SkillAssetView] = {}
        self.versions: dict[uuid.UUID, SkillVersionView] = {}

    async def preview_archive(self, actor, files):
        return await self.pure.preview_archive(actor, files)

    async def apply_draft_changes(
        self,
        actor,
        files,
        changes,
        *,
        expected_draft_checksum,
    ):
        return await self.pure.apply_draft_changes(
            actor,
            files,
            changes,
            expected_draft_checksum=expected_draft_checksum,
        )

    async def create_project_from_preview_in_session(
        self,
        session,
        actor,
        command,
        preview,
    ):
        assert self.database.active_transactions == 1
        self.create_calls.append((session, actor, command, preview))
        now = datetime.now(UTC)
        skill_id = uuid.uuid4()
        version_id = uuid.uuid4()
        asset = SkillAssetView(
            id=skill_id,
            scope=AssetScope.PROJECT,
            project_id=actor.project_id,
            slug=command.slug,
            display_name=command.display_name,
            status="suspended",
            current_published_version_id=version_id,
            version=3,
            created_by_user_id=str(actor.user_id),
            created_at=now,
            updated_at=now,
            description=preview.description,
        )
        version = SkillVersionView(
            id=version_id,
            skill_id=skill_id,
            version_number=1,
            workflow_status=WorkflowStatus.PUBLISHED,
            description=preview.description,
            frontmatter=preview.frontmatter,
            compatibility=preview.compatibility,
            secret_requirements=preview.secret_requirements,
            scan_decision=preview.scan_decision,
            scan_rule_ids=preview.scan_rule_ids,
            scan_summary=preview.scan_summary,
            file_views=preview.file_views,
            supersedes_version_id=None,
            payload_checksum=preview.checksum,
            created_by_user_id=str(actor.user_id),
            created_at=now,
        )
        self.assets[skill_id] = asset
        self.versions[version_id] = version
        return ProjectSkillArchiveCreateResult(
            asset=asset,
            version=version,
        )

    async def get(self, actor, skill_id):
        asset = self.assets.get(skill_id)
        if asset is None or asset.project_id != actor.project_id:
            raise AssetNotFound(actor.request_id)
        return asset


def _service_with_candidate(
    context: ProjectContext,
) -> tuple[
    object,
    _FakeSkillDatabase,
    _CandidateSkillGenerator,
    _FakeSkillService,
]:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    database = _FakeSkillDatabase()
    generator = _CandidateSkillGenerator(database)
    skill_service = _FakeSkillService(database)
    service = module.SkillDesignService(
        database,
        generator=generator,
        skill_service=skill_service,
        repository_factory=_FakeSkillDesignRepository,
    )
    del context
    return service, database, generator, skill_service


@pytest.mark.asyncio
async def test_skill_design_create_pins_creator_checks_names_and_is_idempotent() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    context = _editor_context()
    service, database, generator, _ = _service_with_candidate(context)
    database.existing_slugs.add("existing-skill")

    with pytest.raises(AssetConflict):
        await service.create(
            context,
            module.CreateSkillDesignSession(
                slug="existing-skill",
                display_name="Existing Skill",
                idempotency_key="existing-1",
            ),
        )
    assert not generator.calls

    database.existing_slugs.clear()
    command = module.CreateSkillDesignSession(
        slug="release-notes",
        display_name="Release Notes",
        idempotency_key="create-1",
    )
    created = await service.create(context, command)
    repeated = await service.create(context, command)

    assert repeated == created
    assert created.status is module.SkillDesignStatus.INTERVIEWING
    row = database.sessions[created.id]
    assert row.skill_creator_skill_id == database.creator.skill_id
    assert row.skill_creator_version_id == database.creator.version_id
    assert row.skill_creator_payload_checksum == database.creator.payload_checksum
    with pytest.raises(AssetConflict):
        await service.create(
            context,
            dataclasses.replace(command, display_name="Different"),
        )


@pytest.mark.asyncio
async def test_skill_design_generation_is_two_phase_and_keeps_full_conversation() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    context = _editor_context()
    database = _FakeSkillDatabase()
    generator = _ConversationSkillGenerator(database)
    skill_service = _FakeSkillService(database)
    service = module.SkillDesignService(
        database,
        generator=generator,
        skill_service=skill_service,
        repository_factory=_FakeSkillDesignRepository,
    )
    created = await service.create(
        context,
        module.CreateSkillDesignSession(
            slug="release-notes",
            display_name="Release Notes",
            idempotency_key="create-conversation",
        ),
    )
    clarification = await service.submit_turn(
        context,
        created.id,
        module.SubmitSkillDesignTurn(
            input=module.SkillDesignMessageTurn(
                kind="message",
                message="从合并的拉取请求生成发布说明。",
            ),
            expected_revision=created.revision,
            idempotency_key="turn-question",
        ),
    )
    assert clarification.status is module.SkillDesignStatus.AWAITING_CLARIFICATION
    assert clarification.active_clarification is not None
    selected = clarification.active_clarification.options[0]
    ready_command = module.SubmitSkillDesignTurn(
        input=module.SkillDesignClarificationTurn(
            kind="clarification",
            response=module.SkillDesignClarificationResponse(
                version=1,
                kind="human_input_response",
                source=clarification.active_clarification.source,
                request_id=clarification.active_clarification.request_id,
                response_kind="option",
                value=selected.value,
                option_id=selected.id,
            ),
        ),
        expected_revision=clarification.revision,
        idempotency_key="turn-answer",
    )
    ready = await service.submit_turn(
        context,
        created.id,
        ready_command,
    )
    repeated = await service.submit_turn(
        context,
        created.id,
        ready_command,
    )

    assert ready.status is module.SkillDesignStatus.DRAFT_READY
    assert repeated == ready
    assert len(generator.calls) == 2
    assert all(creator == database.creator.skill_md_content for _, creator in generator.calls)
    second_request = generator.calls[1][0]
    assert "user: 从合并的拉取请求生成发布说明。" in second_request.brief
    assert "assistant: 合并记录来自哪个系统？" in second_request.brief
    assert second_request.brief.endswith("user: GitHub")
    assert database.active_transactions == 0


@pytest.mark.asyncio
async def test_manual_draft_edit_invalidates_validation_without_requiring_valid_skill() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    context = _editor_context()
    service, _, _, _ = _service_with_candidate(context)
    created = await service.create(
        context,
        module.CreateSkillDesignSession(
            slug="release-notes",
            display_name="Release Notes",
            idempotency_key="create-edit",
        ),
    )
    ready = await service.submit_turn(
        context,
        created.id,
        module.SubmitSkillDesignTurn(
            input=module.SkillDesignMessageTurn(
                kind="message",
                message="生成发布说明。",
            ),
            expected_revision=created.revision,
            idempotency_key="turn-edit",
        ),
    )
    validated = await service.validate(
        context,
        created.id,
        module.ValidateSkillDesignSession(
            expected_revision=ready.revision,
            expected_draft_checksum=ready.draft_checksum,
            idempotency_key="validate-edit",
        ),
    )
    edited = await service.submit_turn(
        context,
        created.id,
        module.SubmitSkillDesignTurn(
            input=module.SkillDesignDraftUpdateTurn(
                kind="draft_update",
                expected_draft_checksum=validated.draft_checksum,
                changes=(
                    module.SkillFileChange(
                        op="replace",
                        path="SKILL.md",
                        content="# Temporarily invalid",
                        media_type="text/markdown",
                    ),
                ),
            ),
            expected_revision=validated.revision,
            idempotency_key="manual-edit",
        ),
    )

    assert validated.status is module.SkillDesignStatus.VALIDATED
    assert [item.status.value for item in validated.progress] == [
        "completed",
        "completed",
        "completed",
    ]
    assert edited.status is module.SkillDesignStatus.DRAFT_READY
    assert edited.validation is None
    assert edited.draft_checksum != validated.draft_checksum
    assert edited.files[0].content == "# Temporarily invalid"


@pytest.mark.asyncio
async def test_validate_and_commit_create_suspended_published_skill_atomically_and_idempotently() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    context = _editor_context()
    service, database, _, skill_service = _service_with_candidate(context)
    created = await service.create(
        context,
        module.CreateSkillDesignSession(
            slug="release-notes",
            display_name="Release Notes",
            idempotency_key="create-commit",
        ),
    )
    ready = await service.submit_turn(
        context,
        created.id,
        module.SubmitSkillDesignTurn(
            input=module.SkillDesignMessageTurn(
                kind="message",
                message="生成发布说明。",
            ),
            expected_revision=created.revision,
            idempotency_key="turn-commit",
        ),
    )
    validated = await service.validate(
        context,
        created.id,
        module.ValidateSkillDesignSession(
            expected_revision=ready.revision,
            expected_draft_checksum=ready.draft_checksum,
            idempotency_key="validate-commit",
        ),
    )
    command = module.CommitSkillDesignSession(
        expected_revision=validated.revision,
        expected_draft_checksum=validated.draft_checksum,
        acknowledge_warnings=False,
        idempotency_key="commit-1",
    )
    committed = await service.commit(context, created.id, command)
    repeated = await service.commit(context, created.id, command)

    assert repeated == committed
    assert committed.session.status is module.SkillDesignStatus.COMPLETED
    assert committed.session.created_skill_id == committed.skill.id
    assert committed.session.files == ()
    assert committed.skill.status == "suspended"
    assert skill_service.versions[committed.skill.current_published_version_id].workflow_status is WorkflowStatus.PUBLISHED
    assert len(skill_service.create_calls) == 1
    assert database.files[created.id] == ()
    assert [item.status.value for item in committed.session.progress] == [
        "completed",
        "completed",
        "completed",
    ]


@pytest.mark.asyncio
async def test_cancel_physically_clears_draft_and_private_derived_state() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    context = _editor_context()
    service, database, _, _ = _service_with_candidate(context)
    created = await service.create(
        context,
        module.CreateSkillDesignSession(
            slug="release-notes",
            display_name="Release Notes",
            idempotency_key="create-cancel",
        ),
    )
    ready = await service.submit_turn(
        context,
        created.id,
        module.SubmitSkillDesignTurn(
            input=module.SkillDesignMessageTurn(
                kind="message",
                message="生成发布说明。",
            ),
            expected_revision=created.revision,
            idempotency_key="turn-cancel",
        ),
    )
    row = database.sessions[created.id]
    row.validation_json = {"draft_checksum": ready.draft_checksum}
    row.validated_draft_checksum = ready.draft_checksum
    row.active_clarification_json = {"private": "derived"}
    row.error_code = "OLD"
    row.error_message = "old"
    cancelled = await service.cancel(
        context,
        created.id,
        module.CancelSkillDesignSession(
            expected_revision=ready.revision,
            idempotency_key="cancel-1",
        ),
    )

    assert cancelled.status is module.SkillDesignStatus.CANCELLED
    assert cancelled.files == ()
    assert cancelled.draft_checksum is None
    assert cancelled.validation is None
    assert cancelled.active_clarification is None
    assert cancelled.error_code is None
    assert cancelled.error_message is None
    assert database.files[created.id] == ()


@pytest.mark.asyncio
async def test_stale_generation_is_recovered_to_stable_failed_state() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    context = _editor_context()
    service, database, _, _ = _service_with_candidate(context)
    service._stale_after = timedelta(seconds=1)
    created = await service.create(
        context,
        module.CreateSkillDesignSession(
            slug="release-notes",
            display_name="Release Notes",
            idempotency_key="create-stale",
        ),
    )
    row = database.sessions[created.id]
    row.status = module.SkillDesignStatus.GENERATING.value
    row.updated_at = datetime.now(UTC) - timedelta(minutes=1)
    operation = service._new_operation(
        context,
        created.id,
        kind="turn",
        idempotency_hash="d" * 64,
        request_checksum="e" * 64,
    )
    database.operations[("turn", "d" * 64)] = operation

    recovered = await service.get(context, created.id)

    assert recovered.status is module.SkillDesignStatus.FAILED
    assert recovered.error_code == "SKILL_DESIGN_GENERATION_INTERRUPTED"
    assert operation.status == "failed"
    assert [item.status.value for item in recovered.progress] == [
        "completed",
        "failed",
        "pending",
    ]


@pytest.mark.asyncio
async def test_generation_reserves_two_message_slots_before_operation_or_model() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    context = _editor_context()

    service, database, generator, _ = _service_with_candidate(context)
    created = await service.create(
        context,
        module.CreateSkillDesignSession(
            slug="release-notes",
            display_name="Release Notes",
            idempotency_key="create-capacity-ok",
        ),
    )
    row = database.sessions[created.id]
    row.messages_json = [
        module.SkillDesignService._message_json(
            "user" if index % 2 else "assistant",
            f"history-{index}",
            now=datetime.now(UTC),
        )
        for index in range(126)
    ]
    ready = await service.submit_turn(
        context,
        created.id,
        module.SubmitSkillDesignTurn(
            input=module.SkillDesignMessageTurn(
                kind="message",
                message="final requirement",
            ),
            expected_revision=created.revision,
            idempotency_key="capacity-ok",
        ),
    )
    assert ready.status is module.SkillDesignStatus.DRAFT_READY
    assert len(database.sessions[created.id].messages_json) == 128
    assert len(generator.calls) == 1

    blocked_service, blocked_db, blocked_generator, _ = _service_with_candidate(context)
    blocked = await blocked_service.create(
        context,
        module.CreateSkillDesignSession(
            slug="blocked-skill",
            display_name="Blocked Skill",
            idempotency_key="create-capacity-blocked",
        ),
    )
    blocked_row = blocked_db.sessions[blocked.id]
    blocked_row.messages_json = [
        module.SkillDesignService._message_json(
            "user" if index % 2 else "assistant",
            f"history-{index}",
            now=datetime.now(UTC),
        )
        for index in range(127)
    ]

    with pytest.raises(AssetValidationFailed):
        await blocked_service.submit_turn(
            context,
            blocked.id,
            module.SubmitSkillDesignTurn(
                input=module.SkillDesignMessageTurn(
                    kind="message",
                    message="must be rejected before generation",
                ),
                expected_revision=blocked.revision,
                idempotency_key="capacity-blocked",
            ),
        )
    assert blocked_row.status == module.SkillDesignStatus.INTERVIEWING.value
    assert blocked_row.revision == blocked.revision
    assert not blocked_generator.calls
    assert not blocked_db.operations


@pytest.mark.asyncio
async def test_skill_hard_delete_tombstones_completed_builder_reference_first() -> None:
    context = _editor_context()
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace()),
        scalar=AsyncMock(return_value=None),
        flush=AsyncMock(return_value=None),
        delete=AsyncMock(return_value=None),
    )
    repository = SkillRepository(session)
    asset = SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=context.project_id,
        current_published_version_id=uuid.uuid4(),
        status="suspended",
    )

    await repository.delete_project_asset(context, asset, ())

    tombstone = session.execute.await_args_list[0].args[0]
    sql = str(
        tombstone.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert sql.startswith("UPDATE skill_design_sessions")
    assert "created_skill_id=NULL" in sql
    assert "created_skill_version_id=NULL" in sql
    assert "created_skill_deleted=true" in sql
    assert str(context.project_id) in sql
    assert str(asset.id) in sql
    session.delete.assert_awaited_once_with(asset)


@pytest.mark.asyncio
async def test_secret_like_message_is_rejected_before_persistence_or_model() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    context = _editor_context()
    service, database, generator, _ = _service_with_candidate(context)
    created = await service.create(
        context,
        module.CreateSkillDesignSession(
            slug="release-notes",
            display_name="Release Notes",
            idempotency_key="create-secret-message",
        ),
    )

    with pytest.raises(AssetValidationFailed):
        await service.submit_turn(
            context,
            created.id,
            module.SubmitSkillDesignTurn(
                input=module.SkillDesignMessageTurn(
                    kind="message",
                    message="api_key=supersecret123",
                ),
                expected_revision=created.revision,
                idempotency_key="secret-message",
            ),
        )

    assert len(database.sessions[created.id].messages_json) == 1
    assert not database.operations
    assert not generator.calls


@pytest.mark.asyncio
async def test_secret_like_clarification_and_manual_edit_are_not_stored() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    context = _editor_context()
    database = _FakeSkillDatabase()
    generator = _ConversationSkillGenerator(database)
    skill_service = _FakeSkillService(database)
    service = module.SkillDesignService(
        database,
        generator=generator,
        skill_service=skill_service,
        repository_factory=_FakeSkillDesignRepository,
    )
    created = await service.create(
        context,
        module.CreateSkillDesignSession(
            slug="release-notes",
            display_name="Release Notes",
            idempotency_key="create-secret-turns",
        ),
    )
    clarification = await service.submit_turn(
        context,
        created.id,
        module.SubmitSkillDesignTurn(
            input=module.SkillDesignMessageTurn(
                kind="message",
                message="生成发布说明。",
            ),
            expected_revision=created.revision,
            idempotency_key="ask-secret-turns",
        ),
    )
    assert clarification.active_clarification is not None
    with pytest.raises(AssetValidationFailed):
        await service.submit_turn(
            context,
            created.id,
            module.SubmitSkillDesignTurn(
                input=module.SkillDesignClarificationTurn(
                    kind="clarification",
                    response=module.SkillDesignClarificationResponse(
                        version=1,
                        kind="human_input_response",
                        source=clarification.active_clarification.source,
                        request_id=clarification.active_clarification.request_id,
                        response_kind="text",
                        value="password=supersecret123",
                    ),
                ),
                expected_revision=clarification.revision,
                idempotency_key="secret-clarification",
            ),
        )
    assert database.sessions[created.id].status == module.SkillDesignStatus.AWAITING_CLARIFICATION.value
    assert len(generator.calls) == 1

    selected = clarification.active_clarification.options[0]
    ready = await service.submit_turn(
        context,
        created.id,
        module.SubmitSkillDesignTurn(
            input=module.SkillDesignClarificationTurn(
                kind="clarification",
                response=module.SkillDesignClarificationResponse(
                    version=1,
                    kind="human_input_response",
                    source=clarification.active_clarification.source,
                    request_id=clarification.active_clarification.request_id,
                    response_kind="option",
                    option_id=selected.id,
                    value=selected.value,
                ),
            ),
            expected_revision=clarification.revision,
            idempotency_key="safe-clarification",
        ),
    )
    original = database.files[created.id]
    with pytest.raises(AssetValidationFailed):
        await service.submit_turn(
            context,
            created.id,
            module.SubmitSkillDesignTurn(
                input=module.SkillDesignDraftUpdateTurn(
                    kind="draft_update",
                    expected_draft_checksum=ready.draft_checksum,
                    changes=(
                        module.SkillFileChange(
                            op="replace",
                            path="SKILL.md",
                            content="access_token=supersecret123",
                            media_type="text/markdown",
                        ),
                    ),
                ),
                expected_revision=ready.revision,
                idempotency_key="secret-manual-edit",
            ),
        )
    assert database.files[created.id] == original


@pytest.mark.asyncio
async def test_secret_like_model_candidate_never_becomes_a_draft_or_get_payload() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    context = _editor_context()
    database = _FakeSkillDatabase()
    generator = _SecretCandidateGenerator(database)
    service = module.SkillDesignService(
        database,
        generator=generator,
        skill_service=_FakeSkillService(database),
        repository_factory=_FakeSkillDesignRepository,
    )
    created = await service.create(
        context,
        module.CreateSkillDesignSession(
            slug="release-notes",
            display_name="Release Notes",
            idempotency_key="create-secret-candidate",
        ),
    )

    failed = await service.submit_turn(
        context,
        created.id,
        module.SubmitSkillDesignTurn(
            input=module.SkillDesignMessageTurn(
                kind="message",
                message="生成发布说明。",
            ),
            expected_revision=created.revision,
            idempotency_key="secret-candidate",
        ),
    )

    assert failed.status is module.SkillDesignStatus.FAILED
    assert failed.files == ()
    assert database.files[created.id] == ()
    assert "supersecret123" not in repr(failed)

    database.sessions[created.id].messages_json[0]["content"] = "Bearer abcdefghijklmnopqrstuvwxyz"
    with pytest.raises(AssetValidationFailed):
        await service.get(context, created.id)


@pytest.mark.asyncio
async def test_read_only_list_and_get_do_not_recover_stale_generation() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    editor = _editor_context()
    viewer = dataclasses.replace(
        editor,
        role=ProjectRole.VIEWER,
        capabilities=capabilities_for(ProjectRole.VIEWER),
    )
    service, database, _, _ = _service_with_candidate(editor)
    created = await service.create(
        editor,
        module.CreateSkillDesignSession(
            slug="release-notes",
            display_name="Release Notes",
            idempotency_key="create-read-only-stale",
        ),
    )
    row = database.sessions[created.id]
    row.status = module.SkillDesignStatus.GENERATING.value
    row.updated_at = datetime.now(UTC) - timedelta(hours=1)

    fetched = await service.get(viewer, created.id)
    listed = await service.list_incomplete(viewer)

    assert fetched.status is module.SkillDesignStatus.GENERATING
    assert listed[0].status is module.SkillDesignStatus.GENERATING
    assert row.status == module.SkillDesignStatus.GENERATING.value
    assert row.revision == created.revision


@pytest.mark.asyncio
async def test_completed_new_idempotency_keys_cannot_bypass_revision_binding() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    context = _editor_context()
    service, _, _, _ = _service_with_candidate(context)
    created = await service.create(
        context,
        module.CreateSkillDesignSession(
            slug="release-notes",
            display_name="Release Notes",
            idempotency_key="create-completed-binding",
        ),
    )
    ready = await service.submit_turn(
        context,
        created.id,
        module.SubmitSkillDesignTurn(
            input=module.SkillDesignMessageTurn(
                kind="message",
                message="生成发布说明。",
            ),
            expected_revision=created.revision,
            idempotency_key="turn-completed-binding",
        ),
    )
    validated = await service.validate(
        context,
        created.id,
        module.ValidateSkillDesignSession(
            expected_revision=ready.revision,
            expected_draft_checksum=ready.draft_checksum,
            idempotency_key="validate-completed-binding",
        ),
    )
    committed = await service.commit(
        context,
        created.id,
        module.CommitSkillDesignSession(
            expected_revision=validated.revision,
            expected_draft_checksum=validated.draft_checksum,
            acknowledge_warnings=False,
            idempotency_key="commit-original",
        ),
    )
    with pytest.raises(AssetConflict):
        await service.commit(
            context,
            created.id,
            module.CommitSkillDesignSession(
                expected_revision=validated.revision,
                expected_draft_checksum=validated.draft_checksum,
                acknowledge_warnings=False,
                idempotency_key="commit-new-stale-revision",
            ),
        )
    rebound = await service.commit(
        context,
        created.id,
        module.CommitSkillDesignSession(
            expected_revision=committed.session.revision,
            expected_draft_checksum=committed.session.draft_checksum,
            acknowledge_warnings=False,
            idempotency_key="commit-new-current-revision",
        ),
    )
    assert rebound.session == committed.session

    cancel_service, _, _, _ = _service_with_candidate(context)
    cancel_created = await cancel_service.create(
        context,
        module.CreateSkillDesignSession(
            slug="cancel-binding",
            display_name="Cancel Binding",
            idempotency_key="create-cancel-binding",
        ),
    )
    cancelled = await cancel_service.cancel(
        context,
        cancel_created.id,
        module.CancelSkillDesignSession(
            expected_revision=cancel_created.revision,
            idempotency_key="cancel-original",
        ),
    )
    with pytest.raises(AssetConflict):
        await cancel_service.cancel(
            context,
            cancel_created.id,
            module.CancelSkillDesignSession(
                expected_revision=cancel_created.revision,
                idempotency_key="cancel-new-stale-revision",
            ),
        )
    rebound_cancel = await cancel_service.cancel(
        context,
        cancel_created.id,
        module.CancelSkillDesignSession(
            expected_revision=cancelled.revision,
            idempotency_key="cancel-new-current-revision",
        ),
    )
    assert rebound_cancel == cancelled


@pytest.mark.asyncio
async def test_incomplete_session_limit_is_strict_but_idempotent_and_reclaimable() -> None:
    module = importlib.import_module("app.shared_assets.skill_design_service")
    context = _editor_context()
    service, _, _, _ = _service_with_candidate(context)
    created = []
    for index in range(module.MAX_INCOMPLETE_SKILL_DESIGN_SESSIONS_PER_OWNER_PROJECT):
        command = module.CreateSkillDesignSession(
            slug=f"bounded-{index}",
            display_name=f"Bounded {index}",
            idempotency_key=f"bounded-{index}",
        )
        created.append((await service.create(context, command), command))

    assert await service.create(context, created[0][1]) == created[0][0]
    with pytest.raises(AssetStorageQuotaExceeded) as captured:
        await service.create(
            context,
            module.CreateSkillDesignSession(
                slug="bounded-overflow",
                display_name="Bounded Overflow",
                idempotency_key="bounded-overflow",
            ),
        )
    assert captured.value.status_code == 429

    cancelled = await service.cancel(
        context,
        created[0][0].id,
        module.CancelSkillDesignSession(
            expected_revision=created[0][0].revision,
            idempotency_key="bounded-cancel",
        ),
    )
    assert cancelled.status is module.SkillDesignStatus.CANCELLED
    replacement = await service.create(
        context,
        module.CreateSkillDesignSession(
            slug="bounded-replacement",
            display_name="Bounded Replacement",
            idempotency_key="bounded-replacement",
        ),
    )
    assert replacement.status is module.SkillDesignStatus.INTERVIEWING
