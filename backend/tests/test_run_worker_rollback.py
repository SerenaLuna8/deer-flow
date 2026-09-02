import asyncio
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, TypedDict
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from deerflow.agents.memory.snip import MEMORY_ARCHIVE_CONTEXT_KEY
from deerflow.agents.middlewares.provider_request_usage import (
    ProviderDispatchOutcomeAmbiguous,
)
from deerflow.error_codes import PublicRunError, PublicRunErrorCode
from deerflow.runtime.checkpoint_state import (
    CheckpointStateAccessor,
    build_state_mutation_graph,
)
from deerflow.runtime.context_evidence import ContextRebaseReason
from deerflow.runtime.context_keys import (
    CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY,
    RuntimeContextKeys,
)
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.recovered_llm_failures import (
    RunRecoveredLLMFailureRecorder,
)
from deerflow.runtime.runs.checkpoint_rollback import (
    RollbackPoint,
    _collect_pre_existing_message_ids,
    _linearize_delta_checkpoint_resume,
    _rollback_to_pre_run_checkpoint,
)
from deerflow.runtime.runs.manager import ConflictError, RunManager
from deerflow.runtime.runs.schemas import RunStatus
from deerflow.runtime.runs.worker import (
    RunContext,
    _agent_factory_supports_app_config,
    _build_runtime_context,
    _extract_llm_error_fallback,
    _install_runtime_context,
    run_agent,
)
from deerflow.runtime.secret_context import _SLASH_SKILL_ACTIVATION_RUN_KEY
from deerflow.runtime.skill_context_authority import (
    VERIFIED_SKILL_SOURCE_CONTEXT_KEY,
)
from deerflow.subagents.runtime_catalog import build_runtime_agent_catalog

SAFE_MODEL_REF = "00000000-0000-4000-8000-000000000401"
AUTHORITATIVE_MODEL_REF = "00000000-0000-4000-8000-000000000402"
FAKE_TEST_MODEL_REF = "00000000-0000-4000-8000-000000000403"


class FakeCheckpointer:
    def __init__(self, *, put_result):
        self.adelete_thread = AsyncMock()
        self.aput = AsyncMock(return_value=put_result)
        self.aput_writes = AsyncMock()


def test_public_run_error_rejects_unclassified_message() -> None:
    with pytest.raises(TypeError, match="PublicRunErrorCode"):
        PublicRunError("provider-secret-sentinel")  # type: ignore[arg-type]


def test_public_run_error_codes_have_closed_stable_payloads() -> None:
    expected_messages = {
        PublicRunErrorCode.PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE: ("Private Run pre-run message boundary is unavailable"),
        PublicRunErrorCode.MODEL_OUTPUT_LIMIT: ("The model reached its output limit before completing the response"),
        PublicRunErrorCode.GRAPH_RECURSION_LIMIT: ("The Run stopped after reaching the graph execution step limit"),
        PublicRunErrorCode.LOOP_SAFETY_LIMIT: ("The Run stopped after reaching the loop safety limit"),
        PublicRunErrorCode.LOOP_FINALIZATION_FAILED: ("The model did not complete the required tool-free final response"),
        PublicRunErrorCode.TOOL_CALL_CONTROL_STATE_INVALID: ("The Run stopped because its tool-control state could not be validated"),
        PublicRunErrorCode.OUTPUT_DELIVERY_INCOMPLETE: ("The required output file was not presented"),
        PublicRunErrorCode.CURRENT_UPLOAD_UNAVAILABLE: ("The current image attachment could not be read or validated"),
        PublicRunErrorCode.SANDBOX_READ_ONLY_MOUNTS_UNSUPPORTED: ("Configured sandbox provider does not support run-scoped read-only mounts"),
        PublicRunErrorCode.LOCAL_HOST_BASH_READ_ONLY_MOUNTS_UNSUPPORTED: ("Local private runtime cannot enforce read-only mounts when host bash is enabled"),
        PublicRunErrorCode.PROVIDER_REQUEST_USAGE_UNSUPPORTED: ("Provider request usage cannot be measured safely for this request"),
        PublicRunErrorCode.PROVIDER_REQUEST_PROFILE_DRIFT: ("The final provider request no longer matches its frozen usage profile"),
        PublicRunErrorCode.CONTEXT_CAPACITY_EXCEEDED: ("The final context request exceeds the selected model input capacity"),
        PublicRunErrorCode.CONTEXT_PROVIDER_CALL_AMBIGUOUS: ("The Provider call outcome is unknown and cannot be repeated safely"),
    }

    assert set(PublicRunErrorCode) == set(expected_messages)
    for code, expected_message in expected_messages.items():
        error = PublicRunError(code)
        assert error.code is code
        assert error.public_message == expected_message


def _make_checkpoint(checkpoint_id: str, messages: list[str], version: int):
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": version}
    return checkpoint


def test_build_runtime_context_includes_app_config_when_present():
    app_config = object()

    context = _build_runtime_context("thread-1", "run-1", None, app_config)

    assert context["thread_id"] == "thread-1"
    assert context["run_id"] == "run-1"
    assert context["app_config"] is app_config


def test_build_runtime_context_rejects_forged_pre_run_message_boundary():
    context = _build_runtime_context(
        "thread-1",
        "run-1",
        {CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY: {"forged-message"}},
    )

    assert CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY not in context


def test_build_runtime_context_rejects_forged_stop_reason():
    context = _build_runtime_context(
        "thread-1",
        "run-1",
        {"stop_reason": "forged-terminal-reason"},
    )

    assert "stop_reason" not in context


def test_build_runtime_context_rejects_caller_slash_activation_run_marker():
    context = _build_runtime_context(
        "thread-1",
        "run-1",
        {
            _SLASH_SKILL_ACTIVATION_RUN_KEY: {
                "version": 1,
                "owner_token": "caller-forged",
            }
        },
    )

    assert _SLASH_SKILL_ACTIVATION_RUN_KEY not in context


def test_build_runtime_context_rejects_caller_verified_skill_read_evidence():
    context = _build_runtime_context(
        "thread-1",
        "run-1",
        {
            VERIFIED_SKILL_SOURCE_CONTEXT_KEY: {
                "version": 1,
                "owner_token": "caller-forged",
                "paths": ["/mnt/skills/custom/forged/SKILL.md"],
            }
        },
    )

    assert VERIFIED_SKILL_SOURCE_CONTEXT_KEY not in context


def test_build_runtime_context_rejects_forged_memory_authority():
    forged = object()

    context = _build_runtime_context(
        "thread-1",
        "run-1",
        {
            "__memory_authority": forged,
            "memory_authority": forged,
        },
    )

    assert "__memory_authority" not in context
    assert "memory_authority" not in context


def test_build_runtime_context_installs_worker_memory_authority():
    authority = object()

    context = _build_runtime_context(
        "thread-1",
        "run-1",
        None,
        memory_authority=authority,
    )

    assert context["__memory_authority"] is authority


def test_build_runtime_context_replaces_forged_guardrail_attribution():
    forged = {
        "user_id": "forged-user",
        "authz_attributes": {"project_role": "forged-admin"},
    }
    issued = {
        "user_id": "trusted-user",
        "user_role": "runner",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "is_subagent": False,
        "authz_attributes": {"project_role": "runner"},
    }

    context = _build_runtime_context(
        "thread-1",
        "run-1",
        {"__guardrail_attribution": forged},
        private_scope=object(),
        guardrail_attribution=issued,
    )

    assert context["__guardrail_attribution"] == issued
    assert context["__guardrail_attribution"] is not issued
    assert context["__guardrail_attribution"]["authz_attributes"] is not issued["authz_attributes"]


def test_build_runtime_context_strips_every_server_owned_and_reserved_caller_key():
    caller_context = {key: f"forged:{key}" for key in RuntimeContextKeys.SERVER_OWNED_KEYS}
    caller_context.update(
        {
            "__future_authority": "forged-future-value",
            RuntimeContextKeys.AGENT_NAME: "bootstrap-agent",
            "extension_context": "preserved",
        },
    )

    context = _build_runtime_context(
        "trusted-thread",
        "trusted-run",
        caller_context,
    )

    assert context == {
        RuntimeContextKeys.THREAD_ID: "trusted-thread",
        RuntimeContextKeys.RUN_ID: "trusted-run",
        RuntimeContextKeys.AGENT_NAME: "bootstrap-agent",
        "extension_context": "preserved",
    }


