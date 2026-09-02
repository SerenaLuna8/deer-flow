"""Harness contracts for single-use Local host-execution approval."""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import threading
from types import MappingProxyType, ModuleType, SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.graph import END
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import ValidationError

from deerflow.agents.middlewares.sandbox_audit_middleware import (
    SandboxAuditMiddleware,
)
from deerflow.agents.middlewares.tool_error_handling_middleware import (
    ToolErrorHandlingMiddleware,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import (
    HostExecutionApprovalConfig,
    SandboxConfig,
)
from deerflow.runtime.host_execution_approval import (
    HostExecutionApprovalArtifact,
    HostExecutionApprovalPort,
    HostExecutionApprovalResult,
    HostExecutionFrozenClaim,
    HostExecutionOutcome,
    HostExecutionPlan,
    HostExecutionSkillSecretSource,
)
from deerflow.runtime.runs.runtime_binding import _build_runtime_context
from deerflow.sandbox.exceptions import SandboxRuntimeError
from deerflow.sandbox.security import (
    HostBashExecutionMode,
    is_host_bash_allowed,
    is_host_bash_available,
    resolve_host_bash_execution_mode,
)
from deerflow.sandbox.tooling.host_execution import (
    _host_execution_skill_secret_sources,
    mask_secret_values,
    prepare_local_host_execution,
)
from deerflow.sandbox.tools import bash_tool
from deerflow.subagents.binding import (
    AgentGraphExecutionInputs,
    ConfiguredLeadParentExecutionProfile,
    ParentExecutionBarrier,
    ParentExecutionBinding,
)
from deerflow.subagents.delegated_context import (
    _OwnerLoopHostExecutionApprovalProxy,
)
from deerflow.subagents.registry import get_available_subagent_names
from deerflow.tools.builtins.task_tool import (
    _host_execution_approval_command,
)
from deerflow.tools.tools import get_available_tools


def _sandbox_config(
    *,
    use: str = "deerflow.sandbox.local:LocalSandboxProvider",
    approval_mode: str = "disabled",
    allow_host_bash: bool = False,
) -> SandboxConfig:
    return SandboxConfig(
        use=use,
        allow_host_bash=allow_host_bash,
        host_execution_approval={
            "mode": approval_mode,
            "execution_domain_id": ("test-worker" if approval_mode == "approval_required" else None),
        },
    )


def test_host_execution_masks_overlapping_secrets_longest_first() -> None:
    assert (
        mask_secret_values(
            "token-long token",
            {
                "short": "token",
                "long": "token-long",
            },
        )
        == "[redacted] [redacted]"
    )


def test_host_execution_approval_config_is_bounded_and_mutually_exclusive() -> None:
    config = HostExecutionApprovalConfig()
    assert config.mode == "disabled"
    assert config.request_ttl_seconds == 300
    assert config.max_timeout_seconds == 600

    with pytest.raises(ValidationError, match="mutually exclusive"):
        _sandbox_config(
            approval_mode="approval_required",
            allow_host_bash=True,
        )

    with pytest.raises(ValidationError):
        HostExecutionApprovalConfig(request_ttl_seconds=0)
    with pytest.raises(ValidationError):
        HostExecutionApprovalConfig(max_timeout_seconds=0)


def test_local_approval_rejects_mounts_that_are_not_stable_at_startup(
    tmp_path,
) -> None:
    missing = tmp_path / "created-later"
    with pytest.raises(ValidationError, match="exist at startup"):
        SandboxConfig(
            use="deerflow.sandbox.local:LocalSandboxProvider",
            host_execution_approval={
                "mode": "approval_required",
                "execution_domain_id": "test-worker",
            },
            mounts=[
                {
                    "host_path": str(missing),
                    "container_path": "/mnt/shared",
                },
            ],
        )

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValidationError, match="skills.container_path"):
        AppConfig(
            sandbox=SandboxConfig(
                use="deerflow.sandbox.local:LocalSandboxProvider",
                host_execution_approval={
                    "mode": "approval_required",
                    "execution_domain_id": "test-worker",
                },
                mounts=[
                    {
                        "host_path": str(existing),
                        "container_path": "/mnt/project-skills/custom",
                    },
                ],
            ),
            skills={"container_path": "/mnt/project-skills"},
        )


def test_custom_local_provider_cannot_bypass_mount_stability_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from deerflow.sandbox.local import LocalSandboxProvider

    module_name = "test_custom_local_provider_mount_validation"
    module = ModuleType(module_name)

    class CustomLocalProvider(LocalSandboxProvider):
        pass

    module.CustomLocalProvider = CustomLocalProvider
    monkeypatch.setitem(sys.modules, module_name, module)

    with pytest.raises(ValidationError, match="exist at startup"):
        SandboxConfig(
            use=f"{module_name}:CustomLocalProvider",
            host_execution_approval={
                "mode": "approval_required",
                "execution_domain_id": "test-worker",
            },
            mounts=[
                {
                    "host_path": str(tmp_path / "created-later"),
                    "container_path": "/mnt/shared",
                },
            ],
        )

    existing = tmp_path / "custom-existing"
    existing.mkdir()
    with pytest.raises(ValidationError, match="skills.container_path"):
        AppConfig(
            sandbox=SandboxConfig(
                use=f"{module_name}:CustomLocalProvider",
                host_execution_approval={
                    "mode": "approval_required",
                    "execution_domain_id": "test-worker",
                },
                mounts=[
                    {
                        "host_path": str(existing),
                        "container_path": "/mnt/project-skills/custom",
                    },
                ],
            ),
            skills={"container_path": "/mnt/project-skills"},
        )


