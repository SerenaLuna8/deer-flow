"""Delegated runtime-context projection interface contracts."""

from __future__ import annotations

import asyncio
import pickle
from dataclasses import FrozenInstanceError
from types import MappingProxyType, SimpleNamespace

import pytest

from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.host_execution_approval import HostExecutionApprovalPort
from deerflow.runtime.recovered_llm_failures import (
    RunRecoveredLLMFailureRecorder,
    build_recovered_llm_failures_receipt,
)
from deerflow.subagents.binding import (
    AgentGraphExecutionInputs,
    ParentExecutionBarrier,
    ParentExecutionBinding,
    PrivateRunParentExecutionProfile,
    SdkParentExecutionProfile,
)
from deerflow.subagents.delegated_context import (
    DelegatedRuntimeContextProjection,
    project_delegated_runtime_context,
)
from deerflow.token_budget_usage import (
    TokenBudgetUsageRecorder,
    TokenBudgetUsageSnapshot,
)


def _private_binding(
    *,
    context: dict[str, object],
    config: dict[str, object] | None = None,
) -> ParentExecutionBinding:
    profile = PrivateRunParentExecutionProfile(
        graph=AgentGraphExecutionInputs(
            model=object(),
            tools=(),
            middleware=(),
            system_prompt=None,
            state_schema=dict,
        ),
        app_config=SimpleNamespace(name="profile-app-config"),
        asset_context=None,
        private_runtime=object(),
        model_name="private-model",
        thinking_enabled=False,
        reasoning_effort=None,
        runtime_skills=(),
        runtime_agent_catalog=None,
        tool_groups=(),
    )
    return ParentExecutionBinding(
        profile=profile,
        state=MappingProxyType({}),
        context=MappingProxyType(dict(context)),
        config=MappingProxyType(dict(config or {})),
        owner_loop=asyncio.get_running_loop(),
        store=None,
        barrier=ParentExecutionBarrier(),
    )


def _sdk_binding(*, context: dict[str, object]) -> ParentExecutionBinding:
    profile = SdkParentExecutionProfile(
        graph=AgentGraphExecutionInputs(
            model=object(),
            tools=(),
            middleware=(),
            system_prompt=None,
            state_schema=dict,
        ),
        features=None,
        full_middleware_takeover=False,
        plan_mode=False,
        checkpoint_channel_mode="full",
        checkpoint_snapshot_frequency=None,
    )
    return ParentExecutionBinding(
        profile=profile,
        state=MappingProxyType({}),
        context=MappingProxyType(dict(context)),
        config=MappingProxyType({}),
        owner_loop=asyncio.get_running_loop(),
        store=None,
        barrier=ParentExecutionBarrier(),
    )