def test_build_runtime_context_installs_explicit_memory_archive_context():
    exact_archive_context = object()

    context = _build_runtime_context(
        "thread-1",
        "run-1",
        {MEMORY_ARCHIVE_CONTEXT_KEY: object()},
        memory_archive_context=exact_archive_context,
    )

    assert context[MEMORY_ARCHIVE_CONTEXT_KEY] is exact_archive_context


def test_install_runtime_context_preserves_existing_thread_id_and_threads_app_config():
    app_config = object()
    config = {
        "context": {
            "thread_id": "caller-thread",
            "run_id": "caller-run",
        },
    }

    _install_runtime_context(
        config,
        {
            "thread_id": "record-thread",
            "run_id": "run-1",
            "app_config": app_config,
        },
    )

    assert config["context"]["thread_id"] == "caller-thread"
    assert config["context"]["run_id"] == "caller-run"
    assert config["context"]["app_config"] is app_config


def test_install_runtime_context_overwrites_pre_run_boundary_from_worker():
    config = {
        "context": {
            CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY: {"forged-message"},
        }
    }

    _install_runtime_context(
        config,
        {
            "thread_id": "thread-1",
            "run_id": "run-1",
            CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY: frozenset({"trusted-message"}),
        },
    )

    assert config["context"][CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY] == frozenset({"trusted-message"})


def test_install_runtime_context_clears_stale_stop_reason():
    config = {
        "context": {
            "stop_reason": "stale-terminal-reason",
        }
    }

    _install_runtime_context(
        config,
        {
            "thread_id": "thread-1",
            "run_id": "run-1",
        },
    )

    assert "stop_reason" not in config["context"]


def test_install_runtime_context_never_installs_stop_reason():
    config = {}

    _install_runtime_context(
        config,
        {
            "thread_id": "thread-1",
            "run_id": "run-1",
            "stop_reason": "untrusted-terminal-reason",
        },
    )

    assert "stop_reason" not in config["context"]


def test_install_runtime_context_clears_slash_marker_when_private_config_is_reused():
    config = {
        "context": {
            "thread_id": "forged-thread",
            "run_id": "run-old",
            "user_id": "forged-owner",
            _SLASH_SKILL_ACTIVATION_RUN_KEY: {
                "version": 1,
                "owner_token": "old-owner-token",
                "project_id": "project-a",
                "owner_user_id": "owner-a",
                "run_id": "run-old",
                "message_key": "id:msg-1",
            },
        }
    }

    _install_runtime_context(
        config,
        {
            "thread_id": "thread-1",
            "run_id": "run-new",
            "private_scope": PrivateResourceScope(
                project_id="project-a",
                owner_user_id="owner-a",
                membership_version=2,
            ),
            "user_id": "owner-a",
        },
    )

    assert config["context"]["thread_id"] == "thread-1"
    assert config["context"]["run_id"] == "run-new"
    assert config["context"]["user_id"] == "owner-a"
    assert _SLASH_SKILL_ACTIVATION_RUN_KEY not in config["context"]


def test_install_runtime_context_clears_verified_reads_when_config_is_reused():
    config = {
        "context": {
            "thread_id": "thread-1",
            "run_id": "run-old",
            VERIFIED_SKILL_SOURCE_CONTEXT_KEY: {
                "version": 1,
                "owner_token": "old-owner-token",
                "identity": [
                    "private-v1",
                    "project-a",
                    "owner-a",
                    "run-old",
                ],
                "paths": ["/mnt/skills/custom/old/SKILL.md"],
            },
        }
    }

    _install_runtime_context(
        config,
        {
            "thread_id": "thread-1",
            "run_id": "run-new",
            "private_scope": PrivateResourceScope(
                project_id="project-a",
                owner_user_id="owner-a",
                membership_version=2,
            ),
            "user_id": "owner-a",
        },
    )

    assert config["context"]["run_id"] == "run-new"
    assert VERIFIED_SKILL_SOURCE_CONTEXT_KEY not in config["context"]


def test_install_runtime_context_overwrites_forged_memory_authority():
    exact = object()
    config = {
        "context": {
            "__memory_authority": "forged-memory-authority",
            "memory_authority": "forged-memory-authority",
        }
    }

    _install_runtime_context(
        config,
        {
            "thread_id": "thread-1",
            "run_id": "run-1",
            "private_scope": object(),
            "__memory_authority": exact,
        },
    )

    assert config["context"]["__memory_authority"] is exact
    assert "memory_authority" not in config["context"]


def test_install_runtime_context_clears_stale_memory_authority():
    config = {
        "context": {
            "__memory_authority": object(),
        }
    }

    _install_runtime_context(
        config,
        {
            "thread_id": "thread-1",
            "run_id": "run-2",
            "private_scope": object(),
        },
    )

    assert "__memory_authority" not in config["context"]


def test_install_runtime_context_uses_canonical_key_sets_for_reused_private_context():
    exact_catalog = build_runtime_agent_catalog(())
    trusted: dict[str, object] = {key: object() for key in RuntimeContextKeys.INSTALL_KEYS}
    trusted.update(
        {
            RuntimeContextKeys.THREAD_ID: "trusted-thread",
            RuntimeContextKeys.RUN_ID: "trusted-run",
            RuntimeContextKeys.USER_ID: "trusted-user",
            RuntimeContextKeys.IS_SUBAGENT: False,
            RuntimeContextKeys.PRIVATE_SCOPE: object(),
            RuntimeContextKeys.RUN_READ_ONLY_MOUNTS: (object(),),
            RuntimeContextKeys.GUARDRAIL_ATTRIBUTION: {
                "user_id": "trusted-user",
            },
            RuntimeContextKeys.RUNTIME_AGENT_CATALOG: exact_catalog,
            RuntimeContextKeys.SKILL_SCOPED_SECRETS: {
                "/mnt/skills/example/SKILL.md": {"TOKEN": "secret"},
            },
            RuntimeContextKeys.CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS: frozenset(
                {"message-1"},
            ),
            RuntimeContextKeys.TRACE_ID: "trusted-trace",
        },
    )
    config = {
        "context": {
            **{key: f"forged:{key}" for key in RuntimeContextKeys.SERVER_OWNED_KEYS},
            "__future_authority": "forged-future-value",
            RuntimeContextKeys.SECRETS: {"LEGACY_TOKEN": "preserved"},
            "extension_context": "preserved",
        },
    }

    _install_runtime_context(config, trusted)

    installed = config["context"]
    for key in RuntimeContextKeys.INSTALL_KEYS:
        assert installed[key] is trusted[key]
    assert not (RuntimeContextKeys.SERVER_OWNED_KEYS - RuntimeContextKeys.INSTALL_KEYS).intersection(installed)
    assert "__future_authority" not in installed
    assert installed[RuntimeContextKeys.SECRETS] == {
        "LEGACY_TOKEN": "preserved",
    }
    assert installed["extension_context"] == "preserved"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "checkpointer",
    [
        SimpleNamespace(aget_tuple=AsyncMock(side_effect=RuntimeError("database detail must not leak"))),
        SimpleNamespace(
            aget_tuple=AsyncMock(
                return_value=SimpleNamespace(
                    config={
                        "configurable": {
                            "thread_id": "private-thread",
                            "checkpoint_ns": "",
                            "checkpoint_id": "checkpoint-1",
                        }
                    },
                    checkpoint={"channel_values": {"messages": "malformed"}},
                    metadata={},
                    pending_writes=[],
                )
            )
        ),
        SimpleNamespace(
            aget_tuple=AsyncMock(
                return_value=SimpleNamespace(
                    config={
                        "configurable": {
                            "thread_id": "private-thread",
                            "checkpoint_ns": "",
                            "checkpoint_id": "checkpoint-1",
                        }
                    },
                    checkpoint={"channel_values": {"messages": [AIMessage(content="history without id")]}},
                    metadata={},
                    pending_writes=[],
                )
            )
        ),
    ],
    ids=["read-failed", "malformed", "history-without-stable-id"],
)
async def test_private_run_fails_closed_when_pre_run_message_boundary_is_unavailable(
    checkpointer,
):
    run_manager = RunManager()
    scope = PrivateResourceScope(
        project_id="project-1",
        owner_user_id="owner-1",
        membership_version=1,
    )
    record = await run_manager.register_persisted(
        run_id="private-run",
        thread_id="private-thread",
        assistant_id="project-agent",
        model_name=SAFE_MODEL_REF,
        scope=scope,
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    factory = MagicMock()

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=checkpointer, private_scope=scope),
        agent_factory=factory,
        graph_input={},
        config={},
    )

    assert record.status == RunStatus.error
    assert record.error == "Private Run pre-run message boundary is unavailable"
    error_events = [call.args[2] for call in bridge.publish.await_args_list if call.args[1] == "error"]
    assert error_events == [
        {
            "message": "Private Run pre-run message boundary is unavailable",
            "name": "PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE",
        }
    ]
    factory.assert_not_called()


