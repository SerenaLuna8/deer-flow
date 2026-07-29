"""Tests for SubagentLimitMiddleware."""

import logging
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.middlewares.subagent_limit_middleware import (
    MAX_CONCURRENT_SUBAGENTS,
    MAX_SUBAGENT_LIMIT,
    MIN_SUBAGENT_LIMIT,
    SubagentLimitMiddleware,
    _clamp_subagent_limit,
)
from deerflow.private_scope import PrivateResourceScope


def _make_runtime(
    run_id: str = "run-1",
    *,
    project_id: str = "project-1",
    owner_user_id: str = "owner-1",
):
    runtime = MagicMock()
    runtime.context = {
        "thread_id": "test-thread",
        "run_id": run_id,
        "private_scope": PrivateResourceScope(
            project_id=project_id,
            owner_user_id=owner_user_id,
            membership_version=1,
        ),
    }
    return runtime


def _task_call(task_id="call_1"):
    return {"name": "task", "id": task_id, "args": {"prompt": "do something"}}


def _other_call(name="bash", call_id="call_other"):
    return {"name": name, "id": call_id, "args": {}}


def _delegation(
    entry_id: str,
    *,
    run_id: str = "run-1",
    project_id: str = "project-1",
    owner_user_id: str = "owner-1",
    occurrence: int | None = None,
) -> dict:
    entry = {
        "id": entry_id,
        "project_id": project_id,
        "owner_user_id": owner_user_id,
        "run_id": run_id,
        "description": "prior work",
        "subagent_type": "general-purpose",
        "status": "completed",
        "created_at": "2026-07-11T00:00:00Z",
    }
    if occurrence is not None:
        entry["occurrence"] = occurrence
    return entry


def _raw_tool_call(call_id: str, name: str = "task") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


class TestClampSubagentLimit:
    def test_below_min_clamped_to_min(self):
        assert _clamp_subagent_limit(0) == MIN_SUBAGENT_LIMIT
        assert _clamp_subagent_limit(1) == MIN_SUBAGENT_LIMIT

    def test_above_max_clamped_to_max(self):
        assert _clamp_subagent_limit(10) == MAX_SUBAGENT_LIMIT
        assert _clamp_subagent_limit(100) == MAX_SUBAGENT_LIMIT

    def test_within_range_unchanged(self):
        assert _clamp_subagent_limit(2) == 2
        assert _clamp_subagent_limit(3) == 3
        assert _clamp_subagent_limit(4) == 4


class TestSubagentLimitMiddlewareInit:
    def test_default_max_concurrent(self):
        mw = SubagentLimitMiddleware()
        assert mw.max_concurrent == MAX_CONCURRENT_SUBAGENTS

    def test_custom_max_concurrent_clamped(self):
        mw = SubagentLimitMiddleware(max_concurrent=1)
        assert mw.max_concurrent == MIN_SUBAGENT_LIMIT

        mw = SubagentLimitMiddleware(max_concurrent=10)
        assert mw.max_concurrent == MAX_SUBAGENT_LIMIT

    def test_total_limit_is_bounded(self):
        assert SubagentLimitMiddleware(max_total=0).max_total == 1
        assert SubagentLimitMiddleware(max_total=500).max_total == 50


