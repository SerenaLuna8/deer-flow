from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.gateway.routers.project_agent_builder import (
    AgentDesignCommitRequest,
    AgentDesignSessionListResponse,
    CreateAgentDesignSessionRequest,
    _session_item,
    commit_agent_design_session,
)
from app.gateway.routers.project_assets import raise_asset_domain
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.agent_design_generation import AgentDesignConflict
from app.shared_assets.agent_design_repository import AgentDesignRepository
from app.shared_assets.agent_design_service import (
    AgentDesignBlueprint,
    AgentDesignBlueprintTurn,
    AgentDesignClarificationResponse,
    AgentDesignClarificationTurn,
    AgentDesignMessageTurn,
    AgentDesignService,
    AgentDesignStatus,
    CancelAgentDesignSession,
    CommitAgentDesignSession,
    CreateAgentDesignSession,
    SubmitAgentDesignTurn,
)
from app.shared_assets.errors import (
    AgentDesignConflictUnresolved,
    AgentDesignSecretDetected,
    AgentDesignSessionLimitExceeded,
    AgentDesignSlugConflict,
    AssetConflict,
    AssetValidationFailed,
    SharedAssetError,
)
from deerflow.persistence.shared_assets import (
    AgentDesignOperationRow,
    AgentDesignSessionRow,
)


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.EDITOR,
        capabilities=frozenset(
            {
                Capability.SHARED_ASSETS_READ,
                Capability.SHARED_ASSETS_EDIT,
            }
        ),
        membership_version=3,
        request_id="request-agent-builder-safety",
    )


def _read_only_context() -> ProjectContext:
    return replace(
        _context(),
        role=ProjectRole.VIEWER,
        capabilities=frozenset({Capability.SHARED_ASSETS_READ}),
    )


class _TransactionSession:
    def begin(self) -> _TransactionSession:
        return self

    async def __aenter__(self) -> _TransactionSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def execute(self, _statement: object) -> None:
        return None


def _blueprint(service: AgentDesignService) -> AgentDesignBlueprint:
    return AgentDesignBlueprint(
        description="审查代码并输出可验证的问题清单。",
        model_ref="default",
        tool_groups=("file:read",),
        skill_version_ids=(),
        mcp_version_ids=(),
        agents_instructions="读取代码，定位问题，并给出证据。",
        soul="严谨、直接、可验证。",
        identity="代码审查 Agent。",
        user_context="使用中文，按风险排序。",
    )


def _row(
    context: ProjectContext,
    *,
    status: AgentDesignStatus = AgentDesignStatus.INTERVIEWING,
    revision: int = 1,
    blueprint_json: dict[str, object] | None = None,
    blueprint_checksum: str | None = None,
) -> AgentDesignSessionRow:
    now = datetime.now(UTC)
    return AgentDesignSessionRow(
        id=uuid.uuid4(),
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        thread_id=uuid.uuid4(),
        slug="reviewer",
        display_name="Reviewer",
        status=status.value,
        revision=revision,
        messages_json=[],
        progress_json=[],
        active_clarification_json=None,
        blueprint_json=blueprint_json,
        blueprint_checksum=blueprint_checksum,
        error_code=None,
        error_message=None,
        created_agent_id=None,
        created_agent_version_id=None,
        create_idempotency_key_hash="a" * 64,
        create_request_checksum="b" * 64,
        created_at=now,
        updated_at=now,
    )


def _no_database() -> _TransactionSession:
    raise AssertionError("secret-bearing input must be rejected before opening a database session")


