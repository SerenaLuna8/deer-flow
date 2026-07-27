from __future__ import annotations

import dataclasses
import importlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_design_generation import (
    DEFAULT_GENERATION_TIMEOUT_SECONDS,
    AgentDesignDraft,
    AgentDesignGenerationUnavailable,
    CandidateResult,
    ClarificationQuestion,
    NeedsClarificationResult,
)
from app.shared_assets.agent_service import (
    AgentAssetView,
    AgentVersionView,
    ProjectAgentCreateResult,
)
from app.shared_assets.errors import (
    AssetConflict,
    AssetNotFound,
    AssetStorageUnavailable,
    AssetValidationFailed,
)
from app.shared_assets.models import AssetScope, WorkflowStatus


def _editor_context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=capabilities_for(ProjectRole.EDITOR),
        membership_version=1,
        request_id="req-agent-design",
    )


def test_agent_design_exposes_frozen_domain_contracts() -> None:
    module = importlib.import_module("app.shared_assets.agent_design_service")

    assert module.AgentDesignStatus.INTERVIEWING == "interviewing"
    assert module.AgentDesignStatus.PROPOSAL_READY == "proposal_ready"
    assert module.AgentDesignStatus.COMPLETED == "completed"
    assert module.AgentDesignStatus.CANCELLED == "cancelled"
    for contract in (
        module.CreateAgentDesignSession,
        module.AgentDesignBlueprint,
        module.AgentDesignMessage,
        module.AgentDesignSessionView,
        module.AgentDesignCommitResult,
    ):
        assert dataclasses.is_dataclass(contract)
        assert contract.__dataclass_params__.frozen is True


@pytest.mark.asyncio
async def test_invalid_agent_design_create_is_rejected_before_storage_or_model_call() -> None:
    module = importlib.import_module("app.shared_assets.agent_design_service")

    class ExplodingSessionFactory:
        def __call__(self):
            raise AssertionError("invalid input must not open persistence")

    class ExplodingGenerator:
        async def generate(self, request):
            raise AssertionError("create must not call the model")

    service = module.AgentDesignService(
        ExplodingSessionFactory(),
        generator=ExplodingGenerator(),
    )
    with pytest.raises(AssetValidationFailed) as captured:
        await service.create(
            _editor_context(),
            module.CreateAgentDesignSession(
                slug="Not Valid",
                display_name="Agent",
                idempotency_key="idem-1",
            ),
        )
    assert captured.value.request_id == "req-agent-design"


def test_agent_design_blueprint_checksum_is_canonical_and_covers_all_documents() -> None:
    module = importlib.import_module("app.shared_assets.agent_design_service")
    blueprint = module.AgentDesignBlueprint(
        description="Test",
        model_ref="model-a",
        tool_groups=("web",),
        skill_version_ids=(uuid.uuid4(),),
        mcp_version_ids=(uuid.uuid4(),),
        agents_instructions="# AGENTS",
        soul="# SOUL",
        identity="# IDENTITY",
        user_context="# USER",
    )

    assert module.AgentDesignService.blueprint_checksum(blueprint) == module.AgentDesignService.blueprint_checksum(blueprint)
    assert module.AgentDesignService.blueprint_checksum(dataclasses.replace(blueprint, identity="# different")) != module.AgentDesignService.blueprint_checksum(blueprint)


def test_agent_design_blueprint_requires_all_four_logical_documents() -> None:
    module = importlib.import_module("app.shared_assets.agent_design_service")
    blueprint = module.AgentDesignBlueprint(
        description="Test",
        model_ref="default",
        tool_groups=("web",),
        skill_version_ids=(),
        mcp_version_ids=(),
        agents_instructions="# AGENTS",
        soul="",
        identity="# IDENTITY",
        user_context="# USER",
    )

    with pytest.raises(AssetValidationFailed):
        module.AgentDesignService._validate_blueprint(
            _editor_context(),
            blueprint,
        )


def test_default_stale_recovery_outlives_the_generation_deadline() -> None:
    module = importlib.import_module("app.shared_assets.agent_design_service")
    service = module.AgentDesignService(lambda: None, generator=object())

    assert service._stale_after.total_seconds() > (DEFAULT_GENERATION_TIMEOUT_SECONDS)


