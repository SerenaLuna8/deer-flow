"""Real-PostgreSQL regression coverage for Agent Builder clarification turns."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from support.private_thread_seed import PrivateThreadSeed, seed_private_thread_database

from app.projects.context import ProjectContext
from app.shared_assets.agent_design_generation import (
    AgentDesignDraft,
    AgentDesignGenerationContext,
    AgentDesignGenerationRequest,
    CandidateResult,
    ClarificationQuestion,
    NeedsClarificationResult,
)
from app.shared_assets.agent_design_repository import AgentDesignRepository
from app.shared_assets.agent_design_service import (
    MAX_INCOMPLETE_AGENT_DESIGN_SESSIONS_PER_OWNER_PROJECT,
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
from app.shared_assets.agent_service import AgentService, CreateAgent
from app.shared_assets.errors import (
    AgentDesignSessionLimitExceeded,
    AgentDesignSlugConflict,
    AssetConflict,
)
from app.shared_assets.models import AgentPayload
from deerflow.persistence.shared_assets import (
    AgentDesignOperationRow,
    AgentDesignSessionRow,
    AgentRow,
)


class _QuestionGenerator:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        request: AgentDesignGenerationRequest,
        *,
        context: AgentDesignGenerationContext,
        model_ref: str | None = None,
    ) -> NeedsClarificationResult:
        del request, context, model_ref
        self.calls += 1
        question_id = "scope" if self.calls == 1 else "priority"
        return NeedsClarificationResult(
            questions=(
                ClarificationQuestion(
                    id=question_id,
                    targets=("agents_instructions",),
                    prompt=("主要职责范围是什么？" if self.calls == 1 else "测试工作的优先级是什么？"),
                    reason="补齐 Agent 设计信息",
                    kind="single_select",
                    required=True,
                    options=("测试设计、执行与报告", "仅执行自动化测试"),
                ),
            )
        )


class _PausingQuestionGenerator:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.resume = asyncio.Event()

    async def generate(
        self,
        request: AgentDesignGenerationRequest,
        *,
        context: AgentDesignGenerationContext,
        model_ref: str | None = None,
    ) -> NeedsClarificationResult:
        del request, context, model_ref
        self.started.set()
        await self.resume.wait()
        return NeedsClarificationResult(
            questions=(
                ClarificationQuestion(
                    id="scope",
                    targets=("agents_instructions",),
                    prompt="主要职责范围是什么？",
                    reason="补齐 Agent 设计信息",
                    kind="single_select",
                    required=True,
                    options=("测试设计、执行与报告", "仅执行自动化测试"),
                ),
            )
        )


class _FailingGenerator:
    async def generate(
        self,
        request: AgentDesignGenerationRequest,
        *,
        context: AgentDesignGenerationContext,
        model_ref: str | None = None,
    ) -> NeedsClarificationResult:
        del request, context, model_ref
        raise RuntimeError("synthetic generation failure")


class _CandidateGenerator:
    async def generate(
        self,
        request: AgentDesignGenerationRequest,
        *,
        context: AgentDesignGenerationContext,
        model_ref: str | None = None,
    ) -> CandidateResult:
        del request, context, model_ref
        return CandidateResult(
            description="审查代码并输出可验证的问题清单。",
            documents=AgentDesignDraft(
                agents_instructions="读取代码，定位问题，并给出证据。",
                soul="严谨、直接、可验证。",
                identity="代码审查 Agent。",
                user_context="使用中文，按风险排序。",
            ),
            changed_fields=(
                "agents_instructions",
                "soul",
                "identity",
                "user_context",
            ),
        )


class _AllowCatalogValidator:
    async def validate(self, *args, **kwargs) -> None:
        del args, kwargs


class _PauseAfterUnlockedGeneratingReadRepository(AgentDesignRepository):
    def __init__(
        self,
        session,
        *,
        stale_read: asyncio.Event,
        resume: asyncio.Event,
    ) -> None:
        super().__init__(session)
        self._stale_read = stale_read
        self._resume = resume

    async def get(
        self,
        context: ProjectContext,
        session_id,
        *,
        for_update: bool = False,
    ) -> AgentDesignSessionRow:
        row = await super().get(
            context,
            session_id,
            for_update=for_update,
        )
        if not for_update and row.status == AgentDesignStatus.GENERATING.value:
            self._stale_read.set()
            await self._resume.wait()
        return row


class _PauseDuringRetryPrepareRepository(AgentDesignRepository):
    def __init__(
        self,
        session,
        *,
        prepared: asyncio.Event,
        resume: asyncio.Event,
    ) -> None:
        super().__init__(session)
        self._prepared = prepared
        self._resume = resume

    async def list_allowed_assets(self, *args, **kwargs):
        self._prepared.set()
        await self._resume.wait()
        return await super().list_allowed_assets(*args, **kwargs)


class _ObserveTurnScanRepository(AgentDesignRepository):
    def __init__(
        self,
        session,
        *,
        attempted: asyncio.Event,
        scanned: asyncio.Event,
    ) -> None:
        super().__init__(session)
        self._attempted = attempted
        self._scanned = scanned

    async def lock_in_progress_turn_operations(self, *args, **kwargs):
        self._attempted.set()
        operations = await super().lock_in_progress_turn_operations(
            *args,
            **kwargs,
        )
        self._scanned.set()
        return operations


class _PauseFirstLimitCountRepository(AgentDesignRepository):
    def __init__(
        self,
        session,
        *,
        first_counted: asyncio.Event,
        second_counted: asyncio.Event,
        release_first: asyncio.Event,
    ) -> None:
        super().__init__(session)
        self._first_counted = first_counted
        self._second_counted = second_counted
        self._release_first = release_first

    async def count_incomplete(self, context: ProjectContext) -> int:
        count = await super().count_incomplete(context)
        if count == MAX_INCOMPLETE_AGENT_DESIGN_SESSIONS_PER_OWNER_PROJECT - 1:
            if not self._first_counted.is_set():
                self._first_counted.set()
                await self._release_first.wait()
            else:
                self._second_counted.set()
        return count


def _project_context(seed: PrivateThreadSeed) -> ProjectContext:
    source = seed.owner_a
    return ProjectContext(
        user_id=source.user_id,
        project_id=source.project_id,
        membership_id=source.membership_id,
        role=source.role,
        capabilities=source.capabilities,
        membership_version=source.membership_version,
        request_id="agent-builder-clarification-postgres",
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_first_clarification_answer_advances_to_the_next_question(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    generator = _QuestionGenerator()
    service = AgentDesignService(
        seed.factory,
        generator=generator,  # type: ignore[arg-type]
    )
    context = _project_context(seed)
    try:
        created = await service.create(
            context,
            CreateAgentDesignSession(
                slug="browser-test-agent",
                display_name="Browser Test Agent",
                idempotency_key="create-browser-test-agent",
            ),
        )
        first = await service.submit_turn(
            context,
            created.id,
            SubmitAgentDesignTurn(
                input=AgentDesignMessageTurn(
                    kind="message",
                    message="设计一个负责浏览器测试的 Agent",
                ),
                expected_revision=created.revision,
                idempotency_key="initial-design-turn",
            ),
        )
        question = first.active_clarification
        assert first.status is AgentDesignStatus.AWAITING_CLARIFICATION
        assert question is not None
        selected = question.options[0]

        second = await service.submit_turn(
            context,
            created.id,
            SubmitAgentDesignTurn(
                input=AgentDesignClarificationTurn(
                    kind="clarification",
                    response=AgentDesignClarificationResponse(
                        version=1,
                        kind="human_input_response",
                        source=question.source,
                        request_id=question.request_id,
                        response_kind="option",
                        option_id=selected.id,
                        value=selected.value,
                    ),
                ),
                expected_revision=first.revision,
                idempotency_key="first-clarification-turn",
            ),
        )

        assert second.status is AgentDesignStatus.AWAITING_CLARIFICATION
        assert second.active_clarification is not None
        assert second.active_clarification.request_id == "priority"
        assert generator.calls == 2
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_concurrent_create_cannot_exceed_incomplete_session_limit(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    context = _project_context(seed)
    seed_service = AgentDesignService(seed.factory)
    first_counted = asyncio.Event()
    second_counted = asyncio.Event()
    release_first = asyncio.Event()
    service = AgentDesignService(
        seed.factory,
        repository_factory=lambda session: _PauseFirstLimitCountRepository(
            session,
            first_counted=first_counted,
            second_counted=second_counted,
            release_first=release_first,
        ),
    )
    tasks: list[asyncio.Task] = []
    try:
        for index in range(MAX_INCOMPLETE_AGENT_DESIGN_SESSIONS_PER_OWNER_PROJECT - 1):
            await seed_service.create(
                context,
                CreateAgentDesignSession(
                    slug=f"limit-seed-{index}",
                    display_name=f"Limit seed {index}",
                    idempotency_key=f"create-limit-seed-{index}",
                ),
            )

        tasks.append(
            asyncio.create_task(
                service.create(
                    context,
                    CreateAgentDesignSession(
                        slug="limit-candidate-eight",
                        display_name="Limit candidate eight",
                        idempotency_key="create-limit-candidate-eight",
                    ),
                )
            )
        )
        await asyncio.wait_for(first_counted.wait(), timeout=5)
        tasks.append(
            asyncio.create_task(
                service.create(
                    context,
                    CreateAgentDesignSession(
                        slug="limit-candidate-nine",
                        display_name="Limit candidate nine",
                        idempotency_key="create-limit-candidate-nine",
                    ),
                )
            )
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(second_counted.wait(), timeout=0.25)

        release_first.set()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=5,
        )

        assert sum(not isinstance(result, BaseException) for result in results) == 1
        assert sum(isinstance(result, AgentDesignSessionLimitExceeded) for result in results) == 1
        async with seed.factory() as session:
            count = await AgentDesignRepository(session).count_incomplete(context)
        assert count == MAX_INCOMPLETE_AGENT_DESIGN_SESSIONS_PER_OWNER_PROJECT
    finally:
        release_first.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_stale_get_refreshes_locked_session_after_generation_settles(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    generator = _PausingQuestionGenerator()
    context = _project_context(seed)
    submit_service = AgentDesignService(
        seed.factory,
        generator=generator,  # type: ignore[arg-type]
    )
    stale_read = asyncio.Event()
    resume_get = asyncio.Event()
    get_service = AgentDesignService(
        seed.factory,
        repository_factory=lambda session: _PauseAfterUnlockedGeneratingReadRepository(
            session,
            stale_read=stale_read,
            resume=resume_get,
        ),
        stale_generating_seconds=1,
    )
    get_service._now = lambda: datetime.now(UTC) + timedelta(minutes=10)  # type: ignore[method-assign]  # noqa: SLF001
    submit_task: asyncio.Task | None = None
    get_task: asyncio.Task | None = None
    try:
        created = await submit_service.create(
            context,
            CreateAgentDesignSession(
                slug="stale-settle-race",
                display_name="Stale settle race",
                idempotency_key="create-stale-settle-race",
            ),
        )
        submit_task = asyncio.create_task(
            submit_service.submit_turn(
                context,
                created.id,
                SubmitAgentDesignTurn(
                    input=AgentDesignMessageTurn(
                        kind="message",
                        message="设计一个负责浏览器测试的 Agent",
                    ),
                    expected_revision=created.revision,
                    idempotency_key="stale-settle-turn",
                ),
            )
        )
        await asyncio.wait_for(generator.started.wait(), timeout=5)

        get_task = asyncio.create_task(get_service.get(context, created.id))
        await asyncio.wait_for(stale_read.wait(), timeout=5)

        generator.resume.set()
        settled = await asyncio.wait_for(submit_task, timeout=5)
        assert settled.status is AgentDesignStatus.AWAITING_CLARIFICATION
        resume_get.set()
        observed = await asyncio.wait_for(get_task, timeout=5)

        async with seed.factory() as session:
            persisted = await session.get(AgentDesignSessionRow, created.id)
            operation_status = await session.scalar(
                select(AgentDesignOperationRow.status).where(
                    AgentDesignOperationRow.session_id == created.id,
                    AgentDesignOperationRow.operation_kind == "turn",
                )
            )
        assert persisted is not None
        assert persisted.status == AgentDesignStatus.AWAITING_CLARIFICATION.value
        assert operation_status == "completed"
        assert observed.status is AgentDesignStatus.AWAITING_CLARIFICATION
    finally:
        generator.resume.set()
        resume_get.set()
        for task in (submit_task, get_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (submit_task, get_task) if task is not None),
            return_exceptions=True,
        )
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_incomplete_cursor_does_not_skip_a_session_recovered_between_pages(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    generator = _PausingQuestionGenerator()
    context = _project_context(seed)
    generation_service = AgentDesignService(
        seed.factory,
        generator=generator,  # type: ignore[arg-type]
    )
    list_service = AgentDesignService(seed.factory)
    recovery_service = AgentDesignService(
        seed.factory,
        stale_generating_seconds=1,
    )
    recovery_service._now = lambda: datetime.now(UTC) + timedelta(minutes=10)  # type: ignore[method-assign]  # noqa: SLF001
    submit_task: asyncio.Task | None = None
    try:
        older = await generation_service.create(
            context,
            CreateAgentDesignSession(
                slug="cursor-recovery-older",
                display_name="Cursor recovery older",
                idempotency_key="create-cursor-recovery-older",
            ),
        )
        submit_task = asyncio.create_task(
            generation_service.submit_turn(
                context,
                older.id,
                SubmitAgentDesignTurn(
                    input=AgentDesignMessageTurn(
                        kind="message",
                        message="设计一个负责浏览器测试的 Agent",
                    ),
                    expected_revision=older.revision,
                    idempotency_key="cursor-recovery-turn",
                ),
            )
        )
        await asyncio.wait_for(generator.started.wait(), timeout=5)
        newer = await generation_service.create(
            context,
            CreateAgentDesignSession(
                slug="cursor-recovery-newer",
                display_name="Cursor recovery newer",
                idempotency_key="create-cursor-recovery-newer",
            ),
        )

        first_page = await list_service.list_incomplete(context, limit=1)
        assert [item.id for item in first_page.items] == [newer.id]
        assert first_page.next_cursor is not None

        recovered = await recovery_service.get(context, older.id)
        assert recovered.status is AgentDesignStatus.FAILED

        second_page = await list_service.list_incomplete(
            context,
            limit=10,
            cursor=first_page.next_cursor,
        )
        assert older.id in {item.id for item in second_page.items}
    finally:
        generator.resume.set()
        if submit_task is not None and not submit_task.done():
            submit_task.cancel()
        if submit_task is not None:
            await asyncio.gather(submit_task, return_exceptions=True)
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_commit_recovers_from_original_slug_conflict_with_an_override(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    context = _project_context(seed)
    agent_service = AgentService(
        seed.factory,
        catalog_validator=_AllowCatalogValidator(),  # type: ignore[arg-type]
    )
    service = AgentDesignService(
        seed.factory,
        generator=_CandidateGenerator(),  # type: ignore[arg-type]
        agent_service=agent_service,
    )
    try:
        created = await service.create(
            context,
            CreateAgentDesignSession(
                slug="builder-slug-conflict",
                display_name="Builder slug conflict",
                idempotency_key="create-builder-slug-conflict",
            ),
        )
        proposal = await service.submit_turn(
            context,
            created.id,
            SubmitAgentDesignTurn(
                input=AgentDesignMessageTurn(
                    kind="message",
                    message="设计一个代码审查 Agent",
                ),
                expected_revision=created.revision,
                idempotency_key="builder-slug-conflict-turn",
            ),
        )
        assert proposal.status is AgentDesignStatus.PROPOSAL_READY
        assert proposal.blueprint_checksum is not None

        await agent_service.create_project(
            context,
            CreateAgent(
                slug="builder-slug-conflict",
                display_name="Conflicting Agent",
            ),
            AgentPayload(
                description="占用原始 slug。",
                soul="保持简洁。",
                model_ref="default",
                tool_groups=(),
                skill_refs=(),
                mcp_version_ids=(),
            ),
        )

        with pytest.raises(AgentDesignSlugConflict):
            await service.commit(
                context,
                created.id,
                CommitAgentDesignSession(
                    expected_revision=proposal.revision,
                    expected_blueprint_checksum=proposal.blueprint_checksum,
                    idempotency_key="commit-conflicting-original-slug",
                ),
            )

        committed = await service.commit(
            context,
            created.id,
            CommitAgentDesignSession(
                expected_revision=proposal.revision,
                expected_blueprint_checksum=proposal.blueprint_checksum,
                idempotency_key="commit-recovered-slug",
                slug="builder-slug-recovered",
            ),
        )

        assert committed.session.status is AgentDesignStatus.COMPLETED
        assert committed.session.slug == "builder-slug-recovered"
        assert committed.agent.slug == "builder-slug-recovered"
        assert committed.session.display_name == committed.agent.display_name == "builder-slug-recovered"
        async with seed.factory() as session:
            persisted_session = await session.get(
                AgentDesignSessionRow,
                created.id,
            )
            persisted_agent = await session.get(
                AgentRow,
                committed.agent.id,
            )
        assert persisted_session is not None
        assert persisted_agent is not None
        assert persisted_session.slug == persisted_agent.slug == "builder-slug-recovered"
        assert persisted_session.display_name == persisted_agent.display_name
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_cancel_fences_failed_turn_retry_before_terminalizing_generation(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    context = _project_context(seed)
    failing_service = AgentDesignService(
        seed.factory,
        generator=_FailingGenerator(),  # type: ignore[arg-type]
    )
    retry_generator = _PausingQuestionGenerator()
    retry_prepared = asyncio.Event()
    resume_retry_prepare = asyncio.Event()
    retry_service = AgentDesignService(
        seed.factory,
        generator=retry_generator,  # type: ignore[arg-type]
        repository_factory=lambda session: _PauseDuringRetryPrepareRepository(
            session,
            prepared=retry_prepared,
            resume=resume_retry_prepare,
        ),
    )
    cancel_attempted = asyncio.Event()
    cancel_scanned = asyncio.Event()
    cancel_service = AgentDesignService(
        seed.factory,
        repository_factory=lambda session: _ObserveTurnScanRepository(
            session,
            attempted=cancel_attempted,
            scanned=cancel_scanned,
        ),
    )
    retry_task: asyncio.Task | None = None
    cancel_task: asyncio.Task | None = None
    try:
        created = await failing_service.create(
            context,
            CreateAgentDesignSession(
                slug="cancel-retry-race",
                display_name="Cancel retry race",
                idempotency_key="create-cancel-retry-race",
            ),
        )
        command = SubmitAgentDesignTurn(
            input=AgentDesignMessageTurn(
                kind="message",
                message="设计一个负责浏览器测试的 Agent",
            ),
            expected_revision=created.revision,
            idempotency_key="cancel-retry-turn",
        )
        failed = await failing_service.submit_turn(
            context,
            created.id,
            command,
        )
        assert failed.status is AgentDesignStatus.FAILED

        retry_task = asyncio.create_task(retry_service.submit_turn(context, created.id, command))
        await asyncio.wait_for(retry_prepared.wait(), timeout=5)
        cancel_task = asyncio.create_task(
            cancel_service.cancel(
                context,
                created.id,
                CancelAgentDesignSession(
                    expected_revision=failed.revision + 1,
                    idempotency_key="cancel-during-retry",
                ),
            )
        )
        await asyncio.wait_for(cancel_attempted.wait(), timeout=5)
        await asyncio.sleep(0)
        assert not cancel_scanned.is_set()

        resume_retry_prepare.set()
        await asyncio.wait_for(retry_generator.started.wait(), timeout=5)
        await asyncio.wait_for(cancel_scanned.wait(), timeout=5)
        cancelled = await asyncio.wait_for(cancel_task, timeout=5)
        assert cancelled.status is AgentDesignStatus.CANCELLED

        retry_generator.resume.set()
        with pytest.raises(AssetConflict):
            await asyncio.wait_for(retry_task, timeout=5)

        async with seed.factory() as session:
            operation = await session.scalar(
                select(AgentDesignOperationRow).where(
                    AgentDesignOperationRow.session_id == created.id,
                    AgentDesignOperationRow.operation_kind == "turn",
                )
            )
        assert operation is not None
        assert operation.status == "failed"
        assert operation.public_error_code == "AGENT_DESIGN_SESSION_CANCELLED"
    finally:
        resume_retry_prepare.set()
        retry_generator.resume.set()
        for task in (retry_task, cancel_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (retry_task, cancel_task) if task is not None),
            return_exceptions=True,
        )
        await seed.engine.dispose()