def test_host_bash_execution_modes_distinguish_local_approval_and_isolated_direct() -> None:
    local_disabled = SimpleNamespace(sandbox=_sandbox_config())
    local_approval = SimpleNamespace(
        sandbox=_sandbox_config(approval_mode="approval_required"),
    )
    local_legacy = SimpleNamespace(
        sandbox=_sandbox_config(allow_host_bash=True),
    )
    isolated = SimpleNamespace(
        sandbox=_sandbox_config(
            use="deerflow.community.aio_sandbox:AioSandboxProvider",
            approval_mode="approval_required",
        ),
    )

    assert resolve_host_bash_execution_mode(local_disabled) is HostBashExecutionMode.LOCAL_DISABLED
    assert resolve_host_bash_execution_mode(local_approval) is HostBashExecutionMode.LOCAL_APPROVAL_REQUIRED
    assert resolve_host_bash_execution_mode(local_legacy) is HostBashExecutionMode.LOCAL_LEGACY_ALLOW
    assert resolve_host_bash_execution_mode(isolated) is HostBashExecutionMode.ISOLATED_DIRECT

    assert not is_host_bash_available(local_disabled)
    assert is_host_bash_available(local_approval)
    assert is_host_bash_available(local_legacy)
    assert is_host_bash_available(isolated)

    assert not is_host_bash_allowed(local_disabled)
    assert not is_host_bash_allowed(local_approval)
    assert is_host_bash_allowed(local_legacy)
    assert is_host_bash_allowed(isolated)


def test_approval_required_exposes_real_bash_tool_and_bash_subagent() -> None:
    config = AppConfig(
        sandbox=_sandbox_config(approval_mode="approval_required"),
        tools=[
            {
                "name": "bash",
                "group": "bash",
                "use": "deerflow.sandbox.tools:bash_tool",
            },
        ],
    )

    tools = get_available_tools(
        include_mcp=False,
        include_acp=False,
        app_config=config,
    )

    assert "bash" in {tool.name for tool in tools}
    assert "bash" in get_available_subagent_names(app_config=config)


def test_disabled_local_mode_hides_bash_tool_and_bash_subagent() -> None:
    config = AppConfig(
        sandbox=_sandbox_config(),
        tools=[
            {
                "name": "bash",
                "group": "bash",
                "use": "deerflow.sandbox.tools:bash_tool",
            },
        ],
    )

    tools = get_available_tools(
        include_mcp=False,
        include_acp=False,
        app_config=config,
    )

    assert "bash" not in {tool.name for tool in tools}
    assert "bash" not in get_available_subagent_names(app_config=config)


@pytest.mark.asyncio
async def test_high_risk_local_command_reaches_approval_barrier() -> None:
    config = AppConfig(
        sandbox=_sandbox_config(approval_mode="approval_required"),
    )
    runtime = SimpleNamespace(
        context={"app_config": config, "thread_id": "thread-1"},
        config={},
    )
    request = ToolCallRequest(
        tool_call={
            "id": "call-1",
            "name": "bash",
            "args": {"command": "cat /etc/shadow"},
            "type": "tool_call",
        },
        tool=None,
        state={},
        runtime=runtime,
    )
    called = False

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(
            "staged",
            tool_call_id="call-1",
            name="bash",
        )

    result = await SandboxAuditMiddleware().awrap_tool_call(request, handler)

    assert called is True
    assert isinstance(result, ToolMessage)
    assert result.content == "staged"


@pytest.mark.asyncio
async def test_malformed_local_command_still_fails_closed_before_approval() -> None:
    config = AppConfig(
        sandbox=_sandbox_config(approval_mode="approval_required"),
    )
    runtime = SimpleNamespace(
        context={"app_config": config, "thread_id": "thread-1"},
        config={},
    )
    request = ToolCallRequest(
        tool_call={
            "id": "call-1",
            "name": "bash",
            "args": {"command": "bad\x00command"},
            "type": "tool_call",
        },
        tool=None,
        state={},
        runtime=runtime,
    )

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        raise AssertionError("malformed command must not reach approval")

    result = await SandboxAuditMiddleware().awrap_tool_call(request, handler)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sandbox_config", "expected_boundary"),
    [
        (
            _sandbox_config(approval_mode="approval_required"),
            "before_idempotent_tool_call",
        ),
        (
            _sandbox_config(
                use="deerflow.community.aio_sandbox:AioSandboxProvider",
                approval_mode="approval_required",
            ),
            "before_tool_call",
        ),
        (
            _sandbox_config(allow_host_bash=True),
            "before_tool_call",
        ),
    ],
)
async def test_only_local_approval_bash_staging_uses_idempotent_boundary(
    sandbox_config: SandboxConfig,
    expected_boundary: str,
) -> None:
    calls: list[str] = []

    class Boundary:
        async def before_idempotent_tool_call(self) -> None:
            calls.append("before_idempotent_tool_call")

        async def before_tool_call(self) -> None:
            calls.append("before_tool_call")

    config = AppConfig(sandbox=sandbox_config)
    request = ToolCallRequest(
        tool_call={
            "id": "call-boundary",
            "name": "bash",
            "args": {"description": "test", "command": "printf ok"},
            "type": "tool_call",
        },
        tool=bash_tool,
        state={},
        runtime=SimpleNamespace(
            context={
                "app_config": config,
                "__authorization_boundary": Boundary(),
            },
        ),
    )

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            "done",
            tool_call_id="call-boundary",
            name="bash",
        )

    result = await ToolErrorHandlingMiddleware(
        app_config=config,
    ).awrap_tool_call(request, handler)

    assert isinstance(result, ToolMessage)
    assert calls == [expected_boundary]