def test_agent_design_persistence_models_are_registered_in_final_metadata() -> None:
    import deerflow.persistence.models  # noqa: F401
    from deerflow.persistence.base import Base

    assert "agent_design_sessions" in Base.metadata.tables
    assert "agent_design_operations" in Base.metadata.tables
    session = Base.metadata.tables["agent_design_sessions"]
    assert {
        "project_id",
        "owner_user_id",
        "slug",
        "status",
        "revision",
        "messages_json",
        "blueprint_json",
        "blueprint_checksum",
        "created_agent_id",
        "created_agent_version_id",
        "created_agent_deleted",
    } <= set(session.columns.keys())
    assert {constraint.name for constraint in session.constraints if constraint.name is not None} >= {
        "fk_agent_design_sessions_membership",
        "fk_agent_design_sessions_created_agent_project",
        "fk_agent_design_sessions_created_agent_version",
        "uq_agent_design_sessions_private_scope",
        "uq_agent_design_sessions_create_idempotency",
    }
    operation = Base.metadata.tables["agent_design_operations"]
    assert {constraint.name for constraint in operation.constraints if constraint.name is not None} >= {
        "fk_agent_design_operations_session",
        "uq_agent_design_operations_idempotency",
    }


class _FakeDatabase:
    def __init__(self) -> None:
        self.sessions = {}
        self.operations = {}
        self.active_transactions = 0

    def __call__(self):
        return _FakeSession(self)


class _FakeTransaction:
    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database

    async def __aenter__(self):
        self.database.active_transactions += 1

    async def __aexit__(self, exc_type, exc, traceback):
        self.database.active_transactions -= 1


class _FakeSession:
    def __init__(self, database: _FakeDatabase) -> None:
        self.database = database

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    def begin(self):
        return _FakeTransaction(self.database)

    async def flush(self):
        return None


class _FakeAgentDesignRepository:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.database = session.database

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


class _CandidateGenerator:
    def __init__(self, database: _FakeDatabase, *, fail_once=False) -> None:
        self.database = database
        self.fail_once = fail_once
        self.calls = []

    async def generate(self, request, *, context):
        assert self.database.active_transactions == 0
        self.calls.append((request, context))
        if self.fail_once:
            self.fail_once = False
            raise AgentDesignGenerationUnavailable(
                "AGENT_DESIGN_GENERATION_UNAVAILABLE",
                "stable",
            )
        return CandidateResult(
            documents=AgentDesignDraft(
                agents_instructions="# Mission\nTest changes.",
                soul="# Style\nPragmatic.",
                identity="# Role\nTest engineer.",
                user_context="# User\nChinese output.",
            ),
            changed_fields=(
                "agents_instructions",
                "soul",
                "identity",
                "user_context",
            ),
            capability_claims=(),
        )


class _ClarificationGenerator(_CandidateGenerator):
    async def generate(self, request, *, context):
        assert self.database.active_transactions == 0
        self.calls.append((request, context))
        if len(self.calls) == 1:
            return NeedsClarificationResult(
                questions=(
                    ClarificationQuestion(
                        id="primary-role",
                        targets=("agents_instructions",),
                        prompt="主要职责是什么？",
                        reason="用于确定工作边界。",
                        kind="single_select",
                        required=True,
                        options=("测试执行", "测试规划"),
                    ),
                )
            )
        return CandidateResult(
            documents=AgentDesignDraft(
                agents_instructions="# Mission\nTest changes.",
                soul="# Style\nPragmatic.",
                identity="# Role\nTest engineer.",
                user_context="# User\nChinese output.",
            ),
            changed_fields=(
                "agents_instructions",
                "soul",
                "identity",
                "user_context",
            ),
            capability_claims=(),
        )


@pytest.mark.asyncio
async def test_message_turn_is_two_phase_uses_defaults_and_is_idempotent() -> None:
    module = importlib.import_module("app.shared_assets.agent_design_service")
    context = _editor_context()
    database = _FakeDatabase()
    generator = _CandidateGenerator(database)
    service = module.AgentDesignService(
        database,
        generator=generator,
        repository_factory=_FakeAgentDesignRepository,
    )
    create = module.CreateAgentDesignSession(
        slug="test-agent",
        display_name="Test Agent",
        idempotency_key="create-1",
    )

    created = await service.create(context, create)
    repeated_create = await service.create(context, create)
    command = module.SubmitAgentDesignTurn(
        input=module.AgentDesignMessageTurn(
            kind="message",
            message="负责回归测试并输出中文报告",
        ),
        expected_revision=created.revision,
        idempotency_key="turn-1",
    )
    ready = await service.submit_turn(context, created.id, command)
    repeated_turn = await service.submit_turn(context, created.id, command)

    assert repeated_create.id == created.id
    assert ready.status is module.AgentDesignStatus.PROPOSAL_READY
    assert repeated_turn == ready
    assert ready.revision == 3
    assert ready.blueprint is not None
    assert ready.blueprint.description == "负责回归测试并输出中文报告"
    assert ready.blueprint.model_ref == "default"
    assert ready.blueprint.tool_groups == (
        "web",
        "file:read",
        "file:write",
        "bash",
        "task",
    )
    assert len(generator.calls) == 1
    request, generation_context = generator.calls[0]
    assert request.brief == ready.blueprint.description
    assert generation_context.allowed_capabilities == ready.blueprint.tool_groups


