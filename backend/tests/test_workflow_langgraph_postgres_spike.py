from __future__ import annotations

import asyncio
import os
import signal
import sys
import uuid
from pathlib import Path

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from deerflow.workflows.compiler import StructuredLoopPlan, WorkflowCompilerCache
from deerflow.workflows.runtime import (
    InjectedWorkflowFault,
    OneShotWorkflowFault,
    StaleWorkflowAttemptError,
    WorkflowLoopRunner,
    WorkflowLoopRuntimeContext,
)

LOOP_ID = "00000000-0000-4000-8000-000000000102"
BODY_ID = "00000000-0000-4000-8000-000000000103"
PLAN = StructuredLoopPlan(
    graph_schema_version=1,
    compiler_contract_version=1,
    semantic_checksum="1" * 64,
    loop_node_id=LOOP_ID,
    body_node_id=BODY_ID,
    max_iterations=5,
)
FAULT_STAGES = (
    "body_before_output",
    "body_after_checkpoint",
    "commit_before_output",
    "commit_after_checkpoint",
    "route_before_output",
    "route_after_checkpoint",
)


def _checkpointer_url(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _context(*, fault: OneShotWorkflowFault | None = None) -> WorkflowLoopRuntimeContext:
    return WorkflowLoopRuntimeContext(
        body_step=lambda value, _identity: value + 1,
        until=lambda value, _iteration: value >= 3,
        fault=fault,
    )


async def _assert_no_agent_run_or_thread_alias(
    database_url: str,
    run_id: str,
) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM runs WHERE run_id = :run_id"),
                    {"run_id": run_id},
                )
                == 0
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM threads_meta WHERE thread_id = :run_id"),
                    {"run_id": run_id},
                )
                == 0
            )
    finally:
        await engine.dispose()


def _assert_atomic_commits(history: list[object]) -> None:
    checkpoint_ids: set[str] = set()
    first_checkpoint_by_activation: dict[str, str] = {}
    for snapshot in reversed(history):
        values = snapshot.values
        checkpoint_id = snapshot.config["configurable"]["checkpoint_id"]
        assert checkpoint_id not in checkpoint_ids
        checkpoint_ids.add(checkpoint_id)
        iteration = values.get("iteration", 0)
        activations = values.get("activation_ids", [])
        if iteration:
            # Loop variable, iteration counter, and journal are one node update.
            assert values["current_value"] == iteration
            assert len(activations) == iteration
        for activation in activations:
            first_checkpoint_by_activation.setdefault(activation, checkpoint_id)

    assert len(first_checkpoint_by_activation) == 3
    assert len(set(first_checkpoint_by_activation.values())) == 3
    paths = [activation.rsplit(":", 2)[-2] for activation in first_checkpoint_by_activation]
    assert paths == ["1", "2", "3"]