@pytest.mark.asyncio
async def test_private_projection_owns_identity_copy_tristate_and_exclusions() -> None:
    prompt_bundle = object()
    runtime_skill = object()
    parent_secrets = {
        "/runtime/example/SKILL.md": {
            "EXAMPLE_TOKEN": "private-value",
        },
    }
    binding = _private_binding(
        context={
            RuntimeContextKeys.THREAD_ID: "thread-parent",
            RuntimeContextKeys.RUN_ID: "run-context",
            RuntimeContextKeys.APP_CONFIG: "forged-app-config",
            RuntimeContextKeys.USER_ID: "user-context",
            RuntimeContextKeys.USER_ROLE: "role-context",
            RuntimeContextKeys.OAUTH_PROVIDER: "oauth-context",
            RuntimeContextKeys.OAUTH_ID: "oauth-id-context",
            RuntimeContextKeys.CHANNEL_USER_ID: None,
            RuntimeContextKeys.IS_SUBAGENT: False,
            RuntimeContextKeys.PRIVATE_SCOPE: object(),
            RuntimeContextKeys.GUARDRAIL_ATTRIBUTION: {
                "user_id": "user-issued",
                "user_role": "role-issued",
                "oauth_provider": "oauth-issued",
                "oauth_id": "oauth-id-issued",
                "run_id": "run-issued",
                "is_subagent": False,
                "authz_attributes": {"roles": ["runner"]},
            },
            RuntimeContextKeys.SKILL_SCOPED_SECRETS: parent_secrets,
            RuntimeContextKeys.TRACE_ID: "trace-context",
            RuntimeContextKeys.MEMORY_AUTHORITY: object(),
            RuntimeContextKeys.RUN_JOURNAL: object(),
            RuntimeContextKeys.TOKEN_USAGE_TRACKING_ENABLED: False,
            RuntimeContextKeys.TOKEN_BUDGET_USAGE_RECORDER: (
                TokenBudgetUsageRecorder(
                    TokenBudgetUsageSnapshot.zero("run-issued"),
                )
            ),
            RuntimeContextKeys.RUN_SEMANTIC_STOP_RECORDER: object(),
            RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY: object(),
            "extension_context": "must-not-cross",
        },
        config={
            "metadata": {
                RuntimeContextKeys.TRACE_ID: "trace-metadata",
            },
        },
    )

    projection = project_delegated_runtime_context(
        binding,
        subagent_name="general-purpose",
        fallback_user_id="user-fallback",
        fallback_trace_id="trace-fallback",
        agent_prompt_bundle=prompt_bundle,
        runtime_skills=(runtime_skill,),
    )

    assert type(projection) is DelegatedRuntimeContextProjection
    assert projection.agent_prompt_bundle is prompt_bundle
    assert projection.runtime_skills == (runtime_skill,)
    assert projection.channel_identity_mode == "unset"
    assert projection.app_config is binding.profile.app_config
    assert projection.thread_id == "thread-parent"
    assert projection.run_id == "run-issued"
    assert projection.user_id == "user-issued"
    assert projection.deerflow_trace_id == "trace-context"
    assert projection.token_usage_tracking_enabled is False

    first = projection.build()
    second = projection.build()
    assert first[RuntimeContextKeys.APP_CONFIG] is binding.profile.app_config
    assert first[RuntimeContextKeys.USER_ID] == "user-issued"
    assert first[RuntimeContextKeys.USER_ROLE] == "role-issued"
    assert first[RuntimeContextKeys.OAUTH_PROVIDER] == "oauth-issued"
    assert first[RuntimeContextKeys.OAUTH_ID] == "oauth-id-issued"
    assert first[RuntimeContextKeys.RUN_ID] == "run-issued"
    assert first[RuntimeContextKeys.IS_SUBAGENT] is True
    assert RuntimeContextKeys.CHANNEL_USER_ID in first
    assert first[RuntimeContextKeys.CHANNEL_USER_ID] is None
    assert first[RuntimeContextKeys.GUARDRAIL_ATTRIBUTION]["is_subagent"] is True
    assert first[RuntimeContextKeys.SKILL_SCOPED_SECRETS] == parent_secrets
    assert first[RuntimeContextKeys.SKILL_SCOPED_SECRETS] is not parent_secrets
    assert first[RuntimeContextKeys.SKILL_SCOPED_SECRETS] is not second[RuntimeContextKeys.SKILL_SCOPED_SECRETS]
    assert RuntimeContextKeys.MEMORY_AUTHORITY not in first
    assert RuntimeContextKeys.RUN_JOURNAL not in first
    assert first[RuntimeContextKeys.TOKEN_USAGE_TRACKING_ENABLED] is False
    assert RuntimeContextKeys.TOKEN_BUDGET_USAGE_RECORDER not in first
    assert RuntimeContextKeys.RUN_SEMANTIC_STOP_RECORDER not in first
    assert RuntimeContextKeys.PARENT_EXECUTION_BINDING_FACTORY not in first
    assert "extension_context" not in first