@pytest.mark.asyncio
async def test_failed_turn_can_retry_with_same_idempotency_key() -> None:
    module = importlib.import_module("app.shared_assets.agent_design_service")
    context = _editor_context()
    database = _FakeDatabase()
    generator = _CandidateGenerator(database, fail_once=True)
    service = module.AgentDesignService(
        database,
        generator=generator,
        repository_factory=_FakeAgentDesignRepository,
    )
    created = await service.create(
        context,
        module.CreateAgentDesignSession(
            slug="retry-agent",
            display_name="Retry Agent",
            idempotency_key="create-retry",
        ),
    )
    command = module.SubmitAgentDesignTurn(
        input=module.AgentDesignMessageTurn(
            kind="message",
            message="负责可靠性测试",
        ),
        expected_revision=created.revision,
        idempotency_key="turn-retry",
    )

    failed = await service.submit_turn(context, created.id, command)
    ready = await service.submit_turn(context, created.id, command)

    assert failed.status is module.AgentDesignStatus.FAILED
    assert failed.error_code == "AGENT_DESIGN_GENERATION_UNAVAILABLE"
    assert failed.active_clarification is None
    assert failed.blueprint is None
    assert failed.blueprint_checksum is None
    assert ready.status is module.AgentDesignStatus.PROPOSAL_READY
    assert ready.error_code is None
    assert len(generator.calls) == 2


@pytest.mark.asyncio
async def test_failed_revision_keeps_the_last_valid_blueprint() -> None:
    module = importlib.import_module("app.shared_assets.agent_design_service")
    context = _editor_context()
    database = _FakeDatabase()
    generator = _CandidateGenerator(database)
    service = module.AgentDesignService(
        database,
        generator=generator,
        repository_factory=_FakeAgentDesignRepository,
    )
    created = await service.create(
        context,
        module.CreateAgentDesignSession(
            slug="revision-failure-agent",
            display_name="Revision Failure Agent",
            idempotency_key="create-revision-failure",
        ),
    )
    ready = await service.submit_turn(
        context,
        created.id,
        module.SubmitAgentDesignTurn(
            input=module.AgentDesignMessageTurn(
                kind="message",
                message="负责可靠性测试",
            ),
            expected_revision=created.revision,
            idempotency_key="turn-revision-initial",
        ),
    )
    generator.fail_once = True

    failed = await service.submit_turn(
        context,
        created.id,
        module.SubmitAgentDesignTurn(
            input=module.AgentDesignMessageTurn(
                kind="message",
                message="调整为更重视回归风险",
            ),
            expected_revision=ready.revision,
            idempotency_key="turn-revision-failure",
        ),
    )

    assert failed.status is module.AgentDesignStatus.FAILED
    assert failed.blueprint == ready.blueprint
    assert failed.blueprint_checksum == ready.blueprint_checksum


