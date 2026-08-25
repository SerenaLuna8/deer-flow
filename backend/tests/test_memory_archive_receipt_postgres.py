from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import create_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Overwrite
from sqlalchemy import func, select
from support.private_thread_seed import PrivateThreadSeed, seed_private_thread_database
from support.system_model_seed import (
    frozen_system_model_execution,
    seed_system_model_config,
)

import deerflow.runtime.checkpoint_mode as checkpoint_mode_state
from app.gateway.deps import (
    get_current_agent_runtime_config,
    private_work_context,
    require_project_private_open,
)
from app.gateway.routers import private_work as private_work_router
from app.personalization.repository import AccountPersonalizationRepository
from app.private_work.chat_controls import ProjectChatControlService
from app.private_work.checkpoint_state import (
    bind_scoped_checkpoint_state,
    checkpoint_config,
    snapshot_checkpoint_id,
)
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.errors import PrivateWorkUnavailable
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.private_work.thread_service import PrivateThreadService
from app.reliability.execution import PrivateRunExecutionBoundary
from app.worker.memory_dream_prepare import MemoryDreamPrepareJobHandler, _PrepareWork
from app.worker.service import JobSettlement
from deerflow.agents.memory.snip import (
    MEMORY_ARCHIVE_CONTEXT_KEY,
    MEMORY_ARCHIVE_RECEIPT_KEY,
    SNIP_ARCHIVE_PROMPT,
    SnipArchiveContext,
    build_memory_archive_receipt,
)
from deerflow.agents.middlewares.summarization_middleware import (
    DeerFlowSummarizationMiddleware,
)
from deerflow.agents.thread_state import get_thread_state_schema
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.persistence.jobs.model import WorkerNodeRow
from deerflow.persistence.jobs.sql import JobClaim, JobRepository, JobScope
from deerflow.persistence.models.run_event import RunEventRow, ThreadEventSequenceRow
from deerflow.persistence.private_work.memory_document_model import (
    MemoryHistoryEntryRow,
)
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentRepository,
    MemoryDocumentScope,
    MemoryDreamAdmissionRecord,
)
from deerflow.persistence.user.model import UserRow
from deerflow.persistence.user.private_lifecycle import AccountPrivateGeneration
from deerflow.runtime.checkpoint_state import (
    CheckpointStateAccessor,
    build_state_mutation_graph,
)
from deerflow.runtime.events.models import StreamFrame, StreamLeaseProof
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.runtime.events.stream import PostgresStreamBridge


class _CountingInMemorySaver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.alist_calls = 0
        self.fail_next_put = False

    async def alist(self, *args, **kwargs):
        self.alist_calls += 1
        async for item in super().alist(*args, **kwargs):
            yield item

    async def aput(self, *args, **kwargs):
        if self.fail_next_put:
            self.fail_next_put = False
            raise RuntimeError("injected raw checkpoint failure")
        return await super().aput(*args, **kwargs)


def _app_config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
            },
            "database": {"checkpoint_channel_mode": "full"},
        }
    )


def _reset_checkpoint_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        checkpoint_mode_state,
        "_frozen_checkpoint_channel_mode",
        None,
    )
    monkeypatch.setattr(
        checkpoint_mode_state,
        "_frozen_checkpoint_snapshot_frequency",
        None,
    )


async def _seed_model_config(seed: PrivateThreadSeed) -> uuid.UUID:
    model_id = uuid.uuid4()
    async with seed.engine.begin() as connection:
        await seed_system_model_config(
            connection,
            model_id=model_id,
            owner_user_id=str(seed.owner_a.user_id),
            display_name="SNIP test model",
            provider_model="snip-test",
        )
    return model_id


def _model_provenance(model_id: uuid.UUID):
    return frozen_system_model_execution(
        model_id=model_id,
        provider_model="snip-test",
    ).provenance