@pytest.mark.asyncio
async def test_private_projection_shares_only_thread_safe_recovered_failure_recorder() -> None:
    recorder = RunRecoveredLLMFailureRecorder()
    journal = object()
    projection = project_delegated_runtime_context(
        _private_binding(
            context={
                RuntimeContextKeys.RUN_JOURNAL: journal,
                RuntimeContextKeys.RECOVERED_LLM_FAILURE_RECORDER: recorder,
            }
        ),
        subagent_name="parallel-delegate",
        fallback_user_id="user-1",
        fallback_trace_id=None,
        agent_prompt_bundle=None,
        runtime_skills=(),
    )

    child_context = projection.build()
    assert RuntimeContextKeys.RUN_JOURNAL not in child_context
    assert child_context[RuntimeContextKeys.RECOVERED_LLM_FAILURE_RECORDER] is recorder

    receipt = build_recovered_llm_failures_receipt(
        (
            {
                "attempt": 1,
                "max_attempts": 3,
                "error_code": "LLM_PROVIDER_UNAVAILABLE",
                "reason": "transient",
                "caller": "subagent",
                "failure_subtype": "connection",
                "status_code": None,
                "disposition": "recovered",
            },
        )
    )
    await asyncio.gather(
        *(
            asyncio.to_thread(
                child_context[RuntimeContextKeys.RECOVERED_LLM_FAILURE_RECORDER].record,
                receipt,
            )
            for _ in range(64)
        )
    )
    assert len(recorder.snapshot()) == 64

    malformed = {
        "schema_version": 2,
        "failures": [
            {
                "attempt": 1,
                "max_attempts": 3,
                "error_code": [],
                "reason": "transient",
                "caller": "subagent",
                "failure_subtype": "connection",
                "status_code": None,
                "disposition": "recovered",
            }
        ],
    }
    assert len(recorder.record(malformed)) == 64
    assert len(recorder.snapshot()) == 64


@pytest.mark.asyncio
async def test_private_projection_adapts_loop_affine_authority_and_prefers_secret_provider() -> None:
    owner_loop = asyncio.get_running_loop()
    calls: list[tuple[str, asyncio.AbstractEventLoop]] = []

    class FileAuthority:
        sandbox_id = "sandbox-1"

        async def write_internal(self, relative_path: str, content: bytes) -> str:
            del relative_path, content
            calls.append(("file", asyncio.get_running_loop()))
            return "file-result"

    class AuthorizationBoundary:
        async def authorize(self, operation: str) -> str:
            del operation
            calls.append(("authorization", asyncio.get_running_loop()))
            return "authorization-result"

    async def authorization_checker() -> str:
        calls.append(("checker", asyncio.get_running_loop()))
        return "checker-result"

    async def skill_secret_provider(path: str) -> str:
        del path
        calls.append(("secret", asyncio.get_running_loop()))
        return "secret-result"

    class ApprovalPort:
        async def request_host_execution(self, plan: object) -> str:
            del plan
            calls.append(("approval", asyncio.get_running_loop()))
            return "approval-result"

        async def complete_host_execution(self, approval_id: str, outcome: object) -> None:
            del approval_id, outcome

    file_authority = FileAuthority()
    authorization_boundary = AuthorizationBoundary()
    approval_port = ApprovalPort()
    assert isinstance(approval_port, HostExecutionApprovalPort)
    mount = object()
    binding = _private_binding(
        context={
            RuntimeContextKeys.PRIVATE_SCOPE: object(),
            RuntimeContextKeys.FILE_AUTHORITY: file_authority,
            RuntimeContextKeys.AUTHORIZATION_BOUNDARY: authorization_boundary,
            RuntimeContextKeys.AUTHORIZATION_CHECKER: authorization_checker,
            RuntimeContextKeys.SKILL_SECRET_PROVIDER: skill_secret_provider,
            RuntimeContextKeys.SKILL_SCOPED_SECRETS: {
                "/runtime/ignored/SKILL.md": {"TOKEN": "must-not-install"},
            },
            RuntimeContextKeys.RUN_READ_ONLY_MOUNTS: (mount,),
            RuntimeContextKeys.HOST_EXECUTION_APPROVAL_PORT: approval_port,
            RuntimeContextKeys.HOST_EXECUTION_AGENT_PATH: ("lead",),
        },
    )
    projection = project_delegated_runtime_context(
        binding,
        subagent_name="delegate",
        fallback_user_id="user-1",
        fallback_trace_id=None,
        agent_prompt_bundle=None,
        runtime_skills=(),
    )
    context = projection.build()

    assert context[RuntimeContextKeys.FILE_AUTHORITY] is not file_authority
    assert context[RuntimeContextKeys.AUTHORIZATION_BOUNDARY] is not authorization_boundary
    assert context[RuntimeContextKeys.AUTHORIZATION_CHECKER] is not authorization_checker
    assert context[RuntimeContextKeys.SKILL_SECRET_PROVIDER] is not skill_secret_provider
    assert context[RuntimeContextKeys.HOST_EXECUTION_APPROVAL_PORT] is not approval_port
    assert context[RuntimeContextKeys.HOST_EXECUTION_AGENT_PATH] == (
        "lead",
        "subagent:delegate",
    )
    assert context[RuntimeContextKeys.RUN_READ_ONLY_MOUNTS] == (mount,)
    assert RuntimeContextKeys.SKILL_SCOPED_SECRETS not in context

    async def invoke_from_child_loop() -> tuple[str, str, str, str, str]:
        return (
            await context[RuntimeContextKeys.FILE_AUTHORITY].write_internal(
                "result.txt",
                b"result",
            ),
            await context[RuntimeContextKeys.AUTHORIZATION_BOUNDARY].authorize(
                "write",
            ),
            await context[RuntimeContextKeys.AUTHORIZATION_CHECKER](),
            await context[RuntimeContextKeys.SKILL_SECRET_PROVIDER]("/skill"),
            await context[RuntimeContextKeys.HOST_EXECUTION_APPROVAL_PORT].request_host_execution(object()),
        )

    results = await asyncio.to_thread(lambda: asyncio.run(invoke_from_child_loop()))
    assert results == (
        "file-result",
        "authorization-result",
        "checker-result",
        "secret-result",
        "approval-result",
    )
    assert [name for name, _loop in calls] == [
        "file",
        "authorization",
        "checker",
        "secret",
        "approval",
    ]
    assert all(loop is owner_loop for _name, loop in calls)