class TestTruncateTaskCalls:
    def test_no_messages_returns_none(self):
        mw = SubagentLimitMiddleware()
        assert mw._truncate_task_calls({"messages": []}) is None

    def test_missing_messages_returns_none(self):
        mw = SubagentLimitMiddleware()
        assert mw._truncate_task_calls({}) is None

    def test_last_message_not_ai_returns_none(self):
        mw = SubagentLimitMiddleware()
        state = {"messages": [HumanMessage(content="hello")]}
        assert mw._truncate_task_calls(state) is None

    def test_ai_no_tool_calls_returns_none(self):
        mw = SubagentLimitMiddleware()
        state = {"messages": [AIMessage(content="thinking...")]}
        assert mw._truncate_task_calls(state) is None

    def test_task_calls_within_limit_returns_none(self):
        mw = SubagentLimitMiddleware(max_concurrent=3)
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("t1"), _task_call("t2"), _task_call("t3")],
        )
        assert mw._truncate_task_calls({"messages": [msg]}) is None

    def test_task_calls_exceeding_limit_truncated(self):
        mw = SubagentLimitMiddleware(max_concurrent=2)
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("t1"), _task_call("t2"), _task_call("t3"), _task_call("t4")],
        )
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is not None
        updated_msg = result["messages"][0]
        task_calls = [tc for tc in updated_msg.tool_calls if tc["name"] == "task"]
        assert len(task_calls) == 2
        assert task_calls[0]["id"] == "t1"
        assert task_calls[1]["id"] == "t2"

    def test_non_task_calls_preserved(self):
        mw = SubagentLimitMiddleware(max_concurrent=2)
        msg = AIMessage(
            content="",
            tool_calls=[
                _other_call("bash", "b1"),
                _task_call("t1"),
                _task_call("t2"),
                _task_call("t3"),
                _other_call("read", "r1"),
            ],
        )
        result = mw._truncate_task_calls({"messages": [msg]})
        assert result is not None
        updated_msg = result["messages"][0]
        names = [tc["name"] for tc in updated_msg.tool_calls]
        assert "bash" in names
        assert "read" in names
        task_calls = [tc for tc in updated_msg.tool_calls if tc["name"] == "task"]
        assert len(task_calls) == 2

    def test_truncation_syncs_raw_provider_tool_calls(self):
        mw = SubagentLimitMiddleware(max_concurrent=2)
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("t1"), _task_call("t2"), _task_call("t3"), _task_call("t4")],
            additional_kwargs={"tool_calls": [_raw_tool_call("t1"), _raw_tool_call("t2"), _raw_tool_call("t3"), _raw_tool_call("t4")]},
            response_metadata={"finish_reason": "tool_calls"},
        )

        result = mw._truncate_task_calls({"messages": [msg]})

        assert result is not None
        updated_msg = result["messages"][0]
        assert [tc["id"] for tc in updated_msg.tool_calls] == ["t1", "t2"]
        assert [tc["id"] for tc in updated_msg.additional_kwargs["tool_calls"]] == ["t1", "t2"]
        assert updated_msg.response_metadata["finish_reason"] == "tool_calls"

    def test_single_batch_is_truncated_by_total_limit(self):
        mw = SubagentLimitMiddleware(max_concurrent=4, max_total=2)
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("t1"), _task_call("t2"), _task_call("t3")],
        )

        result = mw.after_model({"messages": [msg]}, _make_runtime())

        assert result is not None
        assert [call["id"] for call in result["messages"][0].tool_calls] == ["t1", "t2"]

    def test_multiple_rounds_consume_one_run_total(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=3)
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("t3"), _task_call("t4")],
            additional_kwargs={"tool_calls": [_raw_tool_call("t3"), _raw_tool_call("t4")]},
        )
        state = {
            "messages": [msg],
            "delegations": [_delegation("t1"), _delegation("t2")],
        }

        result = mw.after_model(state, _make_runtime())

        assert result is not None
        assert [call["id"] for call in result["messages"][0].tool_calls] == ["t3"]
        assert [call["id"] for call in result["messages"][0].additional_kwargs["tool_calls"]] == ["t3"]

    def test_same_thread_different_run_does_not_share_total(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=2)
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("new-1"), _task_call("new-2")],
        )
        state = {
            "messages": [HumanMessage(content="new request"), msg],
            "delegations": [
                _delegation("old-1", run_id="run-old"),
                _delegation("old-2", run_id="run-old"),
            ],
        }

        assert mw.after_model(state, _make_runtime(run_id="run-new")) is None

    def test_same_run_id_in_other_private_scope_does_not_share_total(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=2)
        msg = AIMessage(content="", tool_calls=[_task_call("current-1"), _task_call("current-2")])
        state = {
            "messages": [msg],
            "delegations": [
                _delegation("other-project", project_id="project-2"),
                _delegation("other-owner", owner_user_id="owner-2"),
            ],
        }

        assert mw.after_model(state, _make_runtime()) is None

    def test_existing_ledger_and_current_batch_share_remaining_capacity(self):
        mw = SubagentLimitMiddleware(max_concurrent=4, max_total=3)
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("new-1"), _task_call("new-2"), _task_call("new-3")],
        )
        state = {"messages": [msg], "delegations": [_delegation("existing")]}

        result = mw.after_model(state, _make_runtime())

        assert result is not None
        assert [call["id"] for call in result["messages"][0].tool_calls] == ["new-1", "new-2"]

    def test_missing_server_scope_counts_all_ledger_entries_fail_restrictive(self, caplog):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=1)
        runtime = MagicMock()
        runtime.context = {"thread_id": "test-thread", "run_id": "run-1"}
        msg = AIMessage(content="", tool_calls=[_task_call("new")])
        state = {"messages": [msg], "delegations": [_delegation("existing", run_id="run-old")]}

        with caplog.at_level(
            logging.WARNING,
            logger="deerflow.agents.middlewares.subagent_limit_middleware",
        ):
            result = mw.after_model(state, runtime)

        assert result is not None
        assert result["messages"][0].tool_calls == []
        assert "exact private delegation scope" in caplog.text

    def test_exhausted_total_adds_guidance_without_claiming_terminal_stop_reason(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=1)
        runtime = _make_runtime()
        msg = AIMessage(content="", tool_calls=[_task_call("new")])
        state = {"messages": [msg], "delegations": [_delegation("existing")]}

        result = mw.after_model(state, runtime)

        assert result is not None
        assert result["messages"][0].tool_calls == []
        assert "subagent delegation limit" in result["messages"][0].content
        assert "stop_reason" not in runtime.context

    def test_total_limit_persists_argument_free_run_journal_event(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=2)
        runtime = _make_runtime()
        journal = MagicMock()
        runtime.context["__run_journal"] = journal
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("new-1"), _task_call("new-2")],
        )
        state = {
            "messages": [msg],
            "delegations": [_delegation("existing")],
        }

        result = mw.after_model(state, runtime)

        assert result is not None
        assert [call["id"] for call in result["messages"][0].tool_calls] == ["new-1"]
        assert "subagent delegation limit" in result["messages"][0].content
        journal.record_middleware.assert_called_once_with(
            tag="subagent_limit",
            name="SubagentLimitMiddleware",
            hook="after_model",
            action="truncate_tool_calls",
            changes={
                "reason": "subagent_total_limit",
                "max_total": 2,
                "prior_delegations": 1,
                "admitted_task_calls": 1,
                "dropped_task_calls": 1,
            },
        )

    def test_concurrent_only_truncation_does_not_emit_total_limit_guidance_or_audit(self):
        mw = SubagentLimitMiddleware(max_concurrent=2, max_total=3)
        runtime = _make_runtime()
        journal = MagicMock()
        runtime.context["__run_journal"] = journal
        msg = AIMessage(
            content="original",
            tool_calls=[
                _task_call("new-1"),
                _task_call("new-2"),
                _task_call("new-3"),
                _task_call("new-4"),
                _task_call("new-5"),
            ],
        )

        result = mw.after_model({"messages": [msg], "delegations": []}, runtime)

        assert result is not None
        assert [call["id"] for call in result["messages"][0].tool_calls] == [
            "new-1",
            "new-2",
        ]
        assert result["messages"][0].content == "original"
        assert "stop_reason" not in runtime.context
        journal.record_middleware.assert_not_called()

    def test_total_limit_audit_counts_only_calls_dropped_beyond_concurrent_cap(self):
        mw = SubagentLimitMiddleware(max_concurrent=4, max_total=3)
        runtime = _make_runtime()
        journal = MagicMock()
        runtime.context["__run_journal"] = journal
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("new-1"), _task_call("new-2")],
        )
        state = {
            "messages": [msg],
            "delegations": [_delegation("old-1"), _delegation("old-2")],
        }

        result = mw.after_model(state, runtime)

        assert result is not None
        assert [call["id"] for call in result["messages"][0].tool_calls] == ["new-1"]
        journal.record_middleware.assert_called_once()
        assert journal.record_middleware.call_args.kwargs["changes"]["dropped_task_calls"] == 1

    def test_duplicate_provider_call_occurrences_each_consume_run_total(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=3)
        runtime = _make_runtime()
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("new-1"), _task_call("new-2")],
        )
        state = {
            "messages": [msg],
            "delegations": [
                _delegation("provider-reused", occurrence=1),
                _delegation("provider-reused", occurrence=2),
            ],
        }

        result = mw.after_model(state, runtime)

        assert result is not None
        assert [call["id"] for call in result["messages"][0].tool_calls] == ["new-1"]

    def test_duplicate_provider_call_ids_keep_raw_metadata_by_occurrence(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=1)
        runtime = _make_runtime()
        msg = AIMessage(
            content="",
            tool_calls=[
                _task_call("provider-reused"),
                _task_call("provider-reused"),
            ],
            additional_kwargs={
                "tool_calls": [
                    {
                        **_raw_tool_call("provider-reused"),
                        "index": 0,
                    },
                    {
                        **_raw_tool_call("provider-reused"),
                        "index": 1,
                    },
                ]
            },
        )

        result = mw.after_model(
            {"messages": [msg], "delegations": []},
            runtime,
        )

        assert result is not None
        updated = result["messages"][0]
        assert len(updated.tool_calls) == 1
        assert updated.additional_kwargs["tool_calls"] == [
            {
                **_raw_tool_call("provider-reused"),
                "index": 0,
            }
        ]

    def test_legacy_entry_without_occurrence_counts_as_first_occurrence(self):
        mw = SubagentLimitMiddleware(max_concurrent=3, max_total=3)
        runtime = _make_runtime()
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("new-1"), _task_call("new-2")],
        )
        state = {
            "messages": [msg],
            "delegations": [
                _delegation("provider-reused"),
                _delegation("provider-reused", occurrence=2),
            ],
        }

        result = mw.after_model(state, runtime)

        assert result is not None
        assert [call["id"] for call in result["messages"][0].tool_calls] == ["new-1"]

    def test_only_non_task_calls_returns_none(self):
        mw = SubagentLimitMiddleware()
        msg = AIMessage(
            content="",
            tool_calls=[_other_call("bash", "b1"), _other_call("read", "r1")],
        )
        assert mw._truncate_task_calls({"messages": [msg]}) is None


class TestAfterModel:
    def test_delegates_to_truncate(self):
        mw = SubagentLimitMiddleware(max_concurrent=2)
        runtime = _make_runtime()
        msg = AIMessage(
            content="",
            tool_calls=[_task_call("t1"), _task_call("t2"), _task_call("t3")],
        )
        result = mw.after_model({"messages": [msg]}, runtime)
        assert result is not None
        task_calls = [tc for tc in result["messages"][0].tool_calls if tc["name"] == "task"]
        assert len(task_calls) == 2