@pytest.mark.anyio
async def test_model_output_limit_publishes_terminal_before_job_settlement() -> None:
    run_manager = RunManager()
    scope = PrivateResourceScope(
        project_id="project-1",
        owner_user_id="owner-1",
        membership_version=1,
    )
    record = await run_manager.register_persisted(
        run_id="output-limit-run",
        thread_id="output-limit-thread",
        assistant_id="project-agent",
        model_name=SAFE_MODEL_REF,
        scope=scope,
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    checkpointer = SimpleNamespace(aget_tuple=AsyncMock(return_value=None))

    class _Graph:
        async def astream(self, *args, **kwargs):
            del args, kwargs
            raise PublicRunError(PublicRunErrorCode.MODEL_OUTPUT_LIMIT)
            yield  # pragma: no cover

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=checkpointer, private_scope=scope),
        agent_factory=lambda **_kwargs: _Graph(),
        graph_input={"messages": []},
        config={},
    )

    assert record.status is RunStatus.error
    assert record.error == "MODEL_OUTPUT_LIMIT"
    assert record.terminal_authority == "durable_response"
    assert not [call for call in bridge.publish.await_args_list if call.args[1] == "error"]
    bridge.publish_end.assert_awaited_once_with("output-limit-run")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stream_modes",
    [
        ["messages", "values"],
        ["messages"],
    ],
)
async def test_model_output_limit_terminal_is_not_delayed_by_task_argument_deltas(
    stream_modes: list[str],
) -> None:
    run_manager = RunManager()
    record = await run_manager.create("output-limit-thread")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class _Graph:
        async def astream(self, *args, **kwargs):
            del args, kwargs
            for index in range(64):
                yield (
                    "messages",
                    (
                        AIMessageChunk(
                            content="",
                            id="task-message",
                            tool_call_chunks=[
                                {
                                    "name": "task" if index == 0 else None,
                                    "args": "验",
                                    "id": "task-call" if index == 0 else None,
                                    "index": 0,
                                    "type": "tool_call_chunk",
                                }
                            ],
                        ),
                        {"langgraph_checkpoint_ns": ""},
                    ),
                )
            raise PublicRunError(PublicRunErrorCode.MODEL_OUTPUT_LIMIT)

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda **_kwargs: _Graph(),
        graph_input={"messages": []},
        config={},
        stream_modes=stream_modes,
    )

    message_frames = [call.args[2] for call in bridge.publish.await_args_list if call.args[1] == "messages"]
    assert len(message_frames) == 2
    assert record.status is RunStatus.error
    assert record.error == "MODEL_OUTPUT_LIMIT"
    bridge.publish_end.assert_awaited_once_with(record.run_id)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "cancel_style",
    ["abort_event", "cancelled_error"],
)
async def test_observed_output_limit_terminal_wins_over_late_user_cancel(
    cancel_style: str,
) -> None:
    run_manager = RunManager()
    record = await run_manager.create("output-limit-cancel-thread")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class _Graph:
        async def astream(self, *args, **kwargs):
            del args
            runtime = kwargs["config"]["configurable"]["__pregel_runtime"]
            recorder = runtime.context[RuntimeContextKeys.RUN_SEMANTIC_STOP_RECORDER]
            recorder.record("model_output_limit")
            record.abort_event.set()
            if cancel_style == "cancelled_error":
                raise asyncio.CancelledError
            yield (
                "messages",
                (
                    AIMessageChunk(content="", id="output-limit-terminal"),
                    {"langgraph_checkpoint_ns": ""},
                ),
            )

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda **_kwargs: _Graph(),
        graph_input={"messages": []},
        config={},
        stream_modes=["messages", "values"],
    )

    assert record.status is RunStatus.error
    assert record.error == "MODEL_OUTPUT_LIMIT"
    assert not [call for call in bridge.publish.await_args_list if call.args[1] == "error"]
    bridge.publish_end.assert_awaited_once_with(record.run_id)


@pytest.mark.anyio
async def test_internal_rollback_preempts_observed_output_limit() -> None:
    run_manager = RunManager()
    record = await run_manager.create("output-limit-rollback-thread")
    record.abort_action = "rollback"
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class _Graph:
        async def astream(self, *args, **kwargs):
            del args
            runtime = kwargs["config"]["configurable"]["__pregel_runtime"]
            recorder = runtime.context[RuntimeContextKeys.RUN_SEMANTIC_STOP_RECORDER]
            recorder.record("model_output_limit")
            record.abort_event.set()
            if False:
                yield  # pragma: no cover

    with patch(
        "deerflow.runtime.runs.worker._rollback_to_pre_run_checkpoint",
        new=AsyncMock(return_value=True),
    ) as rollback:
        await run_agent(
            bridge,
            run_manager,
            record,
            ctx=RunContext(checkpointer=None),
            agent_factory=lambda **_kwargs: _Graph(),
            graph_input={"messages": []},
            config={},
        )

    assert record.status is RunStatus.error
    assert record.error == "Rolled back by user"
    assert record.terminal_authority == "ordinary"
    rollback.assert_awaited_once()
    bridge.publish_end.assert_awaited_once_with(record.run_id)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error_code",
    [
        PublicRunErrorCode.CONTEXT_CAPACITY_EXCEEDED,
        PublicRunErrorCode.CONTEXT_PROVIDER_CALL_AMBIGUOUS,
    ],
)
async def test_context_failures_publish_typed_terminal_before_job_settlement(
    error_code: PublicRunErrorCode,
) -> None:
    run_manager = RunManager()
    scope = PrivateResourceScope(
        project_id="project-1",
        owner_user_id="owner-1",
        membership_version=1,
    )
    record = await run_manager.register_persisted(
        run_id="context-terminal-run",
        thread_id="context-terminal-thread",
        assistant_id="project-agent",
        model_name=SAFE_MODEL_REF,
        scope=scope,
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    checkpointer = SimpleNamespace(aget_tuple=AsyncMock(return_value=None))

    class _Graph:
        async def astream(self, *args, **kwargs):
            del args, kwargs
            raise PublicRunError(error_code)
            yield  # pragma: no cover

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=checkpointer, private_scope=scope),
        agent_factory=lambda **_kwargs: _Graph(),
        graph_input={"messages": []},
        config={},
    )

    assert record.status is RunStatus.error
    assert record.error == error_code.value
    assert [call.args[2] for call in bridge.publish.await_args_list if call.args[1] == "error"] == [
        {
            "message": PublicRunError(error_code).public_message,
            "name": error_code.value,
        }
    ]
    bridge.publish_end.assert_awaited_once_with("context-terminal-run")


@pytest.mark.anyio
async def test_worker_preserves_provider_dispatch_ambiguity_for_durable_executor() -> None:
    run_manager = RunManager()
    record = await run_manager.create("thread-provider-ambiguity")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class _Graph:
        async def astream(self, *args, **kwargs):
            del args, kwargs
            raise ProviderDispatchOutcomeAmbiguous()
            yield  # pragma: no cover

    with pytest.raises(ProviderDispatchOutcomeAmbiguous):
        await run_agent(
            bridge,
            run_manager,
            record,
            ctx=RunContext(checkpointer=None),
            agent_factory=lambda **_kwargs: _Graph(),
            graph_input={},
            config={},
        )

    assert record.status is RunStatus.running
    assert record.error is None
    assert not [call for call in bridge.publish.await_args_list if call.args[1] == "error"]
    bridge.publish_end.assert_not_awaited()