@pytest.mark.postgres
@pytest.mark.anyio
async def test_real_postgres_full_delta_cache_and_takeover_fault_matrix(
    migrated_postgres_database_url: str,
) -> None:
    for mode in ("full", "delta"):
        for fault_stage in FAULT_STAGES:
            run_id = str(uuid.uuid4())
            fault = OneShotWorkflowFault(stage=fault_stage)
            context = _context(fault=fault)
            first_cache = WorkflowCompilerCache()
            async with AsyncPostgresSaver.from_conn_string(_checkpointer_url(migrated_postgres_database_url)) as first_saver:
                await first_saver.setup()
                first_runner = WorkflowLoopRunner.compile(
                    PLAN,
                    checkpointer=first_saver,
                    checkpoint_mode=mode,
                    cache=first_cache,
                )
                cached_runner = WorkflowLoopRunner.compile(
                    PLAN,
                    checkpointer=first_saver,
                    checkpoint_mode=mode,
                    cache=first_cache,
                )
                assert cached_runner.template is first_runner.template
                assert cached_runner.graph is not first_runner.graph
                assert first_runner.graph.checkpointer is first_saver
                assert cached_runner.graph.checkpointer is first_saver
                assert first_cache.hits == 1

                with pytest.raises(InjectedWorkflowFault):
                    await first_runner.run(
                        run_id=run_id,
                        initial_value=0,
                        context=context,
                    )
                crashed = await first_runner.state(run_id=run_id)

                if fault_stage == "body_before_output":
                    assert crashed.get("iteration", 0) == 0
                    assert not crashed.get("pending_ready", False)
                elif fault_stage in {
                    "body_after_checkpoint",
                    "commit_before_output",
                }:
                    assert crashed.get("iteration", 0) == 0
                    assert crashed["pending_ready"] is True
                    assert crashed["pending_value"] == 1
                else:
                    assert crashed["iteration"] == 1
                    assert crashed["current_value"] == 1
                    assert len(crashed["activation_ids"]) == 1

            # A distinct saver/graph stands in for a newly claiming Worker.
            async with AsyncPostgresSaver.from_conn_string(_checkpointer_url(migrated_postgres_database_url)) as takeover_saver:
                takeover_runner = WorkflowLoopRunner.compile(
                    PLAN,
                    checkpointer=takeover_saver,
                    checkpoint_mode=mode,
                    cache=first_cache,
                )
                assert takeover_runner.template is first_runner.template
                assert takeover_runner.graph is not first_runner.graph
                assert takeover_runner.graph.checkpointer is takeover_saver
                assert first_cache.hits == 2
                result = await takeover_runner.resume(
                    run_id=run_id,
                    context=context,
                    attempt=(2 if fault_stage == "body_after_checkpoint" else None),
                )
                assert result.value == 3
                assert result.iterations == 3
                assert len(result.activation_ids) == len(set(result.activation_ids)) == 3
                if fault_stage == "body_after_checkpoint":
                    assert result.current_attempt == 2
                    assert result.activation_ids[0] == crashed["pending_activation_id"]
                    before_stale = await takeover_runner.state(run_id=run_id)
                    with pytest.raises(StaleWorkflowAttemptError):
                        await takeover_runner.resume(
                            run_id=run_id,
                            context=context,
                            attempt=1,
                        )
                    assert await takeover_runner.state(run_id=run_id) == before_stale

                history = await takeover_runner.history(run_id=run_id)
                _assert_atomic_commits(history)
                latest = await takeover_saver.aget_tuple(takeover_runner.config(run_id))
                assert latest is not None
                if mode == "delta":
                    assert latest.metadata["deerflow_checkpoint_channel_mode"] == "delta"
                else:
                    assert "deerflow_checkpoint_channel_mode" not in latest.metadata

            # Only the failed stage reruns. Earlier checkpointed stages do not.
            assert context.trace.attempts[fault_stage] == 4
            stage_index = FAULT_STAGES.index(fault_stage)
            for earlier_stage in FAULT_STAGES[:stage_index]:
                assert context.trace.attempts[earlier_stage] == 3
            await _assert_no_agent_run_or_thread_alias(
                migrated_postgres_database_url,
                run_id,
            )


@pytest.mark.postgres
@pytest.mark.anyio
@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="the real process-death probe requires SIGKILL",
)
async def test_real_sigkill_before_body_commit_is_resumed_by_a_new_worker(
    migrated_postgres_database_url: str,
) -> None:
    run_id = str(uuid.uuid4())
    child = Path(__file__).parent / "support" / "workflow_sigkill_child.py"
    environment = dict(os.environ)
    environment["WORKFLOW_TEST_DATABASE_URL"] = _checkpointer_url(migrated_postgres_database_url)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(child),
        run_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        assert process.stdout is not None
        marker = await asyncio.wait_for(process.stdout.readline(), timeout=15)
        if marker != b"BODY_ENTERED\n":
            assert process.stderr is not None
            stderr = await process.stderr.read()
            pytest.fail("SIGKILL child exited before its body checkpoint probe: " + stderr.decode("utf-8", errors="replace"))
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=10)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    async with AsyncPostgresSaver.from_conn_string(_checkpointer_url(migrated_postgres_database_url)) as takeover_saver:
        runner = WorkflowLoopRunner.compile(
            PLAN,
            checkpointer=takeover_saver,
            checkpoint_mode="full",
            cache=WorkflowCompilerCache(),
        )
        before = await runner.state(run_id=run_id)
        assert before.get("iteration", 0) == 0
        assert before.get("activation_ids", []) == []

        result = await runner.resume(run_id=run_id, context=_context())
        assert result.value == 3
        assert result.iterations == 3
        assert len(result.activation_ids) == len(set(result.activation_ids)) == 3
    await _assert_no_agent_run_or_thread_alias(
        migrated_postgres_database_url,
        run_id,
    )