def _plan(**overrides: object) -> HostExecutionPlan:
    values: dict[str, object] = {
        "source_tool_call_id": "call-1",
        "source_run_id": "run-1",
        "source_thread_id": "thread-1",
        "description": "count text",
        "requested_command": "python /mnt/user-data/workspace/count.py",
        "effective_command": "cd /private/workspace && python /private/workspace/count.py",
        "shell": "/bin/zsh",
        "cwd": "/private/workspace",
        "timeout_seconds": 60,
        "environment_keys": ("TOKEN",),
        "agent_path": ("lead",),
    }
    values.update(overrides)
    return HostExecutionPlan(**values)


def test_execution_digest_binds_execution_shape_not_authority_coordinates() -> None:
    plan = _plan()
    same_execution_different_source = _plan(
        source_tool_call_id="call-2",
        source_run_id="continuation-run",
        source_thread_id="thread-2",
        description="different model prose",
        effective_command="cd /another/run/root && python /another/run/root/count.py",
        cwd="/another/run/root",
    )

    assert plan.execution_digest == same_execution_different_source.execution_digest
    assert plan.execution_digest != _plan(requested_command="python other.py").execution_digest
    assert plan.execution_digest != _plan(timeout_seconds=61).execution_digest
    assert plan.execution_digest != _plan(agent_path=("lead", "subagent:bash")).execution_digest
    assert (
        plan.execution_digest
        != _plan(
            channel_identity_mode="set",
            channel_user_id="channel-user-1",
        ).execution_digest
    )
    assert "count.py" not in repr(plan)


def test_execution_digest_and_private_payload_bind_exact_skill_secret_sources() -> None:
    explicit = HostExecutionSkillSecretSource(
        skill_path="/mnt/skills/demo/SKILL.md",
        secret_names=("TOKEN",),
        explicit=True,
    )
    autonomous = HostExecutionSkillSecretSource(
        skill_path="/mnt/skills/demo/SKILL.md",
        secret_names=("TOKEN",),
        explicit=False,
    )
    plan = _plan(
        skill_secret_sources=(explicit,),
    )

    assert (
        plan.execution_digest
        != _plan(
            skill_secret_sources=(autonomous,),
        ).execution_digest
    )
    restored = HostExecutionPlan.from_private_payload(
        plan.to_private_payload(),
        source_tool_call_id=plan.source_tool_call_id,
        source_run_id=plan.source_run_id,
        source_thread_id=plan.source_thread_id,
    )
    assert restored == plan
    assert "credential" not in repr(plan).lower()

    with pytest.raises(ValueError, match="skill secret source"):
        HostExecutionSkillSecretSource(
            skill_path="/mnt/skills/demo/../other/SKILL.md",
            secret_names=("TOKEN",),
            explicit=True,
        )


def test_skill_secret_source_freezing_rejects_noncanonical_path_and_canonicalizes_declaration_order() -> None:
    assert _host_execution_skill_secret_sources(
        {
            "__active_skill_secret_sources": (
                (
                    "demo",
                    "/mnt/skills/demo/../other/SKILL.md",
                    ("TOKEN",),
                    True,
                ),
                (
                    "demo",
                    "/mnt/skills/demo/SKILL.md",
                    ("Z_TOKEN", "A_TOKEN"),
                    True,
                ),
            ),
        },
    ) == (
        HostExecutionSkillSecretSource(
            skill_path="/mnt/skills/demo/SKILL.md",
            secret_names=("A_TOKEN", "Z_TOKEN"),
            explicit=True,
        ),
    )


def test_execution_plan_binds_exact_channel_identity_state() -> None:
    absent = _plan(environment_keys=())
    cleared = _plan(
        environment_keys=(),
        channel_identity_mode="unset",
    )
    first = _plan(
        environment_keys=(),
        channel_identity_mode="set",
        channel_user_id="channel-user-1",
    )
    second = _plan(
        environment_keys=(),
        channel_identity_mode="set",
        channel_user_id="channel-user-2",
    )

    assert (
        len(
            {
                absent.execution_digest,
                cleared.execution_digest,
                first.execution_digest,
                second.execution_digest,
            },
        )
        == 4
    )
    assert (
        HostExecutionPlan.from_private_payload(
            first.to_private_payload(),
            source_tool_call_id=first.source_tool_call_id,
            source_run_id=first.source_run_id,
            source_thread_id=first.source_thread_id,
        )
        == first
    )

    with pytest.raises(ValueError, match="set channel identity"):
        _plan(channel_identity_mode="set")
    with pytest.raises(ValueError, match="only set channel identity"):
        _plan(
            channel_identity_mode="absent",
            channel_user_id="forged-browser-identity",
        )


def test_plan_rejects_oversized_command_and_tool_call_id_before_staging() -> None:
    # UTF-8 bytes, not Python code points, are the authority boundary.
    with pytest.raises(ValueError, match="requested_command is too long"):
        _plan(requested_command="界" * 21_846)
    with pytest.raises(ValueError, match="source_tool_call_id is too long"):
        _plan(source_tool_call_id="x" * 129)