@pytest.mark.anyio
async def test_private_run_allows_first_empty_checkpoint():
    run_manager = RunManager()
    scope = PrivateResourceScope(
        project_id="project-1",
        owner_user_id="owner-1",
        membership_version=1,
    )
    record = await run_manager.register_persisted(
        run_id="private-run",
        thread_id="private-thread",
        assistant_id="project-agent",
        model_name=SAFE_MODEL_REF,
        scope=scope,
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    checkpointer = SimpleNamespace(aget_tuple=AsyncMock(return_value=None))
    captured = {}

    class DummyAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            captured["boundary"] = config["context"][CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY]
            yield {"messages": []}

    def factory(*, config):
        del config
        return DummyAgent()

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=checkpointer, private_scope=scope),
        agent_factory=factory,
        graph_input={},
        config={},
    )

    assert record.status == RunStatus.success
    assert captured["boundary"] == frozenset()


@pytest.mark.anyio
async def test_worker_releases_private_authority_before_removing_pinned_skills():
    events: list[str] = []
    run_manager = RunManager()
    scope = PrivateResourceScope(
        project_id="project-1",
        owner_user_id="owner-1",
        membership_version=1,
    )
    record = await run_manager.register_persisted(
        run_id="private-run-cleanup-order",
        thread_id="private-thread-cleanup-order",
        assistant_id="project-agent",
        model_name=SAFE_MODEL_REF,
        scope=scope,
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class Authority:
        sandbox_id = "private-sandbox"

        async def restore(self):
            return SimpleNamespace(entries=())

        async def finalize(self):
            return SimpleNamespace(workspace_changes={})

        async def mark_failed(self) -> None:
            return None

        async def release(self) -> None:
            events.append("authority-release")

    class PrivateRuntime:
        skills = ()
        mcp_tools = ()
        agent_catalog = None

        async def aclose(self) -> None:
            events.append("runtime-close")

    class DummyAgent:
        async def astream(
            self,
            graph_input,
            config=None,
            stream_mode=None,
            subgraphs=False,
        ):
            del graph_input, config, stream_mode, subgraphs
            yield {"messages": []}

    def private_factory(*, config, private_runtime):
        del config
        assert isinstance(private_runtime, PrivateRuntime)
        return DummyAgent()

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(
            checkpointer=SimpleNamespace(aget_tuple=AsyncMock(return_value=None)),
            private_scope=scope,
            file_authority=Authority(),
            private_agent_runtime=PrivateRuntime(),
        ),
        agent_factory=private_factory,
        graph_input={},
        config={},
    )

    assert record.status == RunStatus.success
    assert events == ["authority-release", "runtime-close"]


@pytest.mark.anyio
async def test_private_run_forces_persisted_thread_and_root_checkpoint_namespace():
    run_manager = RunManager()
    scope = PrivateResourceScope(
        project_id="project-1",
        owner_user_id="owner-1",
        membership_version=1,
    )
    record = await run_manager.register_persisted(
        run_id="private-run",
        thread_id="persisted-thread",
        assistant_id="project-agent",
        model_name=SAFE_MODEL_REF,
        scope=scope,
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    captured: dict[str, Any] = {}

    class DummyAgent:
        async def astream(
            self,
            graph_input,
            config=None,
            stream_mode=None,
            subgraphs=False,
        ):
            del graph_input, stream_mode, subgraphs
            captured["stream_configurable"] = dict(config["configurable"])
            yield {"messages": []}

    def factory(*, config):
        captured["factory_configurable"] = dict(config["configurable"])
        return DummyAgent()

    checkpointer = SimpleNamespace(
        aget_tuple=AsyncMock(return_value=None),
    )
    caller_config = {
        "configurable": {
            "thread_id": "forged-thread",
            "checkpoint_ns": "forged-subgraph",
            "checkpoint_id": "admitted-checkpoint",
            "checkpoint_map": {"": "admitted-checkpoint"},
        }
    }
    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=checkpointer, private_scope=scope),
        agent_factory=factory,
        graph_input={},
        config=caller_config,
    )

    for configurable in (
        captured["factory_configurable"],
        captured["stream_configurable"],
        caller_config["configurable"],
    ):
        assert configurable["thread_id"] == "persisted-thread"
        assert configurable["checkpoint_ns"] == ""
        assert configurable["checkpoint_id"] == "admitted-checkpoint"
        assert configurable["checkpoint_map"] == {"": "admitted-checkpoint"}
    for checkpoint_call in checkpointer.aget_tuple.await_args_list:
        durable_configurable = checkpoint_call.args[0]["configurable"]
        assert durable_configurable["thread_id"] == "persisted-thread"
        assert durable_configurable["checkpoint_ns"] == ""


@pytest.mark.anyio
async def test_run_agent_threads_explicit_app_config_into_config_only_factory():
    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    app_config = object()
    captured: dict[str, object] = {}

    class DummyAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            captured["astream_context"] = config["context"]
            yield {"messages": []}

    def factory(*, config):
        captured["factory_context"] = config["context"]
        return DummyAgent()

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None, app_config=app_config),
        agent_factory=factory,
        graph_input={},
        config={},
    )
    await asyncio.sleep(0)

    assert captured["factory_context"]["app_config"] is app_config
    assert captured["astream_context"]["app_config"] is app_config
    assert captured["factory_context"][CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY] == frozenset()
    assert captured["astream_context"][CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY] == frozenset()
    factory_recorder = captured["factory_context"][RuntimeContextKeys.RECOVERED_LLM_FAILURE_RECORDER]
    assert isinstance(factory_recorder, RunRecoveredLLMFailureRecorder)
    assert captured["astream_context"][RuntimeContextKeys.RECOVERED_LLM_FAILURE_RECORDER] is factory_recorder
    fetched = await run_manager.get(record.run_id)
    assert fetched is not None
    assert fetched.status == RunStatus.success
    bridge.publish_end.assert_awaited_once_with(record.run_id)
    bridge.cleanup.assert_not_awaited()


@pytest.mark.anyio
async def test_run_agent_replaces_forged_memory_archive_with_explicit_context():
    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    exact_archive_context = object()
    captured: dict[str, object] = {}

    class DummyAgent:
        async def astream(
            self,
            graph_input,
            config=None,
            stream_mode=None,
            subgraphs=False,
        ):
            del graph_input, stream_mode, subgraphs
            captured["astream_archive"] = config["context"][MEMORY_ARCHIVE_CONTEXT_KEY]
            yield {"messages": []}

    def factory(*, config):
        captured["factory_archive"] = config["context"][MEMORY_ARCHIVE_CONTEXT_KEY]
        return DummyAgent()

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            memory_archive_context=exact_archive_context,
        ),
        agent_factory=factory,
        graph_input={},
        config={
            "context": {
                MEMORY_ARCHIVE_CONTEXT_KEY: object(),
            },
        },
    )
    await asyncio.sleep(0)

    assert captured == {
        "factory_archive": exact_archive_context,
        "astream_archive": exact_archive_context,
    }
    assert record.status == RunStatus.success


@pytest.mark.anyio
@pytest.mark.parametrize("incoming_model_name", [None, "forged-client-model"])
async def test_private_run_agent_factory_receives_authoritative_persisted_model_name(
    incoming_model_name: str | None,
) -> None:
    from deerflow.agents.lead_agent.agent import _get_runtime_config
    from deerflow.runtime.private_scope import PrivateResourceScope

    run_manager = RunManager()
    record = await run_manager.register_persisted(
        run_id="private-run",
        thread_id="private-thread",
        assistant_id="project-assistant",
        model_name=AUTHORITATIVE_MODEL_REF,
        scope=PrivateResourceScope(
            project_id="exact-project",
            owner_user_id="exact-owner",
            membership_version=1,
        ),
    )
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    captured: dict[str, object] = {}

    class PrivateRuntime:
        async def aclose(self) -> None:
            return None

    private_runtime = PrivateRuntime()

    class DummyAgent:
        metadata = {"model_name": AUTHORITATIVE_MODEL_REF}

        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            captured["astream_model_name"] = config["configurable"].get("model_name")
            captured["astream_context_model_name"] = config["context"].get("model_name")
            yield {"messages": []}

    def factory(*, config, private_runtime):
        captured["factory_model_name"] = config["configurable"].get("model_name")
        captured["factory_context_model_name"] = config["context"].get("model_name")
        captured["factory_resolved_model_name"] = _get_runtime_config(config).get("model_name")
        captured["private_runtime"] = private_runtime
        return DummyAgent()

    configurable = {} if incoming_model_name is None else {"model_name": incoming_model_name}
    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(
            checkpointer=None,
            private_agent_runtime=private_runtime,
        ),
        agent_factory=factory,
        graph_input={},
        config={
            "configurable": configurable,
            "context": {"model_name": "forged-context-model"},
        },
    )
    await asyncio.sleep(0)

    assert captured == {
        "factory_model_name": AUTHORITATIVE_MODEL_REF,
        "factory_context_model_name": AUTHORITATIVE_MODEL_REF,
        "factory_resolved_model_name": AUTHORITATIVE_MODEL_REF,
        "astream_model_name": AUTHORITATIVE_MODEL_REF,
        "astream_context_model_name": AUTHORITATIVE_MODEL_REF,
        "private_runtime": private_runtime,
    }
    assert record.status == RunStatus.success
    bridge.publish_end.assert_awaited_once_with(record.run_id)