async def _source_snapshot(
    seed: PrivateThreadSeed,
    scoped: ProjectScopedCheckpointer,
    app_config: AppConfig,
    thread_id: str,
):
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
    state = bind_scoped_checkpoint_state(
        scoped,
        seed.owner_a,
        app_config,
        as_node="memory_receipt_test",
    )
    await state.aupdate(
        checkpoint_config(thread_id),
        {
            "messages": [
                HumanMessage(id="human-1", content="Keep the receipt atomic"),
                AIMessage(id="ai-1", content="The checkpoint is the commit point"),
            ]
        },
        as_node="memory_receipt_test",
    )
    snapshot = await state.aget(checkpoint_config(thread_id))
    assert snapshot_checkpoint_id(snapshot) is not None
    return state, snapshot


def _receipt(seed: PrivateThreadSeed, snapshot, model_config_id: uuid.UUID):
    source_checkpoint_id = snapshot_checkpoint_id(snapshot)
    assert source_checkpoint_id is not None
    messages = snapshot.values["messages"]
    tagged_text = "- [durable] A committed checkpoint receipt activates history exactly once"
    receipt = build_memory_archive_receipt(
        SnipArchiveContext(
            enabled=True,
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            namespace="default",
            preference_version=1,
            summary_model=_model_provenance(model_config_id),
            source_checkpoint_id=source_checkpoint_id,
        ),
        thread_id=snapshot.config["configurable"]["thread_id"],
        source_checkpoint_id=source_checkpoint_id,
        previous_summary=None,
        messages=messages,
        tagged_text=tagged_text,
    )
    assert receipt is not None
    return tagged_text, receipt


async def _history_rows(seed: PrivateThreadSeed) -> tuple[MemoryHistoryEntryRow, ...]:
    async with seed.factory() as session:
        return tuple((await session.execute(select(MemoryHistoryEntryRow).order_by(MemoryHistoryEntryRow.sequence))).scalars())