def test_plan_rejects_bidirectional_command_controls_before_staging() -> None:
    with pytest.raises(ValueError, match="bidirectional control"):
        _plan(requested_command="printf safe # \u202erm -rf workspace")
    with pytest.raises(ValueError, match="bidirectional control"):
        _plan(effective_command="printf safe \u2066hidden\u2069")

    class MustNotResolveSandbox:
        def resolve_command_for_execution(self, _command: str) -> str:
            raise AssertionError("oversized command must fail before path mapping")

    with pytest.raises(SandboxRuntimeError, match="approval limit"):
        prepare_local_host_execution(
            SimpleNamespace(context={}, state={}, tool_call_id="call-1"),
            MustNotResolveSandbox(),
            description="oversized",
            requested_command="界" * 21_846,
        )
    with pytest.raises(SandboxRuntimeError, match="tool call id exceeds"):
        prepare_local_host_execution(
            SimpleNamespace(context={}, state={}, tool_call_id="x" * 129),
            MustNotResolveSandbox(),
            description="oversized id",
            requested_command="printf ok",
        )


def test_private_frozen_plan_round_trip_is_strict_and_digest_stable() -> None:
    plan = _plan(environment_keys=())
    restored = HostExecutionPlan.from_private_payload(
        plan.to_private_payload(),
        source_tool_call_id=plan.source_tool_call_id,
        source_run_id=plan.source_run_id,
        source_thread_id=plan.source_thread_id,
    )

    assert restored == plan
    assert restored.execution_digest == plan.execution_digest

    unexpected = {**plan.to_private_payload(), "unexpected": True}
    with pytest.raises(ValueError, match="invalid frozen"):
        HostExecutionPlan.from_private_payload(
            unexpected,
            source_tool_call_id=plan.source_tool_call_id,
            source_run_id=plan.source_run_id,
            source_thread_id=plan.source_thread_id,
        )


def test_frozen_claim_requires_plan_only_after_durable_claim() -> None:
    plan = _plan(environment_keys=())

    assert HostExecutionFrozenClaim.not_applicable().status == "not_applicable"
    assert HostExecutionFrozenClaim.claimed("approval-1", plan).plan is plan
    replay = HostExecutionFrozenClaim.replay(
        "approval-1",
        plan,
        HostExecutionOutcome(status="finished", exit_code=0, result_text="ok"),
    )
    assert replay.status == "replay"
    assert replay.outcome is not None and replay.outcome.result_text == "ok"
    assert HostExecutionFrozenClaim.denied("asset_closure_changed").reason_code == "asset_closure_changed"

    with pytest.raises(ValueError, match="claimed result requires"):
        HostExecutionFrozenClaim(status="claimed")
    with pytest.raises(ValueError, match="denied result requires"):
        HostExecutionFrozenClaim(status="denied")
    with pytest.raises(ValueError, match="durable receipt"):
        HostExecutionFrozenClaim.replay(
            "approval-1",
            plan,
            HostExecutionOutcome(status="unknown", reason_code="ambiguous"),
        )


def test_approval_artifact_is_a_minimal_owner_private_anchor() -> None:
    artifact = HostExecutionApprovalArtifact(
        approval_id="approval-1",
        source_run_id="run-1",
        source_tool_call_id="call-1",
    )

    assert artifact.to_payload() == {
        "schema_version": 1,
        "kind": "local_shell",
        "approval_id": "approval-1",
        "source_run_id": "run-1",
        "source_tool_call_id": "call-1",
    }
    assert "execution_digest" not in artifact.to_payload()
    assert "command" not in artifact.to_payload()


def test_delegated_approval_anchor_builds_parent_checkpoint_command() -> None:
    artifact = HostExecutionApprovalArtifact(
        approval_id="approval-1",
        source_run_id="run-1",
        source_tool_call_id="inner-call-1",
    )
    command = _host_execution_approval_command(
        tool_call_id="outer-task-call-1",
        artifact=artifact,
        usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        usage_receipt_id="execution-1",
        usage_completeness="final_observed",
        delegated_output_promotions=(
            SimpleNamespace(
                source_path="/mnt/user-data/outputs/pre-approval.txt",
                scratch_path=("/mnt/user-data/workspace/.deerflow/subagents/0123456789abcdef0123456789abcdef/outputs/pre-approval.txt"),
            ),
        ),
    )

    assert command.goto == END
    parent_message = command.update["messages"][0]
    assert parent_message.name == "task"
    assert parent_message.tool_call_id == "outer-task-call-1"
    assert parent_message.artifact == {
        "host_execution_approval": artifact.to_payload(),
    }
    assert parent_message.additional_kwargs == {
        "subagent_token_usage": {
            "input_tokens": 2,
            "output_tokens": 3,
            "total_tokens": 5,
        },
        "subagent_usage_receipt_id": "execution-1",
        "subagent_usage_completeness": "final_observed",
    }
    assert "discarded" in parent_message.content
    assert "/mnt/user-data/outputs/pre-approval.txt" not in parent_message.content
    assert ".deerflow/subagents" not in parent_message.content


def test_approval_result_enforces_pending_artifact_shape() -> None:
    artifact = HostExecutionApprovalArtifact(
        approval_id="approval-1",
        source_run_id="run-1",
        source_tool_call_id="call-1",
    )
    assert HostExecutionApprovalResult.pending(artifact).status == "pending"
    assert HostExecutionApprovalResult.denied("policy_denied").status == "denied"

    with pytest.raises(ValueError, match="pending result requires"):
        HostExecutionApprovalResult(status="pending")
    with pytest.raises(ValueError, match="unsupported"):
        HostExecutionApprovalResult(status="approved", artifact=artifact)  # type: ignore[arg-type]


def test_worker_runtime_installs_opaque_port_and_lead_agent_path() -> None:
    port = _FakeApprovalPort(HostExecutionApprovalResult.denied("unused"))

    context = _build_runtime_context(
        "thread-1",
        "run-1",
        {
            "__host_execution_approval_port": "forged",
            "__host_execution_agent_path": ("forged",),
        },
        host_execution_approval_port=port,
    )

    assert context["__host_execution_approval_port"] is port
    assert context["__host_execution_agent_path"] == ("lead",)