@pytest.mark.asyncio
async def test_clarification_option_must_match_the_server_issued_request() -> None:
    module = importlib.import_module("app.shared_assets.agent_design_service")
    context = _editor_context()
    database = _FakeDatabase()
    generator = _ClarificationGenerator(database)
    service = module.AgentDesignService(
        database,
        generator=generator,
        repository_factory=_FakeAgentDesignRepository,
    )
    created = await service.create(
        context,
        module.CreateAgentDesignSession(
            slug="clarification-agent",
            display_name="Clarification Agent",
            idempotency_key="create-clarification",
        ),
    )
    clarification = await service.submit_turn(
        context,
        created.id,
        module.SubmitAgentDesignTurn(
            input=module.AgentDesignMessageTurn(
                kind="message",
                message="帮助团队测试",
            ),
            expected_revision=created.revision,
            idempotency_key="turn-clarification",
        ),
    )
    assert clarification.status is module.AgentDesignStatus.AWAITING_CLARIFICATION
    assert clarification.active_clarification is not None
    assert clarification.blueprint is None
    assert clarification.blueprint_checksum is None

    with pytest.raises(AssetConflict):
        await service.submit_turn(
            context,
            created.id,
            module.SubmitAgentDesignTurn(
                input=module.AgentDesignClarificationTurn(
                    kind="clarification",
                    response=module.AgentDesignClarificationResponse(
                        version=1,
                        kind="human_input_response",
                        source=clarification.active_clarification.source,
                        request_id=clarification.active_clarification.request_id,
                        response_kind="option",
                        option_id="primary-role-999",
                        value="越权注入的其他职责",
                    ),
                ),
                expected_revision=clarification.revision,
                idempotency_key="answer-invalid-option",
            ),
        )

    assert database.sessions[created.id].revision == clarification.revision
    assert database.sessions[created.id].active_clarification_json is not None
    assert len(generator.calls) == 1

    ready = await service.submit_turn(
        context,
        created.id,
        module.SubmitAgentDesignTurn(
            input=module.AgentDesignClarificationTurn(
                kind="clarification",
                response=module.AgentDesignClarificationResponse(
                    version=1,
                    kind="human_input_response",
                    source=clarification.active_clarification.source,
                    request_id=clarification.active_clarification.request_id,
                    response_kind="option",
                    option_id="primary-role-1",
                    value="测试执行",
                ),
            ),
            expected_revision=clarification.revision,
            idempotency_key="answer-valid-option",
        ),
    )

    assert ready.status is module.AgentDesignStatus.PROPOSAL_READY
    assert len(generator.calls) == 2


@pytest.mark.asyncio
async def test_failed_turn_cannot_overwrite_a_newer_blueprint_with_the_old_idempotency_key() -> None:
    module = importlib.import_module("app.shared_assets.agent_design_service")
    context = _editor_context()
    database = _FakeDatabase()
    generator = _CandidateGenerator(database, fail_once=True)
    service = module.AgentDesignService(
        database,
        generator=generator,
        repository_factory=_FakeAgentDesignRepository,
    )
    created = await service.create(
        context,
        module.CreateAgentDesignSession(
            slug="stale-retry-agent",
            display_name="Stale Retry Agent",
            idempotency_key="create-stale-retry",
        ),
    )
    original = module.SubmitAgentDesignTurn(
        input=module.AgentDesignMessageTurn(
            kind="message",
            message="负责可靠性测试",
        ),
        expected_revision=created.revision,
        idempotency_key="turn-stale-retry",
    )
    failed = await service.submit_turn(context, created.id, original)
    updated = await service.submit_turn(
        context,
        created.id,
        module.SubmitAgentDesignTurn(
            input=module.AgentDesignBlueprintTurn(
                kind="blueprint_update",
                blueprint=module.AgentDesignBlueprint(
                    description="更新后的设定",
                    model_ref="default",
                    tool_groups=("web",),
                    skill_version_ids=(),
                    mcp_version_ids=(),
                    agents_instructions="# AGENTS\nUse the newer plan.",
                    soul="# SOUL\nCareful.",
                    identity="# IDENTITY\nReliability engineer.",
                    user_context="# USER\nReply in Chinese.",
                ),
            ),
            expected_revision=failed.revision,
            idempotency_key="blueprint-after-failure",
        ),
    )

    with pytest.raises(AssetConflict):
        await service.submit_turn(context, created.id, original)

    assert database.sessions[created.id].revision == updated.revision
    assert database.sessions[created.id].blueprint_checksum == updated.blueprint_checksum
    assert len(generator.calls) == 1


