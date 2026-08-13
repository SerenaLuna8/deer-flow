"""Acceptance tests for the persistent-loop subagent scheduler.

The main suite mocks ``deerflow.subagents.executor`` during collection to break
an unrelated import cycle. Each case therefore runs a small clean-process probe
against the production executor module.
"""

import json
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _run_probe(scenario: str) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "tests/support/subagent_scheduler_probe.py", scenario],
        cwd=_BACKEND_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_one_run_can_start_four_subagents_without_scheduler_thread_starvation() -> None:
    result = _run_probe("single-run-four")

    assert result["max_active"] == 4
    assert result["scheduler_pool_threads"] == 0
    assert result["isolated_loop_threads"] == 1


def test_multiple_runs_share_the_isolated_loop_without_losing_detached_context() -> None:
    result = _run_probe("multi-run")

    assert result["started"] == 8
    assert result["context_matches"] is True
    assert result["child_configs_detached"] is True


def test_backpressure_queue_time_does_not_consume_execution_timeout() -> None:
    result = _run_probe("queued-timeout")

    assert result["status_while_queued"] == "pending"
    assert result["final_status"] == "completed"


def test_request_cancel_cancels_the_isolated_loop_future() -> None:
    result = _run_probe("cancel")

    assert result["status"] == "cancelled"
    assert result["coroutine_finalized"] is True


def test_cancel_request_is_not_lost_while_the_future_is_being_registered() -> None:
    assert _run_probe("cancel-during-submit")["status"] == "cancelled"


def test_execution_timeout_is_enforced_inside_the_isolated_loop() -> None:
    result = _run_probe("timeout")

    assert result["status"] == "timed_out"
    assert result["error"] == "Execution timed out after 0.1 seconds"
    assert result["elapsed"] < 1.0
    assert result["cancel_event"] is True
    assert result["coroutine_finalized"] is True


def test_scheduler_shutdown_cancels_tasks_and_stops_the_loop() -> None:
    result = _run_probe("shutdown")

    assert result["status"] == "cancelled"
    assert result["thread_stopped"] is True
    assert result["loop_closed"] is True
    assert result["tracked_futures"] == 0


def test_shutdown_linearizes_against_a_submission_that_has_not_reached_the_loop() -> None:
    result = _run_probe("shutdown-during-submit")

    assert result["status"] == "failed"
    assert result["error"] == "SUBAGENT_EXECUTION_FAILED"
    assert result["new_submission_status"] == "failed"
    assert result["new_submission_error"] == "SUBAGENT_EXECUTION_FAILED"
    assert result["submit_thread_stopped"] is True
    assert result["shutdown_thread_stopped"] is True
    assert result["old_loop_closed"] is True
    assert result["new_loop_created"] is False
    assert result["isolated_loop_threads"] == 0


def test_submission_failure_closes_the_never_scheduled_coroutine() -> None:
    result = _run_probe("submission-failure")

    assert result["status"] == "failed"
    assert result["error"] == "SUBAGENT_EXECUTION_FAILED"
    assert result["coroutine_closed"] is True


def test_module_reload_reopens_a_fresh_scheduler_generation_after_shutdown() -> None:
    result = _run_probe("shutdown-reload")

    assert result["old_loop_closed"] is True
    assert result["new_loop_running"] is True
    assert result["new_loop_is_distinct"] is True