def test_worker_runtime_drops_caller_supplied_channel_identity() -> None:
    context = _build_runtime_context(
        "thread-1",
        "run-1",
        {"channel_user_id": "forged-browser-identity"},
    )

    assert "channel_user_id" not in context


def test_private_worker_runtime_installs_only_server_channel_identity() -> None:
    cleared = _build_runtime_context(
        "thread-1",
        "run-1",
        {"channel_user_id": "forged-browser-identity"},
        private_scope=object(),
    )
    verified = _build_runtime_context(
        "thread-1",
        "run-1",
        {"channel_user_id": "forged-browser-identity"},
        private_scope=object(),
        channel_user_id="verified-channel-user",
    )

    assert "channel_user_id" in cleared
    assert cleared["channel_user_id"] is None
    assert verified["channel_user_id"] == "verified-channel-user"


class _RecordingLocalSandbox:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, str] | None, float | None]] = []

    def resolve_command_for_execution(self, command: str) -> str:
        return command.replace("/mnt/user-data/workspace", "/private/workspace")

    def get_execution_shell(self) -> str:
        return "/bin/zsh"

    def execute_prepared_command(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        self.executed.append((command, env, timeout))
        return "ok"

    def execute_prepared_command_result(
        self,
        command: str,
        *,
        shell: str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> SimpleNamespace:
        assert shell == "/bin/zsh"
        self.executed.append((command, env, timeout))
        return SimpleNamespace(
            output="ok",
            stdout="ok",
            stderr="",
            exit_code=0,
        )


class _FakeApprovalPort:
    def __init__(self, result: HostExecutionApprovalResult) -> None:
        self.result = result
        self.plans: list[HostExecutionPlan] = []
        self.completions: list[tuple[str, object]] = []

    async def request_host_execution(
        self,
        plan: HostExecutionPlan,
    ) -> HostExecutionApprovalResult:
        self.plans.append(plan)
        return self.result

    async def complete_host_execution(
        self,
        approval_id: str,
        outcome: object,
    ) -> None:
        self.completions.append((approval_id, outcome))


def _parent_binding(
    owner_loop: asyncio.AbstractEventLoop,
) -> ParentExecutionBinding:
    return ParentExecutionBinding(
        profile=ConfiguredLeadParentExecutionProfile(
            graph=AgentGraphExecutionInputs(
                model=object(),
                tools=(),
                middleware=(),
                system_prompt=None,
                state_schema=dict,
            ),
            app_config=object(),
            asset_context=None,
            agent_config=None,
            model_name="test-model",
            thinking_enabled=False,
            reasoning_effort=None,
            plan_mode=False,
            subagent_enabled=True,
            agent_name="lead",
            available_skills=None,
        ),
        state=MappingProxyType({}),
        context=MappingProxyType({}),
        config=MappingProxyType({}),
        owner_loop=owner_loop,
        store=None,
        barrier=ParentExecutionBarrier(),
    )


@pytest.mark.asyncio
async def test_subagent_owner_loop_proxy_preserves_typed_approval_port() -> None:
    owner_loop = asyncio.get_running_loop()
    target = _FakeApprovalPort(
        HostExecutionApprovalResult.denied("test_denied"),
    )
    proxy = _OwnerLoopHostExecutionApprovalProxy(
        target,
        _parent_binding(owner_loop),
    )
    plan = HostExecutionPlan(
        source_tool_call_id="call-child-1",
        source_run_id="run-1",
        source_thread_id="thread-1",
        description="child command",
        requested_command="python /mnt/user-data/workspace/child.py",
        effective_command="python /private/workspace/child.py",
        shell="/bin/zsh",
        cwd="/private/workspace",
        timeout_seconds=60,
        agent_path=("lead", "subagent:general-purpose"),
    )

    assert isinstance(proxy, HostExecutionApprovalPort)
    assert (await proxy.request_host_execution(plan)).reason_code == "test_denied"
    outcome = HostExecutionOutcome(status="finished", exit_code=0)
    await proxy.complete_host_execution("approval-child-1", outcome)
    assert target.plans == [plan]
    assert target.completions == [("approval-child-1", outcome)]


def _runtime(
    sandbox_config: SandboxConfig,
    sandbox: _RecordingLocalSandbox,
    port: _FakeApprovalPort | None,
) -> SimpleNamespace:
    context: dict[str, object] = {
        "thread_id": "thread-1",
        "run_id": "run-1",
        "app_config": SimpleNamespace(sandbox=sandbox_config),
        "__host_execution_agent_path": ("lead",),
    }
    if port is not None:
        context["__host_execution_approval_port"] = port
    return SimpleNamespace(
        state={
            "sandbox": {"sandbox_id": "local"},
            "thread_data": {
                "workspace_path": "/private/workspace",
                "uploads_path": "/private/uploads",
                "outputs_path": "/private/outputs",
            },
        },
        context=context,
        config={},
        tool_call_id="call-1",
        _sandbox=sandbox,
    )


@pytest.mark.asyncio
async def test_local_bash_pending_approval_stops_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _RecordingLocalSandbox()
    artifact = HostExecutionApprovalArtifact(
        approval_id="approval-1",
        source_run_id="run-1",
        source_tool_call_id="call-1",
    )
    port = _FakeApprovalPort(HostExecutionApprovalResult.pending(artifact))
    runtime = _runtime(
        _sandbox_config(approval_mode="approval_required"),
        sandbox,
        port,
    )

    async def initialized(_runtime: object) -> _RecordingLocalSandbox:
        return sandbox

    monkeypatch.setattr(
        "deerflow.sandbox.tooling.host_execution.ensure_sandbox_initialized_async",
        initialized,
    )
    monkeypatch.setattr(
        "deerflow.sandbox.tooling.host_execution.ensure_thread_directories_exist",
        lambda _runtime: None,
    )

    result = await bash_tool.coroutine(
        runtime=runtime,
        description="count text",
        command="python /mnt/user-data/workspace/count.py",
    )

    assert isinstance(result, Command)
    assert result.goto == END
    assert sandbox.executed == []
    assert len(port.plans) == 1
    assert port.plans[0].effective_command.startswith("cd /private/workspace")
    messages = result.update["messages"]
    assert len(messages) == 1
    assert isinstance(messages[0], ToolMessage)
    assert messages[0].artifact == {
        "host_execution_approval": artifact.to_payload(),
    }


@pytest.mark.asyncio
async def test_local_bash_plan_freezes_trusted_channel_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _RecordingLocalSandbox()
    artifact = HostExecutionApprovalArtifact(
        approval_id="approval-1",
        source_run_id="run-1",
        source_tool_call_id="call-1",
    )
    port = _FakeApprovalPort(HostExecutionApprovalResult.pending(artifact))
    runtime = _runtime(
        _sandbox_config(approval_mode="approval_required"),
        sandbox,
        port,
    )
    runtime.context["channel_user_id"] = "channel-user-1"

    async def initialized(_runtime: object) -> _RecordingLocalSandbox:
        return sandbox

    monkeypatch.setattr(
        "deerflow.sandbox.tooling.host_execution.ensure_sandbox_initialized_async",
        initialized,
    )
    monkeypatch.setattr(
        "deerflow.sandbox.tooling.host_execution.ensure_thread_directories_exist",
        lambda _runtime: None,
    )

    result = await bash_tool.coroutine(
        runtime=runtime,
        description="count text",
        command="python /mnt/user-data/workspace/count.py",
    )

    assert isinstance(result, Command)
    assert len(port.plans) == 1
    plan = port.plans[0]
    assert plan.channel_identity_mode == "set"
    assert plan.channel_user_id == "channel-user-1"
    assert plan.effective_command.startswith(
        "export ACT_WEAVE_CHANNEL_USER_ID=channel-user-1; ",
    )


@pytest.mark.asyncio
async def test_local_bash_plan_freezes_secret_free_exact_skill_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _RecordingLocalSandbox()
    artifact = HostExecutionApprovalArtifact(
        approval_id="approval-1",
        source_run_id="run-1",
        source_tool_call_id="call-1",
    )
    port = _FakeApprovalPort(HostExecutionApprovalResult.pending(artifact))
    runtime = _runtime(
        _sandbox_config(approval_mode="approval_required"),
        sandbox,
        port,
    )
    runtime.context["private_scope"] = object()
    runtime.context["__active_skill_secret_sources"] = (
        (
            "demo",
            "/mnt/skills/demo/SKILL.md",
            (("provider_key", "TOKEN"),),
            True,
        ),
    )
    carrier = {
        "/mnt/skills/demo/SKILL.md": {
            "provider_key": "credential-exact-v7",
        },
    }

    async def skill_secret_provider(
        _requested: dict[str, frozenset[str]],
    ) -> dict[str, dict[str, str]]:
        return carrier

    runtime.context["__skill_secret_provider"] = skill_secret_provider

    async def initialized(_runtime: object) -> _RecordingLocalSandbox:
        return sandbox

    monkeypatch.setattr(
        "deerflow.sandbox.tooling.host_execution.ensure_sandbox_initialized_async",
        initialized,
    )
    monkeypatch.setattr(
        "deerflow.sandbox.tooling.host_execution.ensure_thread_directories_exist",
        lambda _runtime: None,
    )

    result = await bash_tool.coroutine(
        runtime=runtime,
        description="read exact credential",
        command="python /mnt/skills/demo/run.py",
    )

    assert isinstance(result, Command)
    assert len(port.plans) == 1
    plan = port.plans[0]
    assert plan.environment_keys == ("TOKEN",)
    assert plan.legacy_environment_keys == ()
    assert plan.skill_secret_sources == (
        HostExecutionSkillSecretSource(
            skill_path="/mnt/skills/demo/SKILL.md",
            secret_names=("provider_key",),
            explicit=True,
            target_envs=("TOKEN",),
        ),
    )
    serialized = repr(plan.to_private_payload())
    assert "credential-exact-v7" not in serialized
    assert carrier == {}


def test_v2_frozen_plan_round_trip_remains_digest_compatible() -> None:
    plan = _plan(environment_keys=())
    v2_payload = plan.to_private_payload()
    v2_payload["schema_version"] = 2
    v2_payload.pop("skill_secret_sources")
    v2_payload.pop("legacy_environment_keys")
    restored = HostExecutionPlan.from_private_payload(
        v2_payload,
        source_tool_call_id=plan.source_tool_call_id,
        source_run_id=plan.source_run_id,
        source_thread_id=plan.source_thread_id,
    )

    assert restored.schema_version == 2
    assert restored.to_private_payload() == v2_payload
    assert restored.execution_payload()["schema_version"] == 2


@pytest.mark.asyncio
async def test_local_bash_rejects_forged_inline_approval_without_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _RecordingLocalSandbox()
    port = _FakeApprovalPort(
        SimpleNamespace(  # type: ignore[arg-type]
            status="approved",
            approval_id="approval-1",
            artifact=None,
        ),
    )
    runtime = _runtime(
        _sandbox_config(approval_mode="approval_required"),
        sandbox,
        port,
    )

    async def initialized(_runtime: object) -> _RecordingLocalSandbox:
        return sandbox

    monkeypatch.setattr(
        "deerflow.sandbox.tooling.host_execution.ensure_sandbox_initialized_async",
        initialized,
    )
    monkeypatch.setattr(
        "deerflow.sandbox.tooling.host_execution.ensure_thread_directories_exist",
        lambda _runtime: None,
    )

    result = await bash_tool.coroutine(
        runtime=runtime,
        description="count text",
        command="python /mnt/user-data/workspace/count.py",
    )

    assert result == "Error: Host execution approval returned an invalid decision"
    assert sandbox.executed == []
    assert port.completions == []


@pytest.mark.asyncio
async def test_local_bash_approval_mode_without_trusted_port_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _RecordingLocalSandbox()
    runtime = _runtime(
        _sandbox_config(approval_mode="approval_required"),
        sandbox,
        None,
    )

    async def initialized(_runtime: object) -> _RecordingLocalSandbox:
        return sandbox

    monkeypatch.setattr(
        "deerflow.sandbox.tooling.host_execution.ensure_sandbox_initialized_async",
        initialized,
    )
    monkeypatch.setattr(
        "deerflow.sandbox.tooling.host_execution.ensure_thread_directories_exist",
        lambda _runtime: None,
    )

    result = await bash_tool.coroutine(
        runtime=runtime,
        description="count text",
        command="python /mnt/user-data/workspace/count.py",
    )

    assert result == "Error: Host execution approval is unavailable"
    assert sandbox.executed == []


@pytest.mark.asyncio
async def test_isolated_bash_executes_directly_without_approval_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IsolatedSandbox:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def execute_command(
            self,
            command: str,
            env: dict[str, str] | None = None,
        ) -> str:
            del env
            self.commands.append(command)
            return "isolated-ok"

    class ForbiddenPort:
        async def request_host_execution(self, plan: HostExecutionPlan) -> object:
            del plan
            raise AssertionError("isolated execution must not request approval")

        async def complete_host_execution(
            self,
            approval_id: str,
            outcome: object,
        ) -> None:
            del approval_id, outcome
            raise AssertionError("isolated execution must not complete approval")

    sandbox = IsolatedSandbox()
    config = _sandbox_config(
        use="deerflow.community.aio_sandbox:AioSandboxProvider",
        approval_mode="approval_required",
    )
    runtime = SimpleNamespace(
        state={
            "sandbox": {"sandbox_id": "aio:one"},
            "thread_data": {
                "workspace_path": "/private/workspace",
                "uploads_path": "/private/uploads",
                "outputs_path": "/private/outputs",
            },
        },
        context={
            "thread_id": "thread-1",
            "run_id": "run-1",
            "app_config": SimpleNamespace(sandbox=config),
            "__host_execution_approval_port": ForbiddenPort(),
        },
        config={},
        tool_call_id="call-1",
    )

    async def initialized(_runtime: object) -> IsolatedSandbox:
        return sandbox

    monkeypatch.setattr(
        "deerflow.sandbox.tooling.runtime.ensure_sandbox_initialized_async",
        initialized,
    )
    monkeypatch.setattr(
        "deerflow.sandbox.tooling.bash.ensure_sandbox_initialized",
        lambda _runtime: sandbox,
    )
    monkeypatch.setattr(
        "deerflow.sandbox.tooling.bash.ensure_thread_directories_exist",
        lambda _runtime: None,
    )

    result = await bash_tool.coroutine(
        runtime=runtime,
        description="run isolated",
        command="printf ok",
    )

    assert result == "isolated-ok"
    assert sandbox.commands == [
        "cd -- /mnt/user-data/workspace && printf ok",
    ]


@pytest.mark.asyncio
async def test_aio_async_bash_materializes_exact_skill_secret_per_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IsolatedSandbox:
        def __init__(self) -> None:
            self.envs: list[dict[str, str] | None] = []
            self.last_env_reference: dict[str, str] | None = None

        def execute_command(
            self,
            _command: str,
            env: dict[str, str] | None = None,
        ) -> str:
            self.last_env_reference = env
            self.envs.append(dict(env) if env is not None else None)
            if env is None:
                return "missing"
            return "target-only" if set(env) == {"TARGET_API_KEY"} and env["TARGET_API_KEY"] == "provider-secret-value" else "unexpected-environment"

    sandbox = IsolatedSandbox()
    config = _sandbox_config(
        use="deerflow.community.aio_sandbox:AioSandboxProvider",
        approval_mode="approval_required",
    )
    carrier = {
        "/mnt/skills/demo/SKILL.md": {
            "TARGET_API_KEY": "provider-secret-value",
        },
    }
    calls: list[dict[str, frozenset[str]]] = []

    async def skill_secret_provider(
        requested: dict[str, frozenset[str]],
    ) -> dict[str, dict[str, str]]:
        calls.append(requested)
        return carrier

    context = {
        "thread_id": "thread-1",
        "run_id": "run-1",
        "private_scope": object(),
        "app_config": SimpleNamespace(sandbox=config),
        "__skill_secret_provider": skill_secret_provider,
        "__active_skill_secret_sources": (
            (
                "demo",
                "/mnt/skills/demo/SKILL.md",
                ("TARGET_API_KEY",),
                True,
            ),
        ),
    }
    runtime = SimpleNamespace(
        state={"sandbox": {"sandbox_id": "aio:one"}},
        context=context,
        config={},
        tool_call_id="call-1",
    )

    async def initialized(_runtime: object) -> IsolatedSandbox:
        return sandbox

    monkeypatch.setattr(
        "deerflow.sandbox.tooling.runtime.ensure_sandbox_initialized_async",
        initialized,
    )
    monkeypatch.setattr(
        "deerflow.sandbox.tooling.bash.ensure_sandbox_initialized",
        lambda _runtime: sandbox,
    )
    monkeypatch.setattr(
        "deerflow.sandbox.tooling.bash.ensure_thread_directories_exist",
        lambda _runtime: None,
    )

    result = await bash_tool.coroutine(
        runtime=runtime,
        description="read exact credential",
        command='test -n "$TARGET_API_KEY"',
    )

    assert calls == [
        {
            "/mnt/skills/demo/SKILL.md": frozenset({"TARGET_API_KEY"}),
        },
    ]
    assert sandbox.envs == [{"TARGET_API_KEY": "provider-secret-value"}]
    assert "PROVIDER_TOKEN" not in sandbox.envs[0]
    assert "UNRELATED_TOKEN" not in sandbox.envs[0]
    assert sandbox.last_env_reference == {}
    assert carrier == {}
    assert result == "target-only"
    assert "provider-secret-value" not in result
    assert "__active_skill_secrets" not in context
    assert "__skill_secret_exec_ready" not in context


@pytest.mark.asyncio
async def test_aio_bash_refreshes_skill_secret_after_executor_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IsolatedSandbox:
        def __init__(self) -> None:
            self.envs: list[dict[str, str] | None] = []

        def execute_command(
            self,
            _command: str,
            env: dict[str, str] | None = None,
        ) -> str:
            self.envs.append(dict(env) if env is not None else None)
            return "unexpected"

    sandbox = IsolatedSandbox()
    config = _sandbox_config(
        use="deerflow.community.aio_sandbox:AioSandboxProvider",
        approval_mode="approval_required",
    )
    revoked = False
    provider_states: list[bool] = []

    async def skill_secret_provider(
        _requested: dict[str, frozenset[str]],
    ) -> dict[str, dict[str, str]]:
        provider_states.append(revoked)
        if revoked:
            raise RuntimeError("revoked credential detail")
        return {
            "/mnt/skills/demo/SKILL.md": {
                "TOKEN": "credential-before-queue",
            },
        }

    runtime = SimpleNamespace(
        state={"sandbox": {"sandbox_id": "aio:one"}},
        context={
            "thread_id": "thread-1",
            "run_id": "run-1",
            "private_scope": object(),
            "app_config": SimpleNamespace(sandbox=config),
            "__skill_secret_provider": skill_secret_provider,
            "__active_skill_secret_sources": (
                (
                    "demo",
                    "/mnt/skills/demo/SKILL.md",
                    ("TOKEN",),
                    True,
                ),
            ),
        },
        config={},
        tool_call_id="call-1",
    )

    async def initialized(_runtime: object) -> IsolatedSandbox:
        return sandbox

    monkeypatch.setattr(
        "deerflow.sandbox.tooling.runtime.ensure_sandbox_initialized_async",
        initialized,
    )
    monkeypatch.setattr(
        "deerflow.sandbox.tooling.bash.ensure_sandbox_initialized",
        lambda _runtime: sandbox,
    )
    monkeypatch.setattr(
        "deerflow.sandbox.tooling.bash.ensure_thread_directories_exist",
        lambda _runtime: None,
    )

    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    queue_blocked = threading.Event()
    release_queue = threading.Event()

    def occupy_executor() -> None:
        queue_blocked.set()
        release_queue.wait(timeout=2)

    blocker = loop.run_in_executor(None, occupy_executor)
    try:
        while not queue_blocked.is_set():
            await asyncio.sleep(0)
        execution = asyncio.create_task(
            bash_tool.coroutine(
                runtime=runtime,
                description="read exact credential",
                command='printf %s "$TOKEN"',
            )
        )
        await asyncio.sleep(0.1)
        revoked = True
        release_queue.set()
        result = await asyncio.wait_for(execution, timeout=2)

        assert provider_states == [True]
        assert sandbox.envs == []
        assert result == "Error: Skill secret material is unavailable"
        assert "revoked credential detail" not in result
    finally:
        release_queue.set()
        await blocker
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_local_bash_rejects_secret_plaintext_before_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _RecordingLocalSandbox()
    artifact = HostExecutionApprovalArtifact(
        approval_id="approval-1",
        source_run_id="run-1",
        source_tool_call_id="call-1",
    )
    port = _FakeApprovalPort(HostExecutionApprovalResult.pending(artifact))
    runtime = _runtime(
        _sandbox_config(approval_mode="approval_required"),
        sandbox,
        port,
    )
    runtime.context["secrets"] = {"TOKEN": "do-not-persist-this"}

    async def initialized(_runtime: object) -> _RecordingLocalSandbox:
        return sandbox

    monkeypatch.setattr(
        "deerflow.sandbox.tooling.host_execution.ensure_sandbox_initialized_async",
        initialized,
    )
    monkeypatch.setattr(
        "deerflow.sandbox.tooling.host_execution.ensure_thread_directories_exist",
        lambda _runtime: None,
    )

    result = await bash_tool.coroutine(
        runtime=runtime,
        description="unsafe inline secret",
        command="printf do-not-persist-this",
    )

    assert result == "Error: Host command contains secret plaintext and cannot be staged"
    assert port.plans == []
    assert sandbox.executed == []