@pytest.mark.anyio
async def test_run_agent_marks_llm_error_fallback_as_error_status():
    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class DummyAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            yield {
                "messages": [
                    AIMessage(
                        content="The configured LLM provider is temporarily unavailable after multiple retries.",
                        additional_kwargs={
                            "deerflow_error_fallback": True,
                            "error_type": "APIConnectionError",
                            "error_reason": "transient",
                            "error_detail": "Connection error.",
                        },
                    )
                ]
            }

    def factory(*, config):
        return DummyAgent()

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=factory,
        graph_input={},
        config={},
    )

    fetched = await run_manager.get(record.run_id)
    assert fetched is not None
    assert fetched.status == RunStatus.error
    assert fetched.error == "LLM_PROVIDER_UNAVAILABLE"
    bridge.publish_end.assert_awaited_once_with(record.run_id)


@pytest.mark.anyio
async def test_run_agent_does_not_promote_subagent_custom_fallback_to_run_failure():
    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    class DummyAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            del graph_input, config, stream_mode, subgraphs
            yield (
                "custom",
                {
                    "type": "task_running",
                    "task_id": "child-task-1",
                    "message": AIMessage(
                        content="The delegated model provider is unavailable.",
                        additional_kwargs={
                            "deerflow_error_fallback": True,
                            "error_code": "LLM_PROVIDER_UNAVAILABLE",
                            "error_reason": "transient",
                        },
                    ),
                },
            )
            yield (
                "values",
                {
                    "messages": [
                        AIMessage(
                            id="lead-final",
                            content="The lead agent completed the requested work.",
                        )
                    ]
                },
            )

    def factory(*, config):
        del config
        return DummyAgent()

    outcome = await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=factory,
        graph_input={},
        config={},
        stream_modes=["custom"],
    )

    fetched = await run_manager.get(record.run_id)
    assert fetched is not None
    assert fetched.status == RunStatus.success
    assert fetched.error is None
    assert outcome.status == "succeeded"
    assert outcome.public_error_code is None
    assert all(call.args[1] != "values" for call in bridge.publish.await_args_list)
    bridge.publish_end.assert_awaited_once_with(record.run_id)


@pytest.mark.anyio
async def test_run_agent_custom_only_still_observes_lead_fallback():
    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    async def lead_fallback(_state):
        return {
            "messages": [
                AIMessage(
                    id="lead-fallback",
                    content="The configured model provider is unavailable.",
                    additional_kwargs={
                        "deerflow_error_fallback": True,
                        "error_code": "LLM_PROVIDER_UNAVAILABLE",
                        "error_reason": "transient",
                    },
                )
            ]
        }

    builder = StateGraph(MessagesState)
    builder.add_node("lead_fallback", lead_fallback)
    builder.add_edge(START, "lead_fallback")
    builder.add_edge("lead_fallback", END)
    graph = builder.compile()

    def factory(*, config):
        del config
        return graph

    outcome = await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=factory,
        graph_input={"messages": [HumanMessage(content="Do the work")]},
        config={},
        stream_modes=["custom"],
    )

    fetched = await run_manager.get(record.run_id)
    assert fetched is not None
    assert fetched.status == RunStatus.error
    assert fetched.error == "LLM_PROVIDER_UNAVAILABLE"
    assert outcome.status == "failed"
    assert outcome.public_error_code == "LLM_PROVIDER_UNAVAILABLE"
    assert all(call.args[1] != "values" for call in bridge.publish.await_args_list)
    bridge.publish_end.assert_awaited_once_with(record.run_id)


@pytest.mark.anyio
async def test_run_agent_messages_lane_does_not_promote_nested_graph_fallback():
    class ParentState(TypedDict, total=False):
        prompt: str
        result: str

    class ChildState(TypedDict, total=False):
        prompt: str
        messages: list[Any]

    async def child_fallback(_state):
        return {
            "messages": [
                AIMessage(
                    id="child-fallback",
                    content="The delegated model provider is unavailable.",
                    additional_kwargs={
                        "deerflow_error_fallback": True,
                        "error_code": "LLM_PROVIDER_UNAVAILABLE",
                        "error_reason": "transient",
                    },
                )
            ]
        }

    child_builder = StateGraph(ChildState)
    child_builder.add_node("child_fallback", child_fallback)
    child_builder.add_edge(START, "child_fallback")
    child_builder.add_edge("child_fallback", END)

    async def lead_final(_state):
        return {"result": "ok"}

    parent_builder = StateGraph(ParentState)
    parent_builder.add_node("child", child_builder.compile())
    parent_builder.add_node("lead_final", lead_final)
    parent_builder.add_edge(START, "child")
    parent_builder.add_edge("child", "lead_final")
    parent_builder.add_edge("lead_final", END)
    graph = parent_builder.compile()

    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    outcome = await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda **_: graph,
        graph_input={"prompt": "Do the work"},
        config={},
        stream_modes=["messages"],
        stream_subgraphs=False,
    )

    fetched = await run_manager.get(record.run_id)
    assert fetched is not None
    assert fetched.status == RunStatus.success
    assert fetched.error is None
    assert outcome.status == "succeeded"
    assert outcome.public_error_code is None
    assert all(call.args[1] != "values" for call in bridge.publish.await_args_list)
    bridge.publish_end.assert_awaited_once_with(record.run_id)


@pytest.mark.anyio
async def test_run_agent_events_only_keeps_internal_values_lane_hidden():
    async def lead_final(_state):
        return {
            "messages": [
                AIMessage(
                    id="lead-final",
                    content="The lead agent completed the requested work.",
                )
            ]
        }

    builder = StateGraph(MessagesState)
    builder.add_node("lead_final", lead_final)
    builder.add_edge(START, "lead_final")
    builder.add_edge("lead_final", END)
    graph = builder.compile()

    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    outcome = await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=lambda **_: graph,
        graph_input={"messages": [HumanMessage(content="Do the work")]},
        config={},
        stream_modes=["events"],
    )

    fetched = await run_manager.get(record.run_id)
    assert fetched is not None
    assert fetched.status == RunStatus.success
    assert outcome.status == "succeeded"
    assert all(call.args[1] != "values" for call in bridge.publish.await_args_list)
    bridge.publish_end.assert_awaited_once_with(record.run_id)


@pytest.mark.anyio
async def test_run_agent_defaults_root_run_name_from_assistant_id():
    run_manager = RunManager()
    record = await run_manager.create("thread-1", assistant_id="lead_agent")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    captured: dict[str, object] = {}

    class DummyAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            captured["astream_run_name"] = config["run_name"]
            yield {"messages": []}

    def factory(*, config):
        captured["factory_run_name"] = config["run_name"]
        return DummyAgent()

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=factory,
        graph_input={},
        config={},
    )

    assert captured["factory_run_name"] == "lead_agent"
    assert captured["astream_run_name"] == "lead_agent"


@pytest.mark.anyio
async def test_run_agent_defaults_root_run_name_from_context_agent_name():
    run_manager = RunManager()
    record = await run_manager.create("thread-1", assistant_id="lead_agent")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    captured: dict[str, object] = {}

    class DummyAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            captured["astream_run_name"] = config["run_name"]
            yield {"messages": []}

    def factory(*, config):
        captured["factory_run_name"] = config["run_name"]
        return DummyAgent()

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=factory,
        graph_input={},
        config={"context": {"agent_name": "finalis"}},
    )

    assert captured["factory_run_name"] == "finalis"
    assert captured["astream_run_name"] == "finalis"