@pytest.mark.postgres
@pytest.mark.anyio
async def test_checkpoint_aput_activates_once_and_branch_or_client_cannot_copy_receipt(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_checkpoint_mode(monkeypatch)
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    raw = _CountingInMemorySaver()
    scoped = ProjectScopedCheckpointer(raw, seed.factory)
    app_config = _app_config()
    source_thread_id = f"receipt-source-{uuid.uuid4()}"
    target_thread_id = f"receipt-target-{uuid.uuid4()}"
    try:
        model_config_id = await _seed_model_config(seed)
        state, source = await _source_snapshot(
            seed,
            scoped,
            app_config,
            source_thread_id,
        )
        tagged_text, receipt = _receipt(seed, source, model_config_id)
        committed_config = await state.aupdate(
            source.config,
            {
                "messages": Overwrite([]),
                "summary_text": tagged_text,
                MEMORY_ARCHIVE_RECEIPT_KEY: receipt,
            },
            as_node="memory_receipt_test",
        )
        committed_id = committed_config["configurable"]["checkpoint_id"]

        saver = scoped.for_context(seed.owner_a)
        await saver.aget_tuple(checkpoint_config(source_thread_id))
        await saver.aget_tuple(checkpoint_config(source_thread_id))
        rows = await _history_rows(seed)
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].tagged_text == tagged_text
        assert rows[0].committed_checkpoint_id == committed_id

        await PrivateThreadService(seed.factory, scoped).branch(
            seed.owner_a,
            source_thread_id=source_thread_id,
            target_thread_id=target_thread_id,
            checkpoint_id=committed_id,
            replay_base_checkpoint_id=source.config["configurable"]["checkpoint_id"],
            expected_source_version=1,
            app_config=app_config,
        )
        target = await bind_scoped_checkpoint_state(
            scoped,
            seed.owner_a,
            app_config,
            as_node="memory_receipt_target_test",
        ).aget(checkpoint_config(target_thread_id))
        assert MEMORY_ARCHIVE_RECEIPT_KEY not in target.values
        assert len(await _history_rows(seed)) == 1

        forged = {
            "forged": True,
            "project_id": str(seed.owner_a.project_id),
        }
        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            source_thread_id,
            PrivateRunCreate(
                run_id=f"forged-receipt-{uuid.uuid4()}",
                kwargs={
                    "input": {
                        MEMORY_ARCHIVE_RECEIPT_KEY: forged,
                        "messages": [
                            {
                                "type": "human",
                                "content": "ordinary message",
                                "additional_kwargs": {
                                    MEMORY_ARCHIVE_RECEIPT_KEY: "message data",
                                },
                            }
                        ],
                    }
                },
            ),
        )
        admitted_input = admitted.run.kwargs["input"]
        assert MEMORY_ARCHIVE_RECEIPT_KEY not in admitted_input
        assert admitted_input["messages"][0]["additional_kwargs"][MEMORY_ARCHIVE_RECEIPT_KEY] == "message data"
        assert len(await _history_rows(seed)) == 1
        assert raw.alist_calls == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_checkpoint_write_failure_never_creates_history(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_checkpoint_mode(monkeypatch)
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    raw = _CountingInMemorySaver()
    scoped = ProjectScopedCheckpointer(raw, seed.factory)
    app_config = _app_config()
    thread_id = f"receipt-checkpoint-failure-{uuid.uuid4()}"
    try:
        model_config_id = await _seed_model_config(seed)
        state, source = await _source_snapshot(
            seed,
            scoped,
            app_config,
            thread_id,
        )
        tagged_text, receipt = _receipt(seed, source, model_config_id)
        raw.fail_next_put = True

        with pytest.raises(PrivateWorkUnavailable):
            await state.aupdate(
                source.config,
                {
                    "messages": Overwrite([]),
                    "summary_text": tagged_text,
                    MEMORY_ARCHIVE_RECEIPT_KEY: receipt,
                },
                as_node="memory_receipt_test",
            )

        assert await _history_rows(seed) == ()
        current = await raw.aget_tuple(checkpoint_config(thread_id))
        assert current is not None
        assert MEMORY_ARCHIVE_RECEIPT_KEY not in current.checkpoint["channel_values"]
        assert raw.alist_calls == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_real_stategraph_threshold_uses_runtime_checkpoint_and_activates_history(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_checkpoint_mode(monkeypatch)
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    raw = _CountingInMemorySaver()
    scoped = ProjectScopedCheckpointer(raw, seed.factory)
    app_config = _app_config()
    thread_id = f"receipt-runtime-{uuid.uuid4()}"
    try:
        model_config_id = await _seed_model_config(seed)
        state, source = await _source_snapshot(
            seed,
            scoped,
            app_config,
            thread_id,
        )
        await state.aupdate(
            source.config,
            {
                "messages": [
                    HumanMessage(
                        id="recent-human",
                        content="Keep the immediately previous turn available",
                    ),
                    AIMessage(
                        id="recent-ai",
                        content="The recent answer remains in the active context",
                    ),
                    HumanMessage(
                        id="open-human",
                        content="This turn is still open",
                    ),
                ]
            },
            as_node="memory_receipt_test",
        )
        model = FakeListChatModel(
            responses=["<continuity>\nThe open turn continues after automatic compaction.\n</continuity>\n- [durable] Runtime execution_info binds automatic compaction to its source checkpoint"],
            custom_get_token_ids=lambda text: list(range(len(text))),
        )
        middleware = DeerFlowSummarizationMiddleware(
            model=model,
            trigger=("messages", 1),
            keep=("messages", 1),
            trim_tokens_to_summarize=20_000,
            summary_prompt=SNIP_ARCHIVE_PROMPT,
        )
        execution_checkpoint_ids: list[str] = []

        async def automatic_compaction(state, runtime: Runtime):
            assert runtime.execution_info is not None
            execution_checkpoint_ids.append(runtime.execution_info.checkpoint_id)
            return await middleware.abefore_model(state, runtime) or {}

        builder = StateGraph(
            get_thread_state_schema("full"),
            context_schema=dict,
        )
        builder.add_node("automatic_compaction", automatic_compaction)
        builder.set_entry_point("automatic_compaction")
        builder.set_finish_point("automatic_compaction")
        graph = builder.compile(checkpointer=scoped.for_context(seed.owner_a))
        archive_context = SnipArchiveContext(
            enabled=True,
            project_id=seed.owner_a.project_id,
            owner_user_id=str(seed.owner_a.user_id),
            namespace="default",
            preference_version=1,
            summary_model=_model_provenance(model_config_id),
            source_checkpoint_id=None,
        )

        await graph.ainvoke(
            {"messages": []},
            config=checkpoint_config(thread_id),
            context={
                "thread_id": thread_id,
                "user_id": str(seed.owner_a.user_id),
                MEMORY_ARCHIVE_CONTEXT_KEY: archive_context,
            },
        )

        assert len(execution_checkpoint_ids) == 1
        rows = await _history_rows(seed)
        assert len(rows) == 1
        assert rows[0].source_checkpoint_id == execution_checkpoint_ids[0]
        latest = await scoped.for_context(seed.owner_a).aget_tuple(checkpoint_config(thread_id))
        assert latest is not None
        receipt = latest.checkpoint["channel_values"][MEMORY_ARCHIVE_RECEIPT_KEY]
        assert receipt["source_checkpoint_id"] == execution_checkpoint_ids[0]
        assert rows[0].tagged_text == receipt["tagged_text"]
        assert raw.alist_calls == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_worker_receipt_activation_does_not_deadlock_durable_stream_append(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep Worker checkpoint and durable-stream transactions on one lock order."""

    _reset_checkpoint_mode(monkeypatch)
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    raw = _CountingInMemorySaver()
    scoped = ProjectScopedCheckpointer(raw, seed.factory)
    app_config = _app_config()
    thread_id = f"receipt-stream-{uuid.uuid4()}"
    run_id = f"receipt-stream-run-{uuid.uuid4()}"
    try:
        model_config_id = await _seed_model_config(seed)
        _, source = await _source_snapshot(
            seed,
            scoped,
            app_config,
            thread_id,
        )
        tagged_text, receipt = _receipt(seed, source, model_config_id)

        admitted = await PrivateRunAdmissionService(seed.factory).admit(
            seed.owner_a,
            thread_id,
            PrivateRunCreate(
                run_id=run_id,
                kwargs={
                    "input": {
                        "messages": [
                            {
                                "type": "human",
                                "content": "Trigger the first automatic SNIP compaction",
                            }
                        ]
                    }
                },
            ),
        )
        worker_id = uuid.uuid4()
        async with seed.factory() as session, session.begin():
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="receipt-stream-test",
                    capabilities_json=["private_run"],
                    max_concurrent_jobs=1,
                )
            )
            session.add(
                ThreadEventSequenceRow(
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    thread_id=thread_id,
                    high_watermark=0,
                )
            )

        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"private_run"}),
                lease_seconds=300,
            )
            assert claim is not None
            assert claim.job_id == admitted.job.job_id
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )
            await PrivateRunRepository(session).begin_execution(
                scope=seed.owner_a_scope,
                run_id=run_id,
                job_id=claim.job_id,
                lease_token=claim.lease_token,
                origin_trace_id=claim.origin_trace_id,
            )

        boundary = PrivateRunExecutionBoundary(
            seed.factory,
            context=seed.owner_a,
            claim=claim,
        )
        worker_saver = scoped.for_context(seed.owner_a)
        worker_saver.set_authorization_boundary(boundary)
        worker_state = CheckpointStateAccessor.bind(
            build_state_mutation_graph(
                "memory_receipt_stream_test",
                "full",
            ),
            worker_saver,
            mode="full",
        )

        receipt_user_locked = asyncio.Event()
        release_receipt = asyncio.Event()
        stream_governance_locked = asyncio.Event()
        original_activate = MemoryDocumentRepository.activate_history
        original_authorize_stream_lease = DbRunEventStore._authorize_stream_lease

        async def gated_activate_history(self, activation):
            await self.session.execute(select(UserRow.id).where(UserRow.id == activation.scope.owner_user_id).with_for_update(of=UserRow))
            receipt_user_locked.set()
            await asyncio.wait_for(release_receipt.wait(), timeout=10)
            return await original_activate(self, activation)

        async def record_authorized_stream_locks(session, **kwargs):
            cancelled = await original_authorize_stream_lease(
                session,
                **kwargs,
            )
            stream_governance_locked.set()
            return cancelled

        monkeypatch.setattr(
            MemoryDocumentRepository,
            "activate_history",
            gated_activate_history,
        )
        monkeypatch.setattr(
            DbRunEventStore,
            "_authorize_stream_lease",
            staticmethod(record_authorized_stream_locks),
        )

        receipt_task = asyncio.create_task(
            worker_state.aupdate(
                source.config,
                {
                    "messages": Overwrite([]),
                    "summary_text": tagged_text,
                    MEMORY_ARCHIVE_RECEIPT_KEY: receipt,
                },
                as_node="memory_receipt_stream_test",
            )
        )
        stream_task: asyncio.Task | None = None
        try:
            await asyncio.wait_for(receipt_user_locked.wait(), timeout=10)
            stream_task = asyncio.create_task(
                PostgresStreamBridge(seed.factory).publish_frame(
                    seed.owner_a_scope,
                    thread_id,
                    run_id,
                    StreamFrame(
                        event="values",
                        data={"phase": "after-first-snip"},
                    ),
                    lease=StreamLeaseProof(
                        job_id=claim.job_id,
                        lease_token=claim.lease_token,
                    ),
                )
            )

            # Before the lock-order fix the stream transaction acquires
            # Project/Membership while the receipt owns User, so this event is
            # reached and releasing the receipt creates the production
            # deadlock. With the fixed Project -> Membership -> Thread -> User
            # order, the stream waits here until the receipt commits.
            try:
                await asyncio.wait_for(
                    stream_governance_locked.wait(),
                    timeout=2,
                )
            except TimeoutError:
                pass
            release_receipt.set()
            committed_config, stored_frame = await asyncio.wait_for(
                asyncio.gather(receipt_task, stream_task),
                timeout=10,
            )
        finally:
            release_receipt.set()
            pending = tuple(task for task in (receipt_task, stream_task) if task is not None and not task.done())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        assert committed_config["configurable"]["checkpoint_id"]
        assert stored_frame.created is True
        assert stream_governance_locked.is_set()
        rows = await _history_rows(seed)
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].tagged_text == tagged_text
        async with seed.factory() as session:
            stream_rows = tuple(
                (
                    await session.execute(
                        select(RunEventRow).where(
                            RunEventRow.project_id == seed.owner_a.project_id,
                            RunEventRow.owner_user_id == str(seed.owner_a.user_id),
                            RunEventRow.thread_id == thread_id,
                            RunEventRow.run_id == run_id,
                            RunEventRow.category == "stream",
                            RunEventRow.event_type == "values",
                        )
                    )
                ).scalars()
            )
        assert len(stream_rows) == 1
        assert str(stream_rows[0].seq) == stored_frame.id == "1"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_private_compact_endpoint_drains_long_thread_in_multiple_tagged_batches(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real HTTP/service/checkpoint/history path over several SNIPs."""

    _reset_checkpoint_mode(monkeypatch)
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    raw = _CountingInMemorySaver()
    scoped = ProjectScopedCheckpointer(raw, seed.factory)
    app_config = _app_config()
    thread_id = str(uuid.uuid4())
    continuity = "The long thread remains coherent across every compaction batch."
    tagged_text = "- [durable] Manual compact archives each bounded batch into tagged history"
    try:
        model_config_id = await _seed_model_config(seed)
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        state = bind_scoped_checkpoint_state(
            scoped,
            seed.owner_a,
            app_config,
            as_node="long_compact_test",
        )
        messages = []
        for turn in range(8):
            messages.extend(
                [
                    HumanMessage(
                        id=f"human-{turn}",
                        content=f"User turn {turn}: " + ("u" * 320),
                    ),
                    AIMessage(
                        id=f"ai-{turn}",
                        content=f"Assistant turn {turn}: " + ("a" * 320),
                    ),
                ]
            )
        await state.aupdate(
            checkpoint_config(thread_id),
            {"messages": messages},
            as_node="long_compact_test",
        )

        runtime_model = ModelConfig(
            name="snip-long-compact-model",
            display_name=None,
            description=None,
            use="support.fake_models:GovernedFakeListChatModel",
            model="snip-long-compact-model",
            max_input_tokens=64_000,
            responses=[
                f"<continuity>\n{continuity}\n</continuity>\n{tagged_text}",
            ],
            # Keep the fake model's deterministic tokenizer; the 1,100-token
            # rendered-prompt ceiling admits at most two of these large turns
            # per batch instead of allowing one oversized archive.
            custom_get_token_ids=lambda text: list(range(len(text))),
        )
        runtime_model._system_model_config_id = model_config_id
        runtime_model._system_model_payload_checksum = _model_provenance(model_config_id).payload_checksum
        app_config.summarization.enabled = True
        app_config.summarization.model_name = runtime_model.name
        app_config.summarization.trim_tokens_to_summarize = 1_100

        class ModelMaterializer:
            async def materialize_active(self, model_ref):
                assert model_ref == runtime_model.name
                return runtime_model

        barrier = ProjectChatControlService(
            seed.factory,
            scoped,
            PrivateThreadService(seed.factory, scoped),
            object(),  # type: ignore[arg-type]
            model_materializer=ModelMaterializer(),  # type: ignore[arg-type]
        )
        app = FastAPI()
        app.include_router(private_work_router.router)
        app.dependency_overrides[private_work_context] = lambda: seed.owner_a
        app.dependency_overrides[require_project_private_open] = lambda: None
        app.dependency_overrides[get_current_agent_runtime_config] = lambda: app_config
        monkeypatch.setattr(
            private_work_router,
            "_chat_control_service",
            lambda _request, _request_id: barrier,
        )

        compacted_batches: list[dict[str, object]] = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            for _attempt in range(9):
                response = await client.post(
                    f"/api/projects/{seed.owner_a.project_id}/private-work/threads/{thread_id}/compact",
                    json={
                        "force": True,
                        "keep": {"type": "messages", "value": 0},
                    },
                )
                assert response.status_code == 200, response.text
                payload = response.json()
                if not payload["compacted"]:
                    assert payload["reason"] == "not_enough_messages"
                    break
                compacted_batches.append(payload)
            else:
                pytest.fail("long /compact did not drain within its bounded attempts")

        assert len(compacted_batches) >= 2
        assert sum(int(batch["removed_message_count"]) for batch in compacted_batches) == len(messages)
        assert all(int(batch["preserved_message_count"]) < len(messages) for batch in compacted_batches)
        rows = await _history_rows(seed)
        assert len(rows) == len(compacted_batches)
        assert all(row.status == "pending" for row in rows)
        assert all(row.tagged_text == tagged_text for row in rows)
        assert len({row.source_digest for row in rows}) == len(rows)

        latest = await bind_scoped_checkpoint_state(
            scoped,
            seed.owner_a,
            app_config,
            as_node="long_compact_test",
        ).aget(checkpoint_config(thread_id))
        assert latest.values["messages"] == []
        assert latest.values["summary_text"] == continuity
        assert "[durable]" not in latest.values["summary_text"]
        assert raw.alist_calls == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_durable_dream_prepare_worker_drains_real_thread_and_activates_before_admission(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_checkpoint_mode(monkeypatch)
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    raw = _CountingInMemorySaver()
    scoped = ProjectScopedCheckpointer(raw, seed.factory)
    app_config = _app_config()
    thread_id = f"dream-barrier-{uuid.uuid4()}"
    try:
        model_config_id = await _seed_model_config(seed)
        await _source_snapshot(
            seed,
            scoped,
            app_config,
            thread_id,
        )
        runtime_model = ModelConfig(
            name="snip-barrier-model",
            display_name=None,
            description=None,
            use="support.fake_models:GovernedFakeListChatModel",
            model="snip-barrier-model",
            max_input_tokens=64_000,
            responses=["<continuity>\nThe thread is drained ahead of the Dream admission barrier.\n</continuity>\n- [durable] Direct Dream requests archive the Thread before admission"],
            custom_get_token_ids=lambda text: list(range(len(text))),
        )
        runtime_model._system_model_config_id = model_config_id
        runtime_model._system_model_payload_checksum = _model_provenance(model_config_id).payload_checksum
        app_config.summarization.enabled = True
        app_config.summarization.model_name = runtime_model.name
        app_config.summarization.trim_tokens_to_summarize = 20_000

        class ModelMaterializer:
            async def materialize_active(self, model_ref):
                assert model_ref == runtime_model.name
                return runtime_model

        class Admission:
            def __init__(self) -> None:
                self.calls = 0

            async def require_account_private_generation_after_membership(
                self,
                session,
                scope,
            ):
                assert session.in_transaction()
                assert scope.owner_user_id == str(seed.owner_a.user_id)
                return AccountPrivateGeneration(
                    owner_user_id=scope.owner_user_id,
                    generation=1,
                )

            async def admit(self, session, scope, **kwargs):
                assert session.in_transaction()
                assert len(await _history_rows(seed)) == 1
                assert kwargs["account_private_generation"] == AccountPrivateGeneration(
                    owner_user_id=scope.owner_user_id,
                    generation=1,
                )
                self.calls += 1
                return MemoryDreamAdmissionRecord(
                    disposition="queued",
                    job_id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                    history_count=1,
                )

        admission = Admission()
        thread_service = PrivateThreadService(seed.factory, scoped)
        barrier = ProjectChatControlService(
            seed.factory,
            scoped,
            thread_service,
            object(),  # type: ignore[arg-type]
            model_materializer=ModelMaterializer(),  # type: ignore[arg-type]
        )

        class PreparationRepository:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def set_phase(self, _scope, **_kwargs) -> None:
                self.calls.append("phase")

            async def record_pass(self, _scope, **_kwargs) -> None:
                self.calls.append("pass")

            async def link_dream(self, _scope, **_kwargs) -> None:
                self.calls.append("link")

            async def settle_success(self, _scope, **_kwargs) -> None:
                self.calls.append("success")

        preparation = PreparationRepository()
        handler = MemoryDreamPrepareJobHandler(
            seed.factory,
            app_config=app_config,
            barrier=barrier,
            admission=admission,  # type: ignore[arg-type]
            repository_builder=lambda _session, *, jobs: preparation,
        )
        claim = JobClaim(
            job_id=uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            attempt_id=uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            lease_token="durable-prepare-lease",
            job_type="memory_dream_prepare",
            scope=JobScope(
                seed.owner_a.project_id,
                str(seed.owner_a.user_id),
            ),
            run_id=None,
            occurrence_id=None,
            retry_safety="safe",
            cancel_requested=False,
            namespace="default",
        )
        work = _PrepareWork(
            context=seed.owner_a,
            scope=MemoryDocumentScope(
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
            ),
            thread_id=thread_id,
            request_id="durable-dream-prepare-test",
            app_config=app_config,
        )

        async def authorize(_claim):
            return work

        monkeypatch.setattr(handler, "_authorize", authorize)

        class Authority:
            cancel_requested = False

            async def heartbeat(self) -> None:
                return None

        settlement = await handler(claim, Authority())  # type: ignore[arg-type]

        assert isinstance(settlement, JobSettlement)
        assert settlement.outcome.status == "succeeded"
        assert admission.calls == 0
        await settlement.commit()
        assert admission.calls == 1
        assert preparation.calls == [
            "phase",
            "pass",
            "phase",
            "phase",
            "link",
            "success",
        ]
        rows = await _history_rows(seed)
        assert len(rows) == 1
        assert rows[0].tagged_text == ("- [durable] Direct Dream requests archive the Thread before admission")
        latest = await bind_scoped_checkpoint_state(
            scoped,
            seed.owner_a,
            app_config,
            as_node="dream_barrier_test",
        ).aget(checkpoint_config(thread_id))
        assert latest.values["messages"] == []
        assert raw.alist_calls == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.parametrize("repair_operation", ("read", "write"))
async def test_committed_receipt_repairs_after_history_transaction_loss(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    repair_operation: str,
) -> None:
    _reset_checkpoint_mode(monkeypatch)
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    raw = _CountingInMemorySaver()
    scoped = ProjectScopedCheckpointer(raw, seed.factory)
    app_config = _app_config()
    thread_id = f"receipt-repair-{repair_operation}-{uuid.uuid4()}"
    try:
        model_config_id = await _seed_model_config(seed)
        state, source = await _source_snapshot(
            seed,
            scoped,
            app_config,
            thread_id,
        )
        tagged_text, receipt = _receipt(seed, source, model_config_id)
        original_activate = MemoryDocumentRepository.activate_history
        activation_calls = 0

        async def lose_first_transaction(self, activation):
            nonlocal activation_calls
            activation_calls += 1
            result = await original_activate(self, activation)
            if activation_calls == 1:
                raise RuntimeError("injected history transaction loss")
            return result

        monkeypatch.setattr(
            MemoryDocumentRepository,
            "activate_history",
            lose_first_transaction,
        )
        with pytest.raises(PrivateWorkUnavailable):
            await state.aupdate(
                source.config,
                {
                    "messages": Overwrite([]),
                    "summary_text": tagged_text,
                    MEMORY_ARCHIVE_RECEIPT_KEY: receipt,
                },
                as_node="memory_receipt_test",
            )
        assert await _history_rows(seed) == ()

        saver = scoped.for_context(seed.owner_a)
        if repair_operation == "read":
            await saver.aget_tuple(checkpoint_config(thread_id))
        else:
            committed = await raw.aget_tuple(checkpoint_config(thread_id))
            assert committed is not None
            child = create_checkpoint(
                committed.checkpoint,
                None,
                step=int(committed.metadata.get("step", 0)) + 1,
            )
            await saver.aput(
                committed.config,
                child,
                {"source": "update", "step": 999, "parents": {}},
                {},
            )

        rows = await _history_rows(seed)
        assert len(rows) == 1
        assert rows[0].tagged_text == tagged_text
        assert activation_calls >= 2
        assert raw.alist_calls == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_old_receipt_is_stale_after_memory_reset_version_change(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_checkpoint_mode(monkeypatch)
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    raw = _CountingInMemorySaver()
    scoped = ProjectScopedCheckpointer(raw, seed.factory)
    app_config = _app_config()
    thread_id = f"receipt-stale-{uuid.uuid4()}"
    try:
        model_config_id = await _seed_model_config(seed)
        state, source = await _source_snapshot(
            seed,
            scoped,
            app_config,
            thread_id,
        )
        tagged_text, receipt = _receipt(seed, source, model_config_id)
        original_activate = MemoryDocumentRepository.activate_history
        failed = False

        async def lose_first_transaction(self, activation):
            nonlocal failed
            result = await original_activate(self, activation)
            if not failed:
                failed = True
                raise RuntimeError("injected history transaction loss")
            return result

        monkeypatch.setattr(
            MemoryDocumentRepository,
            "activate_history",
            lose_first_transaction,
        )
        with pytest.raises(PrivateWorkUnavailable):
            await state.aupdate(
                source.config,
                {
                    "messages": Overwrite([]),
                    "summary_text": tagged_text,
                    MEMORY_ARCHIVE_RECEIPT_KEY: receipt,
                },
                as_node="memory_receipt_test",
            )

        async with seed.factory() as session, session.begin():
            reset = await AccountPersonalizationRepository(session).reset_memory(
                seed.owner_a.user_id,
                expected_version=1,
                now=datetime.now(UTC),
            )
            assert reset.version == 2
        await scoped.for_context(seed.owner_a).aget_tuple(checkpoint_config(thread_id))

        assert await _history_rows(seed) == ()
        async with seed.factory() as session:
            assert int(await session.scalar(select(func.count()).select_from(MemoryHistoryEntryRow)) or 0) == 0
        assert raw.alist_calls == 0
    finally:
        await seed.engine.dispose()