@pytest.mark.asyncio
async def test_secret_display_name_is_rejected_before_persistence() -> None:
    context = _context()
    service = AgentDesignService(_no_database)  # type: ignore[arg-type]

    with pytest.raises(AssetValidationFailed):
        await service.create(
            context,
            CreateAgentDesignSession(
                slug="safe-agent",
                display_name="sk-abcdefghijklmnopqrst",
                idempotency_key="create-secret-name",
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turn",
    [
        AgentDesignMessageTurn(
            kind="message",
            message="Use Bearer abcdefghijklmnopqrstuvwxyz for the request",
        ),
        AgentDesignClarificationTurn(
            kind="clarification",
            response=AgentDesignClarificationResponse(
                version=1,
                kind="human_input_response",
                source="agent_builder",
                request_id="credentials",
                response_kind="text",
                value="sk-abcdefghijklmnopqrst",
            ),
        ),
        AgentDesignBlueprintTurn(
            kind="blueprint_update",
            blueprint=AgentDesignBlueprint(
                description="审查代码并输出问题。",
                model_ref="default",
                tool_groups=("file:read",),
                skill_version_ids=(),
                mcp_version_ids=(),
                agents_instructions="读取代码并给出证据。",
                soul="Bearer abcdefghijklmnopqrstuvwxyz",
                identity="代码审查 Agent。",
                user_context="使用中文。",
            ),
        ),
    ],
)
async def test_secret_in_any_turn_is_rejected_before_persistence(
    turn: AgentDesignMessageTurn | AgentDesignClarificationTurn | AgentDesignBlueprintTurn,
) -> None:
    context = _context()
    service = AgentDesignService(_no_database)  # type: ignore[arg-type]

    with pytest.raises(AssetValidationFailed):
        await service.submit_turn(
            context,
            uuid.uuid4(),
            SubmitAgentDesignTurn(
                input=turn,
                expected_revision=1,
                idempotency_key="turn-secret-input",
            ),
        )


def test_edited_retry_after_initial_failure_becomes_authoritative_brief() -> None:
    context = _context()
    service = AgentDesignService(lambda: _TransactionSession())  # type: ignore[arg-type]
    row = _row(context, status=AgentDesignStatus.FAILED, revision=3)
    now = datetime.now(UTC)
    row.error_code = "AGENT_DESIGN_GENERATION_UNAVAILABLE"
    row.error_message = "generation unavailable"
    row.messages_json = [
        service._message_json(  # noqa: SLF001 - focused retry contract
            "user",
            "最初的旧需求",
            now=now - timedelta(seconds=1),
        ),
        service._message_json(  # noqa: SLF001 - focused retry contract
            "user",
            "改写后的权威需求",
            now=now,
        ),
    ]
    blueprint = service._default_blueprint("最初的旧需求")  # noqa: SLF001
    turn = AgentDesignMessageTurn(kind="message", message="改写后的权威需求")

    first = service._generation_request(row, blueprint, turn)  # noqa: SLF001
    replay = service._generation_request(row, blueprint, turn)  # noqa: SLF001

    assert first.brief == "改写后的权威需求"
    assert replay == first


def test_legacy_edited_brief_remains_authoritative_during_later_clarifications() -> None:
    context = _context()
    service = AgentDesignService(lambda: _TransactionSession())  # type: ignore[arg-type]
    row = _row(context, status=AgentDesignStatus.FAILED, revision=4)
    row.error_code = "AGENT_DESIGN_GENERATION_UNAVAILABLE"
    row.error_message = "generation unavailable"
    now = datetime.now(UTC)
    row.messages_json = [
        service._message_json(  # noqa: SLF001 - pre-marker persisted message
            "user",
            "最初的旧需求",
            now=now - timedelta(seconds=2),
        ),
        service._message_json(  # noqa: SLF001 - pre-marker retry message
            "user",
            "改写后的权威需求",
            now=now - timedelta(seconds=1),
        ),
        {
            **service._message_json("user", "澄清答案", now=now),  # noqa: SLF001
            "clarification_request_id": "scope",
        },
    ]

    assert service._first_user_message(row) == "改写后的权威需求"  # noqa: SLF001


def test_session_view_round_trips_candidate_assumptions_and_conflicts() -> None:
    context = _context()
    service = AgentDesignService(lambda: _TransactionSession())  # type: ignore[arg-type]
    blueprint = _blueprint(service)
    raw = service._blueprint_json(  # noqa: SLF001
        blueprint,
        assumptions=("待审查仓库允许只读访问。",),
        conflicts=(
            AgentDesignConflict(
                code="POLICY_CONFLICT",
                fields=("soul",),
                message="严谨性要求与无条件服从发生冲突。",
                severity="error",
            ),
        ),
    )
    row = _row(
        context,
        status=AgentDesignStatus.PROPOSAL_READY,
        revision=4,
        blueprint_json=raw,
        blueprint_checksum=service.blueprint_checksum(blueprint),
    )

    view = service._session_view(row)  # noqa: SLF001

    assert view.assumptions == ("待审查仓库允许只读访问。",)
    assert [conflict.code for conflict in view.conflicts] == ["POLICY_CONFLICT"]
    assert view.conflicts[0].severity == "error"
    response = _session_item(view)
    assert response.assumptions == view.assumptions
    assert response.conflicts[0].code == "POLICY_CONFLICT"


def test_manual_blueprint_edits_do_not_self_resolve_a_conflict() -> None:
    service = AgentDesignService(lambda: _TransactionSession())  # type: ignore[arg-type]
    current = _blueprint(service)
    conflict = AgentDesignConflict(
        code="POLICY_CONFLICT",
        fields=("soul",),
        message="存在未解决的策略冲突。",
        severity="error",
    )
    unrelated_edit = replace(
        current,
        user_context="使用中文，并附带证据链接。",
    )
    conflicted_field_edit = replace(
        current,
        soul="严谨、直接，并遵守所有授权边界。",
    )

    assert service._remaining_conflicts_after_blueprint_update(  # noqa: SLF001
        current,
        unrelated_edit,
        (conflict,),
    ) == (conflict,)
    assert service._remaining_conflicts_after_blueprint_update(  # noqa: SLF001
        current,
        conflicted_field_edit,
        (conflict,),
    ) == (conflict,)
    warning_raw = service._blueprint_json(  # noqa: SLF001
        current,
        conflicts=(conflict.model_copy(update={"severity": "warning"}),),
    )
    assert service._has_blocking_conflicts(warning_raw) is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_commit_rejects_unresolved_error_conflict() -> None:
    context = _context()
    fake_session = _TransactionSession()
    service = AgentDesignService(lambda: fake_session)  # type: ignore[arg-type]
    blueprint = _blueprint(service)
    raw = service._blueprint_json(blueprint)  # noqa: SLF001
    raw["conflicts"] = [
        AgentDesignConflict(
            code="POLICY_CONFLICT",
            fields=("soul",),
            message="存在未解决的策略冲突。",
            severity="error",
        ).model_dump(mode="json")
    ]
    row = _row(
        context,
        status=AgentDesignStatus.PROPOSAL_READY,
        revision=5,
        blueprint_json=raw,
        blueprint_checksum=service.blueprint_checksum(blueprint),
    )

    class _Repository:
        async def get_operation(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def get(self, *_args: object, **_kwargs: object) -> AgentDesignSessionRow:
            return row

        async def create_operation(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def project_agent_slug_exists(self, *_args: object, **_kwargs: object) -> bool:
            return False

    class _AgentService:
        async def create_project_from_design_in_session(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("Agent creation must not run with unresolved error conflicts")

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
        agent_service=_AgentService(),  # type: ignore[arg-type]
    )

    with pytest.raises(AssetConflict):
        await service.commit(
            context,
            row.id,
            CommitAgentDesignSession(
                expected_revision=row.revision,
                expected_blueprint_checksum=row.blueprint_checksum or "",
                idempotency_key="commit-blocking-conflict",
            ),
        )


def test_commit_request_keeps_slug_optional_for_legacy_clients() -> None:
    legacy = AgentDesignCommitRequest.model_validate(
        {
            "expected_revision": 5,
            "expected_blueprint_checksum": "a" * 64,
            "idempotency_key": "legacy-commit",
        }
    )
    renamed = AgentDesignCommitRequest.model_validate(
        {
            "expected_revision": 5,
            "expected_blueprint_checksum": "a" * 64,
            "idempotency_key": "renamed-commit",
            "slug": "recovered-reviewer",
        }
    )

    assert legacy.slug is None
    assert renamed.slug == "recovered-reviewer"


@pytest.mark.asyncio
async def test_commit_replays_a_legacy_checksum_that_omitted_optional_slug() -> None:
    context = _context()
    fake_session = _TransactionSession()
    service = AgentDesignService(lambda: fake_session)  # type: ignore[arg-type]
    row = _row(
        context,
        status=AgentDesignStatus.COMPLETED,
        revision=6,
        blueprint_checksum="a" * 64,
    )
    idempotency_key = "legacy-commit-replay"
    operation = AgentDesignOperationRow(
        id=uuid.uuid4(),
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        session_id=row.id,
        operation_kind="commit",
        idempotency_key_hash=service._idempotency_hash(idempotency_key),  # noqa: SLF001
        request_checksum=service._request_checksum(  # noqa: SLF001
            {
                "session_id": row.id,
                "expected_revision": 5,
                "expected_blueprint_checksum": row.blueprint_checksum,
            }
        ),
        status="completed",
        result_revision=row.revision,
        public_error_code=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class _Repository:
        async def get_operation(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> AgentDesignOperationRow:
            return operation

        async def get(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> AgentDesignSessionRow:
            return row

    committed = object()

    async def _committed_result(*_args: object, **_kwargs: object) -> object:
        return committed

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
    )
    service._committed_result = _committed_result  # type: ignore[method-assign]  # noqa: SLF001

    result = await service.commit(
        context,
        row.id,
        CommitAgentDesignSession(
            expected_revision=5,
            expected_blueprint_checksum=row.blueprint_checksum or "",
            idempotency_key=idempotency_key,
        ),
    )

    assert result is committed


@pytest.mark.parametrize(
    "slug",
    (
        "a",
        "ab",
        "a--b",
        "-agent",
        "agent-",
        "Agent",
        "a" * 64,
    ),
)
def test_builder_request_models_reject_noncanonical_slugs(slug: str) -> None:
    with pytest.raises(ValidationError):
        CreateAgentDesignSessionRequest.model_validate(
            {
                "slug": slug,
                "display_name": "Reviewer",
                "idempotency_key": "create-invalid-slug",
            }
        )

    with pytest.raises(ValidationError):
        AgentDesignCommitRequest.model_validate(
            {
                "expected_revision": 5,
                "expected_blueprint_checksum": "a" * 64,
                "idempotency_key": "commit-invalid-slug",
                "slug": slug,
            }
        )


@pytest.mark.parametrize("slug", ("abc", "a" * 63, "review-agent-2"))
def test_builder_request_models_accept_canonical_slug_boundaries(slug: str) -> None:
    created = CreateAgentDesignSessionRequest.model_validate(
        {
            "slug": slug,
            "display_name": "Reviewer",
            "idempotency_key": "create-valid-slug",
        }
    )
    committed = AgentDesignCommitRequest.model_validate(
        {
            "expected_revision": 5,
            "expected_blueprint_checksum": "a" * 64,
            "idempotency_key": "commit-valid-slug",
            "slug": slug,
        }
    )

    assert created.slug == slug
    assert committed.slug == slug


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ("a", "ab", "a--b", "-agent", "agent-", "Agent", "a" * 64))
async def test_builder_service_rejects_noncanonical_create_and_commit_slugs(
    slug: str,
) -> None:
    context = _context()
    service = AgentDesignService(_no_database)  # type: ignore[arg-type]

    with pytest.raises(AssetValidationFailed):
        await service.create(
            context,
            CreateAgentDesignSession(
                slug=slug,
                display_name="Reviewer",
                idempotency_key="create-invalid-slug",
            ),
        )

    with pytest.raises(AssetValidationFailed):
        await service.commit(
            context,
            uuid.uuid4(),
            CommitAgentDesignSession(
                expected_revision=5,
                expected_blueprint_checksum="a" * 64,
                idempotency_key="commit-invalid-slug",
                slug=slug,
            ),
        )


@pytest.mark.asyncio
async def test_commit_route_forwards_optional_slug_to_the_domain_command() -> None:
    context = _context()
    session_id = uuid.uuid4()
    captured: list[CommitAgentDesignSession] = []

    class _StopRoute(Exception):
        pass

    class _Service:
        async def commit(
            self,
            _context: ProjectContext,
            _session_id: uuid.UUID,
            command: CommitAgentDesignSession,
        ) -> None:
            captured.append(command)
            raise _StopRoute

    with pytest.raises(_StopRoute):
        await commit_agent_design_session(
            session_id,
            AgentDesignCommitRequest(
                expected_revision=5,
                expected_blueprint_checksum="a" * 64,
                idempotency_key="route-override-slug",
                slug="route-recovered-reviewer",
            ),
            context,
            _Service(),  # type: ignore[arg-type]
        )

    assert len(captured) == 1
    assert captured[0].slug == "route-recovered-reviewer"


@pytest.mark.asyncio
async def test_commit_slug_secret_is_rejected_before_persistence() -> None:
    context = _context()
    service = AgentDesignService(_no_database)  # type: ignore[arg-type]

    with pytest.raises(AgentDesignSecretDetected):
        await service.commit(
            context,
            uuid.uuid4(),
            CommitAgentDesignSession(
                expected_revision=5,
                expected_blueprint_checksum="a" * 64,
                idempotency_key="commit-secret-slug",
                slug="sk-abcdefghijklmnopqrst",
            ),
        )


@pytest.mark.asyncio
async def test_commit_rechecks_a_legacy_session_slug_for_secrets() -> None:
    context = _context()
    fake_session = _TransactionSession()
    base_service = AgentDesignService(lambda: fake_session)  # type: ignore[arg-type]
    blueprint = _blueprint(base_service)
    row = _row(
        context,
        status=AgentDesignStatus.PROPOSAL_READY,
        revision=5,
        blueprint_json=base_service._blueprint_json(blueprint),  # noqa: SLF001
        blueprint_checksum=base_service.blueprint_checksum(blueprint),
    )
    row.slug = "sk-abcdefghijklmnopqrst"

    class _Repository:
        async def get_operation(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def get(self, *_args: object, **_kwargs: object) -> AgentDesignSessionRow:
            return row

        async def project_agent_slug_exists(self, *_args: object, **_kwargs: object) -> bool:
            raise AssertionError("secret legacy slug must fail before Agent lookup")

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
    )

    with pytest.raises(AgentDesignSecretDetected):
        await service.commit(
            context,
            row.id,
            CommitAgentDesignSession(
                expected_revision=row.revision,
                expected_blueprint_checksum=row.blueprint_checksum or "",
                idempotency_key="commit-legacy-secret-slug",
            ),
        )


@pytest.mark.asyncio
async def test_commit_rejects_a_legacy_secret_blueprint_before_operation_write() -> None:
    context = _context()
    fake_session = _TransactionSession()
    base_service = AgentDesignService(lambda: fake_session)  # type: ignore[arg-type]
    blueprint = replace(
        _blueprint(base_service),
        agents_instructions="Use Bearer abcdefghijklmnop when reviewing code.",
    )
    row = _row(
        context,
        status=AgentDesignStatus.PROPOSAL_READY,
        revision=5,
        blueprint_json=base_service._blueprint_json(blueprint),  # noqa: SLF001
        blueprint_checksum=base_service.blueprint_checksum(blueprint),
    )

    class _Repository:
        async def get_operation(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def get(self, *_args: object, **_kwargs: object) -> AgentDesignSessionRow:
            return row

        async def project_agent_slug_exists(self, *_args: object, **_kwargs: object) -> bool:
            return False

        async def create_operation(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("legacy secret blueprint must fail before operation persistence")

    class _AgentService:
        async def create_project_from_design_in_session(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("legacy secret blueprint must never create an Agent version")

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
        agent_service=_AgentService(),  # type: ignore[arg-type]
    )

    with pytest.raises(AgentDesignSecretDetected):
        await service.commit(
            context,
            row.id,
            CommitAgentDesignSession(
                expected_revision=row.revision,
                expected_blueprint_checksum=row.blueprint_checksum or "",
                idempotency_key="commit-legacy-secret-blueprint",
            ),
        )


@pytest.mark.asyncio
async def test_commit_override_replaces_legacy_secret_display_name_and_syncs_identity() -> None:
    context = _context()
    fake_session = _TransactionSession()
    base_service = AgentDesignService(lambda: fake_session)  # type: ignore[arg-type]
    blueprint = _blueprint(base_service)
    row = _row(
        context,
        status=AgentDesignStatus.PROPOSAL_READY,
        revision=5,
        blueprint_json=base_service._blueprint_json(blueprint),  # noqa: SLF001
        blueprint_checksum=base_service.blueprint_checksum(blueprint),
    )
    row.display_name = "sk-abcdefghijklmnopqrst"
    preflight_slugs: list[str] = []
    create_commands: list[object] = []
    commit_operations: list[AgentDesignOperationRow] = []
    created_agent_id = uuid.uuid4()
    created_version_id = uuid.uuid4()

    class _Repository:
        async def get_operation(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def get(self, *_args: object, **_kwargs: object) -> AgentDesignSessionRow:
            return row

        async def project_agent_slug_exists(
            self,
            _context: ProjectContext,
            slug: str,
            **_kwargs: object,
        ) -> bool:
            preflight_slugs.append(slug)
            return False

        async def create_operation(
            self,
            _context: ProjectContext,
            operation: AgentDesignOperationRow,
        ) -> None:
            commit_operations.append(operation)

    class _AgentService:
        async def create_project_from_design_in_session(
            self,
            _session: object,
            _context: ProjectContext,
            command: object,
            _payload: object,
        ) -> SimpleNamespace:
            create_commands.append(command)
            return SimpleNamespace(
                asset=SimpleNamespace(
                    id=created_agent_id,
                    slug="recovered-reviewer",
                    display_name="recovered-reviewer",
                ),
                version=SimpleNamespace(id=created_version_id),
            )

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
        agent_service=_AgentService(),  # type: ignore[arg-type]
    )

    result = await service.commit(
        context,
        row.id,
        CommitAgentDesignSession(
            expected_revision=row.revision,
            expected_blueprint_checksum=row.blueprint_checksum or "",
            idempotency_key="commit-override-slug",
            slug="  recovered-reviewer  ",
        ),
    )

    assert preflight_slugs == ["recovered-reviewer"]
    assert len(create_commands) == 1
    assert getattr(create_commands[0], "slug") == "recovered-reviewer"
    assert getattr(create_commands[0], "display_name") == "recovered-reviewer"
    assert len(commit_operations) == 1
    assert commit_operations[0].request_checksum == service._request_checksum(  # noqa: SLF001
        {
            "session_id": row.id,
            "expected_revision": 5,
            "expected_blueprint_checksum": row.blueprint_checksum,
            "slug": "recovered-reviewer",
        }
    )
    assert row.slug == result.session.slug == "recovered-reviewer"
    assert row.display_name == result.session.display_name == "recovered-reviewer"


@pytest.mark.asyncio
async def test_create_preflights_duplicate_project_agent_slug() -> None:
    context = _context()
    fake_session = _TransactionSession()

    class _Repository:
        async def lock_session_create_scope(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def get_by_create_idempotency(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def count_incomplete(self, *_args: object, **_kwargs: object) -> int:
            return 0

        async def project_agent_slug_exists(self, *_args: object, **_kwargs: object) -> bool:
            return True

        async def create(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("duplicate slug must fail before creating a Builder session")

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
    )

    with pytest.raises(AssetConflict) as exc_info:
        await service.create(
            context,
            CreateAgentDesignSession(
                slug="reviewer",
                display_name="Reviewer",
                idempotency_key="create-duplicate-slug",
            ),
        )

    assert exc_info.value.code == "AGENT_DESIGN_SLUG_CONFLICT"


@pytest.mark.asyncio
async def test_create_locks_and_rejects_a_ninth_incomplete_session() -> None:
    context = _context()
    fake_session = _TransactionSession()
    calls: list[str] = []

    class _Repository:
        async def lock_session_create_scope(self, *_args: object, **_kwargs: object) -> None:
            calls.append("lock_create_scope")

        async def get_by_create_idempotency(self, *_args: object, **_kwargs: object) -> None:
            calls.append("get_idempotency")
            return None

        async def count_incomplete(self, *_args: object, **_kwargs: object) -> int:
            calls.append("count_incomplete")
            return 8

        async def project_agent_slug_exists(self, *_args: object, **_kwargs: object) -> bool:
            raise AssertionError("limit must fail before Agent slug preflight")

        async def create(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("limit must fail before session persistence")

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
    )

    with pytest.raises(SharedAssetError) as exc_info:
        await service.create(
            context,
            CreateAgentDesignSession(
                slug="ninth-reviewer",
                display_name="Ninth Reviewer",
                idempotency_key="create-ninth-reviewer",
            ),
        )

    assert exc_info.value.code == "AGENT_DESIGN_SESSION_LIMIT_EXCEEDED"
    assert calls == ["lock_create_scope", "get_idempotency", "count_incomplete"]


@pytest.mark.asyncio
async def test_create_idempotent_replay_precedes_incomplete_session_limit() -> None:
    context = _context()
    fake_session = _TransactionSession()
    service = AgentDesignService(lambda: fake_session)  # type: ignore[arg-type]
    command = CreateAgentDesignSession(
        slug="existing-reviewer",
        display_name="Existing Reviewer",
        idempotency_key="create-existing-reviewer",
    )
    row = _row(context)
    row.slug = command.slug
    row.display_name = command.display_name
    row.create_request_checksum = service._request_checksum(  # noqa: SLF001
        {
            "slug": command.slug,
            "display_name": command.display_name,
        }
    )
    calls: list[str] = []

    class _Repository:
        async def lock_session_create_scope(self, *_args: object, **_kwargs: object) -> None:
            calls.append("lock_create_scope")

        async def get_by_create_idempotency(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> AgentDesignSessionRow:
            calls.append("get_idempotency")
            return row

        async def count_incomplete(self, *_args: object, **_kwargs: object) -> int:
            raise AssertionError("idempotent replay must bypass the current session count")

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
    )

    replayed = await service.create(context, command)

    assert replayed.id == row.id
    assert calls == ["lock_create_scope", "get_idempotency"]


@pytest.mark.asyncio
async def test_cancel_clears_private_draft_content_and_remains_idempotent() -> None:
    context = _context()
    fake_session = _TransactionSession()
    service = AgentDesignService(lambda: fake_session)  # type: ignore[arg-type]
    blueprint = _blueprint(service)
    row = _row(
        context,
        status=AgentDesignStatus.PROPOSAL_READY,
        revision=6,
        blueprint_json=service._blueprint_json(blueprint),  # noqa: SLF001
        blueprint_checksum=service.blueprint_checksum(blueprint),
    )
    row.slug = "sensitive-project-name"
    row.display_name = "Sensitive private draft"
    row.messages_json = [
        service._message_json(  # noqa: SLF001
            "user",
            "private design content",
            now=datetime.now(UTC),
        )
    ]
    row.progress_json = [{"id": "soul", "label": "SOUL.md", "status": "completed"}]
    operation: AgentDesignOperationRow | None = None

    class _Repository:
        async def get_operation(self, *_args: object, **_kwargs: object) -> AgentDesignOperationRow | None:
            return operation

        async def get(self, *_args: object, **_kwargs: object) -> AgentDesignSessionRow:
            return row

        async def lock_in_progress_turn_operations(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> tuple[AgentDesignOperationRow, ...]:
            return ()

        async def create_operation(
            self,
            _context: ProjectContext,
            created: AgentDesignOperationRow,
        ) -> None:
            nonlocal operation
            operation = created

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
    )
    command = CancelAgentDesignSession(
        expected_revision=row.revision,
        idempotency_key="cancel-and-clear",
    )

    first = await service.cancel(context, row.id, command)
    replay = await service.cancel(context, row.id, command)
    fetched = await service.get(context, row.id)

    assert first == replay == fetched
    assert first.status == AgentDesignStatus.CANCELLED
    assert first.messages == ()
    assert first.blueprint is None
    assert first.blueprint_checksum is None
    assert first.progress == ()
    assert first.slug != "sensitive-project-name"
    assert first.display_name != "Sensitive private draft"
    assert operation is not None
    assert operation.status == "completed"


@pytest.mark.asyncio
async def test_cancel_terminalizes_an_in_progress_generation_operation() -> None:
    context = _context()
    fake_session = _TransactionSession()
    row = _row(
        context,
        status=AgentDesignStatus.GENERATING,
        revision=6,
    )
    active_turn = AgentDesignOperationRow(
        id=uuid.uuid4(),
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        session_id=row.id,
        operation_kind="turn",
        idempotency_key_hash="c" * 64,
        request_checksum="d" * 64,
        status="in_progress",
        result_revision=None,
        public_error_code=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    cancel_operation: AgentDesignOperationRow | None = None

    class _Repository:
        async def get_operation(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> AgentDesignOperationRow | None:
            return cancel_operation

        async def lock_in_progress_turn_operations(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> tuple[AgentDesignOperationRow, ...]:
            return (active_turn,)

        async def get(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> AgentDesignSessionRow:
            return row

        async def create_operation(
            self,
            _context: ProjectContext,
            created: AgentDesignOperationRow,
        ) -> None:
            nonlocal cancel_operation
            cancel_operation = created

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
    )

    result = await service.cancel(
        context,
        row.id,
        CancelAgentDesignSession(
            expected_revision=row.revision,
            idempotency_key="cancel-in-progress-generation",
        ),
    )

    assert result.status == AgentDesignStatus.CANCELLED
    assert active_turn.status == "failed"
    assert active_turn.result_revision == result.revision
    assert active_turn.public_error_code == "AGENT_DESIGN_SESSION_CANCELLED"


@pytest.mark.asyncio
async def test_cancelled_session_rejects_an_unrecorded_cancel_key() -> None:
    context = _context()
    fake_session = _TransactionSession()
    row = _row(
        context,
        status=AgentDesignStatus.CANCELLED,
        revision=7,
    )

    class _Repository:
        async def get_operation(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def lock_in_progress_turn_operations(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> tuple[AgentDesignOperationRow, ...]:
            return ()

        async def get(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> AgentDesignSessionRow:
            return row

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
    )

    with pytest.raises(AssetConflict):
        await service.cancel(
            context,
            row.id,
            CancelAgentDesignSession(
                expected_revision=row.revision - 1,
                idempotency_key="unrecorded-concurrent-cancel",
            ),
        )


@pytest.mark.asyncio
async def test_stale_get_locks_turn_operation_before_session() -> None:
    context = _context()
    fake_session = _TransactionSession()
    row = _row(
        context,
        status=AgentDesignStatus.GENERATING,
        revision=3,
    )
    row.updated_at = datetime.now(UTC) - timedelta(minutes=10)
    calls: list[str] = []

    class _Repository:
        session = fake_session

        async def get(
            self,
            *_args: object,
            for_update: bool = False,
            **_kwargs: object,
        ) -> AgentDesignSessionRow:
            calls.append("session_lock" if for_update else "session_read")
            return row

        async def lock_in_progress_turn_operations(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> tuple[AgentDesignOperationRow, ...]:
            calls.append("turn_lock")
            return ()

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
        stale_generating_seconds=1,
    )
    observed_now = datetime.now(UTC)
    clock_calls = 0

    def _now() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return observed_now

    service._now = _now  # type: ignore[method-assign]  # noqa: SLF001

    result = await service.get(context, row.id)

    assert result.status == AgentDesignStatus.FAILED
    assert calls == ["session_read", "turn_lock", "session_lock"]
    assert clock_calls == 1


@pytest.mark.asyncio
async def test_read_only_stale_get_returns_a_snapshot_without_recovery_writes() -> None:
    context = _read_only_context()
    fake_session = _TransactionSession()
    row = _row(
        context,
        status=AgentDesignStatus.GENERATING,
        revision=3,
    )
    row.updated_at = datetime.now(UTC) - timedelta(minutes=10)
    calls: list[str] = []

    class _Repository:
        session = fake_session

        async def get(
            self,
            *_args: object,
            for_update: bool = False,
            **_kwargs: object,
        ) -> AgentDesignSessionRow:
            calls.append("session_lock" if for_update else "session_read")
            return row

        async def lock_in_progress_turn_operations(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> tuple[AgentDesignOperationRow, ...]:
            raise AssertionError("read-only GET must not lock or mutate Builder operations")

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
        stale_generating_seconds=1,
    )

    result = await service.get(context, row.id)

    assert result.status == AgentDesignStatus.GENERATING
    assert result.revision == 3
    assert calls == ["session_read"]


@pytest.mark.asyncio
async def test_read_only_stale_list_returns_a_snapshot_without_recovery_writes() -> None:
    context = _read_only_context()
    fake_session = _TransactionSession()
    row = _row(
        context,
        status=AgentDesignStatus.GENERATING,
        revision=3,
    )
    row.updated_at = datetime.now(UTC) - timedelta(minutes=10)

    class _Repository:
        session = fake_session

        async def list_incomplete(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> tuple[AgentDesignSessionRow, ...]:
            return (row,)

        async def get(self, *_args: object, **_kwargs: object) -> AgentDesignSessionRow:
            raise AssertionError("read-only list must not lock or mutate a Builder session")

        async def lock_in_progress_turn_operations(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> tuple[AgentDesignOperationRow, ...]:
            raise AssertionError("read-only list must not lock or mutate Builder operations")

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
        stale_generating_seconds=1,
    )

    page = await service.list_incomplete(context)

    assert len(page.items) == 1
    assert page.items[0].status == AgentDesignStatus.GENERATING
    assert page.items[0].revision == 3


@pytest.mark.asyncio
async def test_editor_stale_list_still_recovers_the_generation() -> None:
    context = _context()
    fake_session = _TransactionSession()
    row = _row(
        context,
        status=AgentDesignStatus.GENERATING,
        revision=3,
    )
    row.updated_at = datetime.now(UTC) - timedelta(minutes=10)
    calls: list[str] = []

    class _Repository:
        session = fake_session

        async def list_incomplete(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> tuple[AgentDesignSessionRow, ...]:
            return (row,)

        async def lock_in_progress_turn_operations(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> tuple[AgentDesignOperationRow, ...]:
            calls.append("turn_lock")
            return ()

        async def get(
            self,
            *_args: object,
            for_update: bool = False,
            **_kwargs: object,
        ) -> AgentDesignSessionRow:
            calls.append("session_lock" if for_update else "session_read")
            return row

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
        stale_generating_seconds=1,
    )

    page = await service.list_incomplete(context)

    assert len(page.items) == 1
    assert page.items[0].status == AgentDesignStatus.FAILED
    assert calls == ["turn_lock", "session_lock"]


@pytest.mark.asyncio
async def test_incomplete_session_list_exposes_a_next_cursor_without_truncation() -> None:
    context = _context()
    fake_session = _TransactionSession()
    rows = [_row(context, revision=index) for index in (3, 2, 1)]
    for index, row in enumerate(rows):
        row.created_at = datetime.now(UTC) - timedelta(minutes=index)
        row.updated_at = datetime.now(UTC) - timedelta(minutes=index)

    class _Repository:
        async def list_incomplete(
            self,
            _context: ProjectContext,
            *,
            limit: int,
            before_created_at: datetime | None = None,
            before_id: uuid.UUID | None = None,
        ) -> tuple[AgentDesignSessionRow, ...]:
            del before_created_at, before_id
            assert limit == 3
            return tuple(rows)

    service = AgentDesignService(
        lambda: fake_session,  # type: ignore[arg-type]
        repository_factory=lambda _session: _Repository(),  # type: ignore[arg-type]
    )

    page = await service.list_incomplete(context, limit=2)

    assert [item.id for item in page.items] == [rows[0].id, rows[1].id]
    assert page.next_cursor is not None
    assert service._decode_session_cursor(  # noqa: SLF001 - opaque cursor contract
        context,
        page.next_cursor,
    ) == (rows[1].created_at, rows[1].id)
    response = AgentDesignSessionListResponse(
        data=[],
        next_cursor=page.next_cursor,
        request_id=context.request_id,
    )
    assert response.next_cursor == page.next_cursor


@pytest.mark.asyncio
async def test_incomplete_session_list_rejects_a_malformed_cursor_before_query() -> None:
    context = _context()
    service = AgentDesignService(_no_database)  # type: ignore[arg-type]

    with pytest.raises(AssetValidationFailed):
        await service.list_incomplete(context, cursor="not-a-valid-cursor")


@pytest.mark.asyncio
async def test_duplicate_slug_preflight_is_project_scoped_and_case_insensitive() -> None:
    context = _context()

    class _Result:
        @staticmethod
        def scalar_one_or_none() -> uuid.UUID:
            return uuid.uuid4()

    class _Session:
        statement: object | None = None

        async def execute(self, statement: object) -> _Result:
            self.statement = statement
            return _Result()

    session = _Session()
    exists = await AgentDesignRepository(session).project_agent_slug_exists(  # type: ignore[arg-type]
        context,
        "ReViEwEr",
    )

    assert exists is True
    assert session.statement is not None
    sql = str(
        session.statement.compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "agents.scope = 'project'" in sql
    assert f"agents.project_id = '{context.project_id}'" in sql
    assert "agents.status != 'archived'" in sql
    assert "lower(agents.slug) = 'reviewer'" in sql
    assert f"project_memberships.id = '{context.membership_id}'" in sql


@pytest.mark.asyncio
async def test_incomplete_repository_cursor_uses_stable_created_at_and_id_order() -> None:
    context = _context()
    before_created_at = datetime.now(UTC)
    before_id = uuid.uuid4()

    class _Result:
        @staticmethod
        def scalars() -> tuple[AgentDesignSessionRow, ...]:
            return ()

    class _Session:
        statement: object | None = None

        async def execute(self, statement: object) -> _Result:
            self.statement = statement
            return _Result()

    session = _Session()
    await AgentDesignRepository(session).list_incomplete(  # type: ignore[arg-type]
        context,
        limit=3,
        before_created_at=before_created_at,
        before_id=before_id,
    )

    assert session.statement is not None
    sql = str(
        session.statement.compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "agent_design_sessions.created_at <" in sql
    assert "agent_design_sessions.id <" in sql
    assert "ORDER BY agent_design_sessions.created_at DESC, agent_design_sessions.id DESC" in sql
    assert "LIMIT 3" in sql


@pytest.mark.asyncio
async def test_cancel_locks_in_progress_turn_operations_before_session_mutation() -> None:
    context = _context()
    session_id = uuid.uuid4()

    class _Result:
        @staticmethod
        def scalar_one_or_none() -> uuid.UUID:
            return context.project_id

        @staticmethod
        def scalars() -> tuple[AgentDesignOperationRow, ...]:
            return ()

    class _Session:
        statements: list[object]

        def __init__(self) -> None:
            self.statements = []

        async def execute(
            self,
            statement: object,
            _parameters: object | None = None,
        ) -> _Result:
            self.statements.append(statement)
            return _Result()

    session = _Session()
    await AgentDesignRepository(session).lock_in_progress_turn_operations(  # type: ignore[arg-type]
        context,
        session_id,
    )

    assert len(session.statements) == 3
    fence_sql = str(session.statements[1])
    assert "pg_advisory_xact_lock" in fence_sql
    sql = str(
        session.statements[-1].compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert f"agent_design_operations.session_id = '{session_id}'" in sql
    assert "agent_design_operations.operation_kind = 'turn'" in sql
    assert "agent_design_operations.status = 'in_progress'" in sql
    assert "FOR UPDATE OF agent_design_operations" in sql


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (AgentDesignSecretDetected("request-secret"), 422),
        (AgentDesignSessionLimitExceeded("request-limit"), 429),
        (AgentDesignSlugConflict("request-slug"), 409),
        (AgentDesignConflictUnresolved("request-conflict"), 409),
    ],
)
def test_agent_builder_specific_errors_keep_stable_public_codes(
    error: SharedAssetError,
    status_code: int,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        raise_asset_domain(error)

    assert exc_info.value.status_code == status_code
    assert exc_info.value.detail == {
        "code": error.code,
        "message": error.public_message,
        "request_id": error.request_id,
    }