@pytest.mark.anyio
async def test_run_agent_defaults_root_run_name_from_configurable_agent_name():
    run_manager = RunManager()
    record = await run_manager.create("thread-1", assistant_id="lead_agent")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    captured: dict[str, object] = {}

    class DummyAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            captured["astream_run_name"] = config["run_name"]
            yield {"messages": []}

    def factory(*, config):
        captured["factory_run_name"] = config["run_name"]
        return DummyAgent()

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=None),
        agent_factory=factory,
        graph_input={},
        config={"configurable": {"agent_name": "finalis"}},
    )

    assert captured["factory_run_name"] == "finalis"
    assert captured["astream_run_name"] == "finalis"


@pytest.mark.anyio
async def test_rollback_restores_snapshot_without_deleting_thread():
    checkpointer = FakeCheckpointer(put_result={"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "restored-1"}})

    await _rollback_to_pre_run_checkpoint(
        checkpointer=checkpointer,
        thread_id="thread-1",
        run_id="run-1",
        pre_run_checkpoint_id="ckpt-1",
        pre_run_snapshot={
            "checkpoint_ns": "",
            "checkpoint": {
                "id": "ckpt-1",
                "channel_versions": {"messages": 3},
                "channel_values": {"messages": ["before"]},
            },
            "metadata": {"source": "input"},
            "pending_writes": [
                ("task-a", "messages", {"content": "first"}),
                ("task-a", "status", "done"),
                ("task-b", "events", {"type": "tool"}),
            ],
        },
        snapshot_capture_failed=False,
    )

    checkpointer.adelete_thread.assert_not_awaited()
    checkpointer.aput.assert_awaited_once()
    restore_config, restored_checkpoint, restored_metadata, new_versions = checkpointer.aput.await_args.args
    assert restore_config == {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}
    assert restored_checkpoint["id"] != "ckpt-1"
    assert "channel_versions" in restored_checkpoint
    assert "channel_values" in restored_checkpoint
    assert restored_checkpoint["channel_versions"] == {"messages": 3}
    assert restored_checkpoint["channel_values"] == {"messages": ["before"]}
    assert restored_metadata == {"source": "input"}
    assert new_versions == {"messages": 3}
    assert checkpointer.aput_writes.await_args_list == [
        call(
            {"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "restored-1"}},
            [("messages", {"content": "first"}), ("status", "done")],
            task_id="task-a",
        ),
        call(
            {"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "restored-1"}},
            [("events", {"type": "tool"})],
            task_id="task-b",
        ),
    ]


@pytest.mark.anyio
async def test_rollback_restored_checkpoint_becomes_latest_with_real_checkpointer():
    checkpointer = InMemorySaver()
    thread_config = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}
    before_checkpoint = _make_checkpoint("0001", ["before"], 1)
    before_config = checkpointer.put(thread_config, before_checkpoint, {"step": 1}, {"messages": 1})
    after_checkpoint = _make_checkpoint("0002", ["after"], 2)
    after_config = checkpointer.put(before_config, after_checkpoint, {"step": 2}, {"messages": 2})
    checkpointer.put_writes(after_config, [("messages", "pending-after")], task_id="task-after")

    await _rollback_to_pre_run_checkpoint(
        checkpointer=checkpointer,
        thread_id="thread-1",
        run_id="run-1",
        pre_run_checkpoint_id="0001",
        pre_run_snapshot={
            "checkpoint_ns": "",
            "checkpoint": before_checkpoint,
            "metadata": {"step": 1},
            "pending_writes": [("task-before", "messages", "pending-before")],
        },
        snapshot_capture_failed=False,
    )

    latest = checkpointer.get_tuple(thread_config)

    assert latest is not None
    assert latest.config["configurable"]["checkpoint_id"] != "0001"
    assert latest.config["configurable"]["checkpoint_id"] != "0002"
    assert latest.checkpoint["channel_values"] == {"messages": ["before"]}
    assert latest.pending_writes == [("task-before", "messages", "pending-before")]
    assert ("task-after", "messages", "pending-after") not in latest.pending_writes


@pytest.mark.anyio
async def test_graph_rollback_records_new_context_window_generation() -> None:
    checkpointer = InMemorySaver()
    graph = build_state_mutation_graph("rollback-test", "full")
    accessor = CheckpointStateAccessor.bind(
        graph,
        checkpointer,
        mode="full",
    )
    head = {
        "configurable": {
            "thread_id": "thread-context-rollback",
            "checkpoint_ns": "",
        }
    }
    before_config = await accessor.aupdate(
        head,
        {"messages": [HumanMessage(id="before", content="before")]},
        as_node="rollback-test",
    )
    before = await accessor.aget(before_config)
    after_config = await accessor.aupdate(
        before_config,
        {"messages": [HumanMessage(id="after", content="after")]},
        as_node="rollback-test",
    )
    observer = SimpleNamespace(record_window_rebased=AsyncMock())

    restored = await _rollback_to_pre_run_checkpoint(
        checkpointer=checkpointer,
        accessor=accessor,
        thread_id="thread-context-rollback",
        run_id="run-context-rollback",
        rollback_point=RollbackPoint(
            config=dict(before.config),
            state_values=dict(before.values),
            messages=tuple(before.values["messages"]),
            metadata=dict(before.metadata),
            pending_writes=(),
        ),
        snapshot_capture_failed=False,
        context_evidence_observer=observer,
    )

    assert restored is True
    call_kwargs = observer.record_window_rebased.await_args.kwargs
    assert call_kwargs["reason"] is ContextRebaseReason.ROLLBACK
    assert call_kwargs["source_checkpoint_id"] == (after_config["configurable"]["checkpoint_id"])
    assert call_kwargs["result_checkpoint_id"] != (after_config["configurable"]["checkpoint_id"])


@pytest.mark.anyio
async def test_rollback_deletes_thread_when_no_snapshot_exists():
    checkpointer = FakeCheckpointer(put_result=None)

    await _rollback_to_pre_run_checkpoint(
        checkpointer=checkpointer,
        thread_id="thread-1",
        run_id="run-1",
        pre_run_checkpoint_id=None,
        pre_run_snapshot=None,
        snapshot_capture_failed=False,
    )

    checkpointer.adelete_thread.assert_awaited_once_with("thread-1")
    checkpointer.aput.assert_not_awaited()
    checkpointer.aput_writes.assert_not_awaited()


@pytest.mark.anyio
async def test_private_rollback_without_checkpoint_never_deletes_business_thread():
    checkpointer = FakeCheckpointer(put_result=None)

    restored = await _rollback_to_pre_run_checkpoint(
        checkpointer=checkpointer,
        thread_id="thread-1",
        run_id="run-1",
        pre_run_checkpoint_id=None,
        pre_run_snapshot=None,
        snapshot_capture_failed=False,
        allow_thread_delete=False,
    )

    assert restored is False
    checkpointer.adelete_thread.assert_not_awaited()
    checkpointer.aput.assert_not_awaited()
    checkpointer.aput_writes.assert_not_awaited()


def test_state_replacement_rejects_channels_outside_effective_schema():
    mutation_graph = build_state_mutation_graph("rollback", "full")
    accessor = CheckpointStateAccessor.bind(
        mutation_graph,
        object(),
        mode="full",
    )

    with pytest.raises(RuntimeError, match="outside the effective schema"):
        accessor.replacement_values(
            {
                "messages": [],
                "unknown_middleware_channel": "must-not-be-dropped",
            },
            current_values={"messages": []},
        )


@pytest.mark.anyio
async def test_delta_historical_resume_replaces_current_head_without_replay() -> None:
    checkpointer = InMemorySaver()
    graph = build_state_mutation_graph("checkpoint_resume", "delta")
    accessor = CheckpointStateAccessor.bind(
        graph,
        checkpointer,
        mode="delta",
    )
    head_config = {
        "configurable": {
            "thread_id": "thread-delta-resume",
            "checkpoint_ns": "",
        },
    }
    selected_config = await accessor.aupdate(
        head_config,
        accessor.replacement_values(
            {"messages": [HumanMessage(content="selected history")]},
            current_values={},
        ),
        as_node="checkpoint_resume",
    )
    await accessor.aupdate(
        head_config,
        accessor.replacement_values(
            {
                "messages": [HumanMessage(content="newer head")],
                "artifacts": ["outputs/stale.txt"],
                "summary_text": "stale summary",
            },
            current_values=(await accessor.aget(head_config)).values,
        ),
        as_node="checkpoint_resume",
    )
    selected_id = selected_config["configurable"]["checkpoint_id"]
    run_config = {
        "configurable": {
            "thread_id": "thread-delta-resume",
            "checkpoint_ns": "",
            "checkpoint_id": selected_id,
            "checkpoint_map": {"": selected_id},
        },
    }

    messages = await _linearize_delta_checkpoint_resume(
        accessor=accessor,
        checkpointer=checkpointer,
        config=run_config,
        thread_id="thread-delta-resume",
        run_id="run-delta-resume",
    )

    materialized = await accessor.aget(head_config)
    assert [message.content for message in messages or []] == [
        "selected history",
    ]
    assert [message.content for message in materialized.values["messages"]] == [
        "selected history",
    ]
    assert materialized.values["artifacts"] == []
    assert materialized.values["summary_text"] is None
    assert "checkpoint_id" not in run_config["configurable"]
    assert "checkpoint_map" not in run_config["configurable"]


@pytest.mark.anyio
async def test_rollback_raises_when_restore_config_has_no_checkpoint_id():
    checkpointer = FakeCheckpointer(put_result={"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}})

    with pytest.raises(RuntimeError, match="did not return checkpoint_id"):
        await _rollback_to_pre_run_checkpoint(
            checkpointer=checkpointer,
            thread_id="thread-1",
            run_id="run-1",
            pre_run_checkpoint_id="ckpt-1",
            pre_run_snapshot={
                "checkpoint_ns": "",
                "checkpoint": {"id": "ckpt-1", "channel_versions": {}},
                "metadata": {},
                "pending_writes": [("task-a", "messages", "value")],
            },
            snapshot_capture_failed=False,
        )

    checkpointer.adelete_thread.assert_not_awaited()
    checkpointer.aput.assert_awaited_once()
    checkpointer.aput_writes.assert_not_awaited()


@pytest.mark.anyio
async def test_rollback_normalizes_none_checkpoint_ns_to_root_namespace():
    checkpointer = FakeCheckpointer(put_result={"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "restored-1"}})

    await _rollback_to_pre_run_checkpoint(
        checkpointer=checkpointer,
        thread_id="thread-1",
        run_id="run-1",
        pre_run_checkpoint_id="ckpt-1",
        pre_run_snapshot={
            "checkpoint_ns": None,
            "checkpoint": {"id": "ckpt-1", "channel_versions": {}},
            "metadata": {},
            "pending_writes": [],
        },
        snapshot_capture_failed=False,
    )

    checkpointer.aput.assert_awaited_once()
    restore_config, restored_checkpoint, restored_metadata, new_versions = checkpointer.aput.await_args.args
    assert restore_config == {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}
    assert restored_checkpoint["id"] != "ckpt-1"
    assert restored_checkpoint["channel_versions"] == {}
    assert restored_metadata == {}
    assert new_versions == {}


@pytest.mark.anyio
async def test_rollback_raises_on_malformed_pending_write_not_a_tuple():
    """pending_writes containing a non-3-tuple item should raise RuntimeError."""
    checkpointer = FakeCheckpointer(put_result={"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "restored-1"}})

    with pytest.raises(RuntimeError, match="rollback failed: pending_write is not a 3-tuple"):
        await _rollback_to_pre_run_checkpoint(
            checkpointer=checkpointer,
            thread_id="thread-1",
            run_id="run-1",
            pre_run_checkpoint_id="ckpt-1",
            pre_run_snapshot={
                "checkpoint_ns": "",
                "checkpoint": {"id": "ckpt-1", "channel_versions": {}},
                "metadata": {},
                "pending_writes": [
                    ("task-a", "messages", "valid"),  # valid
                    ["only", "two"],  # malformed: only 2 elements
                ],
            },
            snapshot_capture_failed=False,
        )

    # aput succeeded but aput_writes should not be called due to malformed data
    checkpointer.aput.assert_awaited_once()
    checkpointer.aput_writes.assert_not_awaited()


@pytest.mark.anyio
async def test_rollback_raises_on_malformed_pending_write_non_string_channel():
    """pending_writes containing a non-string channel should raise RuntimeError."""
    checkpointer = FakeCheckpointer(put_result={"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "restored-1"}})

    with pytest.raises(RuntimeError, match="rollback failed: pending_write has non-string channel"):
        await _rollback_to_pre_run_checkpoint(
            checkpointer=checkpointer,
            thread_id="thread-1",
            run_id="run-1",
            pre_run_checkpoint_id="ckpt-1",
            pre_run_snapshot={
                "checkpoint_ns": "",
                "checkpoint": {"id": "ckpt-1", "channel_versions": {}},
                "metadata": {},
                "pending_writes": [
                    ("task-a", 123, "value"),  # malformed: channel is not a string
                ],
            },
            snapshot_capture_failed=False,
        )

    checkpointer.aput.assert_awaited_once()
    checkpointer.aput_writes.assert_not_awaited()


@pytest.mark.anyio
async def test_rollback_propagates_aput_writes_failure():
    """If aput_writes fails, the exception should propagate (not be swallowed)."""
    checkpointer = FakeCheckpointer(put_result={"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "restored-1"}})
    # Simulate aput_writes failure
    checkpointer.aput_writes.side_effect = RuntimeError("Database connection lost")

    with pytest.raises(RuntimeError, match="Database connection lost"):
        await _rollback_to_pre_run_checkpoint(
            checkpointer=checkpointer,
            thread_id="thread-1",
            run_id="run-1",
            pre_run_checkpoint_id="ckpt-1",
            pre_run_snapshot={
                "checkpoint_ns": "",
                "checkpoint": {"id": "ckpt-1", "channel_versions": {}},
                "metadata": {},
                "pending_writes": [
                    ("task-a", "messages", "value"),
                ],
            },
            snapshot_capture_failed=False,
        )

    # aput succeeded, aput_writes was called but failed
    checkpointer.aput.assert_awaited_once()
    checkpointer.aput_writes.assert_awaited_once()


def test_agent_factory_supports_app_config_detects_supported_signature():
    def factory(*, config, app_config=None):
        return (config, app_config)

    assert _agent_factory_supports_app_config(factory) is True


def test_build_runtime_context_defaults_to_thread_and_run_id():
    ctx = _build_runtime_context("thread-1", "run-1", None)
    assert ctx == {"thread_id": "thread-1", "run_id": "run-1"}


def test_build_runtime_context_merges_caller_context():
    """Regression for issue #2677: keys from ``config['context']`` (e.g. ``agent_name``)
    must be merged into the Runtime's context so that ``ToolRuntime.context`` — which
    is what ``setup_agent`` reads — can see them. Server-owned keys are excluded."""
    caller_context = {"agent_name": "my-agent", "is_bootstrap": True, "model_name": "gpt-4"}

    ctx = _build_runtime_context("thread-1", "run-1", caller_context)

    assert ctx["thread_id"] == "thread-1"
    assert ctx["run_id"] == "run-1"
    assert ctx["agent_name"] == "my-agent"
    assert ctx["is_bootstrap"] is True
    assert "model_name" not in ctx


def test_build_runtime_context_caller_cannot_override_thread_id_or_run_id():
    """A malicious or buggy caller must not be able to overwrite the worker-assigned
    ``thread_id`` / ``run_id`` by stuffing them into ``config['context']``."""
    caller_context = {"thread_id": "spoofed", "run_id": "spoofed", "agent_name": "ok"}

    ctx = _build_runtime_context("real-thread", "real-run", caller_context)

    assert ctx["thread_id"] == "real-thread"
    assert ctx["run_id"] == "real-run"
    assert ctx["agent_name"] == "ok"


def test_build_runtime_context_ignores_non_dict_caller_context():
    ctx = _build_runtime_context("thread-1", "run-1", "not-a-dict")
    assert ctx == {"thread_id": "thread-1", "run_id": "run-1"}


def test_agent_factory_supports_app_config_returns_false_when_signature_lookup_fails(monkeypatch):
    class BrokenCallable:
        def __call__(self, **kwargs):
            return kwargs

    monkeypatch.setattr("deerflow.runtime.runs.worker.inspect.signature", lambda _obj: (_ for _ in ()).throw(ValueError("boom")))

    assert _agent_factory_supports_app_config(BrokenCallable()) is False


def test_extract_llm_error_fallback_returns_fresh_marker_alongside_stale_history():
    """Stale history is ignored, but a brand-new fallback in the same chunk is reported."""
    state = {
        "messages": [
            AIMessage(id="stale-1", content="Hi"),
            AIMessage(
                id="stale-fallback",
                content="Old failure.",
                additional_kwargs={
                    "deerflow_error_fallback": True,
                    "error_detail": "Old error.",
                },
            ),
            AIMessage(
                id="fresh-fallback",
                content="New failure.",
                additional_kwargs={
                    "deerflow_error_fallback": True,
                    "error_detail": "Fresh error.",
                },
            ),
        ]
    }

    fallback = _extract_llm_error_fallback(
        state,
        {"stale-1", "stale-fallback"},
    )

    assert fallback is not None
    assert fallback.message == "Fresh error."


def test_collect_pre_existing_message_ids_pulls_ids_from_snapshot():
    snapshot = {
        "checkpoint": {
            "channel_values": {
                "messages": [
                    AIMessage(id="a", content="x"),
                    AIMessage(id="b", content="y"),
                    AIMessage(content="no-id-here"),  # ignored
                ]
            }
        }
    }
    assert _collect_pre_existing_message_ids(snapshot) == {"a", "b"}


def test_collect_pre_existing_message_ids_handles_missing_pieces():
    assert _collect_pre_existing_message_ids(None) == set()
    assert _collect_pre_existing_message_ids({}) == set()
    assert _collect_pre_existing_message_ids({"checkpoint": None}) == set()
    assert _collect_pre_existing_message_ids({"checkpoint": {}}) == set()
    assert _collect_pre_existing_message_ids({"checkpoint": {"channel_values": None}}) == set()
    assert _collect_pre_existing_message_ids({"checkpoint": {"channel_values": {"messages": None}}}) == set()


@pytest.mark.anyio
async def test_run_agent_ignores_stale_llm_error_fallback_from_prior_run():
    """A stale fallback marker checkpointed by an earlier run on the same thread
    must NOT cause a successful current run to be reported as ``error``.

    This guards against the regression where one IndexError-driven failure (now
    classified transient and surfaced as a ``deerflow_error_fallback`` AIMessage)
    persisted in thread history and tripped ``RunStatus.error`` on every
    subsequent run that re-played the messages channel via ``stream_mode="values"``.
    """
    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )

    stale_fallback = AIMessage(
        id="stale-fallback",
        content="Old failure.",
        additional_kwargs={
            "deerflow_error_fallback": True,
            "error_type": "IndexError",
            "error_reason": "transient",
            "error_detail": "list index out of range",
        },
    )

    class StaleHistoryCheckpointer:
        async def aget_tuple(self, config):
            checkpoint = empty_checkpoint()
            checkpoint["id"] = "ckpt-stale"
            checkpoint["channel_values"] = {"messages": [stale_fallback]}
            return SimpleNamespace(
                config={"configurable": {"thread_id": "thread-1", "checkpoint_ns": "", "checkpoint_id": "ckpt-stale"}},
                checkpoint=checkpoint,
                metadata={},
                pending_writes=[],
            )

    class DummyAgent:
        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            # Replay the prior fallback message (as LangGraph would when using
            # stream_mode="values") and then yield a fresh successful AIMessage.
            yield {
                "messages": [
                    stale_fallback,
                    AIMessage(id="fresh-ok", content="Hello — the run succeeded."),
                ]
            }

    def factory(*, config):
        return DummyAgent()

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=StaleHistoryCheckpointer()),
        agent_factory=factory,
        graph_input={},
        config={},
    )

    fetched = await run_manager.get(record.run_id)
    assert fetched is not None
    assert fetched.status == RunStatus.success, f"Stale fallback marker from prior run should not flip current run to error, got status={fetched.status} error={fetched.error!r}"
    bridge.publish_end.assert_awaited_once_with(record.run_id)


@pytest.mark.anyio
async def test_interrupted_run_never_generates_an_automatic_title(monkeypatch):
    """Only a successful first exchange may trigger automatic title generation."""
    from deerflow.agents.middlewares.title_middleware import TitleMiddleware

    generate = MagicMock(return_value={"title": "Must not be used"})
    monkeypatch.setattr(TitleMiddleware, "_generate_title_result", generate)

    run_manager = RunManager()
    record = await run_manager.create("thread-1")
    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    checkpointer = SimpleNamespace(
        aget_tuple=AsyncMock(return_value=None),
        aput=AsyncMock(),
    )

    class _AbortingAgent:
        metadata = {"model_name": FAKE_TEST_MODEL_REF}
        checkpointer: Any | None = None
        store: Any | None = None
        interrupt_before_nodes = None
        interrupt_after_nodes = None

        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            del graph_input, config, stream_mode, subgraphs
            record.abort_event.set()
            if False:
                yield  # pragma: no cover

    def factory(*, config):
        del config
        return _AbortingAgent()

    await run_agent(
        bridge,
        run_manager,
        record,
        ctx=RunContext(checkpointer=checkpointer),
        agent_factory=factory,
        graph_input={"messages": [{"role": "user", "content": "old prompt"}]},
        config={},
    )

    assert record.status == RunStatus.interrupted
    generate.assert_not_called()
    checkpointer.aput.assert_not_awaited()


@pytest.mark.anyio
async def test_finalizing_run_only_blocks_reject_strategy():
    """A finalizing run must not break interrupt/rollback superseding semantics."""

    async def _seed_finalizing_run():
        run_manager = RunManager()
        record = await run_manager.create("thread-1")
        release_cleanup = asyncio.Event()
        cleanup_cancelled = asyncio.Event()

        async def _cleanup_task():
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                cleanup_cancelled.set()
                raise

        task = asyncio.create_task(_cleanup_task())
        record.task = task
        await run_manager.set_status(record.run_id, RunStatus.interrupted)
        await run_manager.set_finalizing(record.run_id, True)
        return run_manager, record, task, release_cleanup, cleanup_cancelled

    for strategy in ("interrupt", "rollback"):
        run_manager, record, task, release_cleanup, cleanup_cancelled = await _seed_finalizing_run()
        try:
            replacement = await run_manager.create_or_reject("thread-1", multitask_strategy=strategy)
            await asyncio.sleep(0)

            assert replacement.run_id != record.run_id
            assert record.status == RunStatus.interrupted
            assert record.finalizing is True
            assert not cleanup_cancelled.is_set()
            assert not task.done()
        finally:
            release_cleanup.set()
            with suppress(asyncio.CancelledError):
                await task

    run_manager, _record, task, release_cleanup, _cleanup_cancelled = await _seed_finalizing_run()
    try:
        with pytest.raises(ConflictError, match="active run"):
            await run_manager.create_or_reject("thread-1", multitask_strategy="reject")
    finally:
        release_cleanup.set()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_replacement_run_waits_for_prior_finalizing_run():
    """Replacement workers must not enter the graph while an older run is finalizing."""
    run_manager = RunManager()
    old_record = await run_manager.create("thread-1")
    replacement_record = await run_manager.create("thread-1")
    await run_manager.set_finalizing(old_record.run_id, True)

    bridge = SimpleNamespace(
        publish=AsyncMock(),
        publish_end=AsyncMock(),
        cleanup=AsyncMock(),
    )
    replacement_started = asyncio.Event()

    class _ReplacementAgent:
        metadata = {"model_name": FAKE_TEST_MODEL_REF}
        checkpointer: Any | None = None
        store: Any | None = None
        interrupt_before_nodes = None
        interrupt_after_nodes = None

        async def astream(self, graph_input, config=None, stream_mode=None, subgraphs=False):
            del graph_input, config, stream_mode, subgraphs
            replacement_started.set()
            if False:
                yield  # pragma: no cover

    def factory(*, config):
        del config
        return _ReplacementAgent()

    task = asyncio.create_task(
        run_agent(
            bridge,
            run_manager,
            replacement_record,
            ctx=RunContext(checkpointer=None),
            agent_factory=factory,
            graph_input={"messages": [{"role": "user", "content": "Replacement prompt"}]},
            config={},
        )
    )
    replacement_record.task = task

    try:
        await asyncio.sleep(0.1)
        assert not replacement_started.is_set()

        await run_manager.set_finalizing(old_record.run_id, False)
        await asyncio.wait_for(replacement_started.wait(), timeout=1.0)
        await task
    finally:
        await run_manager.set_finalizing(old_record.run_id, False)
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