@pytest.mark.asyncio
async def test_sdk_projection_fails_closed_for_private_authority_and_is_opaque() -> None:
    secret_text = "must-not-appear"
    projection = project_delegated_runtime_context(
        _sdk_binding(
            context={
                RuntimeContextKeys.PRIVATE_SCOPE: object(),
                RuntimeContextKeys.FILE_AUTHORITY: object(),
                RuntimeContextKeys.AUTHORIZATION_BOUNDARY: object(),
                RuntimeContextKeys.AUTHORIZATION_CHECKER: lambda: None,
                RuntimeContextKeys.GUARDRAIL_ATTRIBUTION: {
                    "user_id": "forged-private-user",
                },
                RuntimeContextKeys.RUN_READ_ONLY_MOUNTS: (object(),),
                RuntimeContextKeys.SKILL_SCOPED_SECRETS: {
                    "/forged/SKILL.md": {"TOKEN": secret_text},
                },
                RuntimeContextKeys.SKILL_SECRET_PROVIDER: lambda: None,
                RuntimeContextKeys.CHANNEL_USER_ID: "channel-user-1",
            },
        ),
        subagent_name="general-purpose",
        fallback_user_id="sdk-user",
        fallback_trace_id=None,
        agent_prompt_bundle=None,
        runtime_skills=(),
    )

    context = projection.build()
    assert projection.channel_identity_mode == "set"
    assert context[RuntimeContextKeys.CHANNEL_USER_ID] == "channel-user-1"
    assert context[RuntimeContextKeys.USER_ID] == "sdk-user"
    assert RuntimeContextKeys.PRIVATE_SCOPE not in context
    assert RuntimeContextKeys.FILE_AUTHORITY not in context
    assert RuntimeContextKeys.AUTHORIZATION_BOUNDARY not in context
    assert RuntimeContextKeys.AUTHORIZATION_CHECKER not in context
    assert RuntimeContextKeys.GUARDRAIL_ATTRIBUTION not in context
    assert RuntimeContextKeys.RUN_READ_ONLY_MOUNTS not in context
    assert RuntimeContextKeys.SKILL_SCOPED_SECRETS not in context
    assert RuntimeContextKeys.SKILL_SECRET_PROVIDER not in context
    assert secret_text not in repr(projection)
    with pytest.raises(TypeError, match="delegated runtime-context projection"):
        pickle.dumps(projection)
    with pytest.raises(FrozenInstanceError):
        projection.channel_identity_mode = "absent"  # type: ignore[misc]

    absent = project_delegated_runtime_context(
        _sdk_binding(context={}),
        subagent_name="general-purpose",
        fallback_user_id="sdk-user",
        fallback_trace_id=None,
        agent_prompt_bundle=None,
        runtime_skills=(),
    )
    assert absent.channel_identity_mode == "absent"
    assert RuntimeContextKeys.CHANNEL_USER_ID not in absent.build()