class _FakeAgentService:
    def __init__(self) -> None:
        self.calls = []

    async def create_project_from_design_in_session(
        self,
        session,
        context,
        command,
        payload,
    ):
        self.calls.append((session, context, command, payload))
        now = datetime.now(UTC)
        agent_id = uuid.uuid4()
        version_id = uuid.uuid4()
        return ProjectAgentCreateResult(
            asset=AgentAssetView(
                id=agent_id,
                scope=AssetScope.PROJECT,
                project_id=context.project_id,
                slug=command.slug,
                display_name=command.display_name,
                status="suspended",
                current_published_version_id=version_id,
                version=2,
                created_by_user_id=str(context.user_id),
                created_at=now,
                updated_at=now,
            ),
            version=AgentVersionView(
                id=version_id,
                agent_id=agent_id,
                version_number=1,
                workflow_status=WorkflowStatus.PUBLISHED,
                description=payload.description,
                model_ref=payload.model_ref,
                tool_groups=payload.tool_groups,
                skill_version_ids=payload.skill_version_ids,
                mcp_version_ids=payload.mcp_version_ids,
                supersedes_version_id=None,
                payload_checksum="a" * 64,
                created_by_user_id=str(context.user_id),
                created_at=now,
                agents_instructions=payload.agents_instructions,
                soul=payload.soul,
                identity=payload.identity,
                user_context=payload.user_context,
                payload_schema_version=2,
            ),
        )


@pytest.mark.asyncio
async def test_commit_calls_atomic_agent_creation_and_completes_session() -> None:
    module = importlib.import_module("app.shared_assets.agent_design_service")
    context = _editor_context()
    database = _FakeDatabase()
    generator = _CandidateGenerator(database)
    agent_service = _FakeAgentService()
    service = module.AgentDesignService(
        database,
        generator=generator,
        agent_service=agent_service,
        repository_factory=_FakeAgentDesignRepository,
    )
    created = await service.create(
        context,
        module.CreateAgentDesignSession(
            slug="commit-agent",
            display_name="Commit Agent",
            idempotency_key="create-commit",
        ),
    )
    ready = await service.submit_turn(
        context,
        created.id,
        module.SubmitAgentDesignTurn(
            input=module.AgentDesignMessageTurn(
                kind="message",
                message="负责提交验证",
            ),
            expected_revision=created.revision,
            idempotency_key="turn-commit",
        ),
    )
    assert ready.blueprint_checksum is not None

    committed = await service.commit(
        context,
        created.id,
        module.CommitAgentDesignSession(
            expected_revision=ready.revision,
            expected_blueprint_checksum=ready.blueprint_checksum,
            idempotency_key="commit-1",
        ),
    )

    assert committed.session.status is module.AgentDesignStatus.COMPLETED
    assert committed.session.created_agent_id == committed.agent.id
    assert committed.agent.status == "suspended"
    assert committed.version.workflow_status is WorkflowStatus.PUBLISHED
    assert len(agent_service.calls) == 1
    assert agent_service.calls[0][3].payload_schema_version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constraint_name", "expected_error"),
    [
        ("uq_agents_project_slug", AssetConflict),
        ("ck_agent_versions_checksum", AssetStorageUnavailable),
        (None, AssetStorageUnavailable),
    ],
)
async def test_design_commit_only_maps_known_integrity_conflicts_to_409(
    constraint_name: str | None,
    expected_error: type[Exception],
) -> None:
    module = importlib.import_module("app.shared_assets.agent_design_service")
    context = _editor_context()
    database = _FakeDatabase()
    generator = _CandidateGenerator(database)

    class ConstraintViolation(Exception):
        def __init__(self, name: str | None) -> None:
            self.constraint_name = name

    class FailingAgentService:
        async def create_project_from_design_in_session(
            self,
            session,
            actor,
            command,
            payload,
        ):
            del session, actor, command, payload
            raise IntegrityError(
                "sensitive SQL must not escape",
                {"secret": "hidden"},
                ConstraintViolation(constraint_name),
            )

    service = module.AgentDesignService(
        database,
        generator=generator,
        agent_service=FailingAgentService(),
        repository_factory=_FakeAgentDesignRepository,
    )
    created = await service.create(
        context,
        module.CreateAgentDesignSession(
            slug="integrity-agent",
            display_name="Integrity Agent",
            idempotency_key="create-integrity",
        ),
    )
    ready = await service.submit_turn(
        context,
        created.id,
        module.SubmitAgentDesignTurn(
            input=module.AgentDesignMessageTurn(
                kind="message",
                message="负责完整性验证",
            ),
            expected_revision=created.revision,
            idempotency_key="turn-integrity",
        ),
    )
    assert ready.blueprint_checksum is not None

    with pytest.raises(expected_error) as captured:
        await service.commit(
            context,
            created.id,
            module.CommitAgentDesignSession(
                expected_revision=ready.revision,
                expected_blueprint_checksum=ready.blueprint_checksum,
                idempotency_key="commit-integrity",
            ),
        )

    assert "sensitive SQL" not in str(captured.value)
    assert "hidden" not in str(captured.value)
