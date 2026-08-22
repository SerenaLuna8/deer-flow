"""Clean-process probe for _SubagentGraphRunner runtime-context installation."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from types import MethodType, SimpleNamespace

from langchain_core.messages import ToolMessage

from deerflow.runtime.context_carrier import RuntimeContextCarrier
from deerflow.subagents.change_signal import SubagentChangeSignal
from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.delegated_context import DelegatedRuntimeContextProjection
from deerflow.subagents.executor import _SubagentGraphRunner


class _HostExecutionApprovalPort:
    async def request_host_execution(self, plan):
        del plan
        raise AssertionError("probe must not execute commands")

    async def complete_host_execution(self, approval_id, outcome):
        del approval_id, outcome
        raise AssertionError("probe must not execute commands")


class _Agent:
    def __init__(self) -> None:
        self.context: dict[str, object] | None = None

    async def astream(self, state, *, config, context, stream_mode):
        del state, config, stream_mode
        self.context = context
        yield {
            "messages": [
                ToolMessage(
                    "approval required",
                    tool_call_id="inner-call-1",
                    name="bash",
                    artifact={
                        "host_execution_approval": {
                            "schema_version": 1,
                            "kind": "local_shell",
                            "approval_id": "approval-1",
                            "source_run_id": "run-1",
                            "source_tool_call_id": "inner-call-1",
                        },
                    },
                ),
            ],
        }


async def _run() -> dict[str, object]:
    approval_port = _HostExecutionApprovalPort()
    attribution = {
        "user_id": "user-1",
        "is_subagent": False,
        "authz_attributes": {"roles": ["runner"]},
    }
    secrets = {
        "/mnt/skills/custom/example/SKILL.md": {
            "EXAMPLE_TOKEN": "secret-value",
        },
    }
    projection = DelegatedRuntimeContextProjection(
        _carrier=RuntimeContextCarrier(
            app_config=SimpleNamespace(
                logging=SimpleNamespace(
                    enhance=SimpleNamespace(enabled=True),
                ),
            ),
            thread_id="thread-1",
            run_id="run-1",
            user_id="user-1",
            user_role="runner",
            private_scope=object(),
            file_authority=object(),
            authorization_boundary=object(),
            authorization_checker=object(),
            run_read_only_mounts=(object(),),
            channel_user_id="channel-user-1",
            is_subagent=True,
            trace_id="trace-1",
            guardrail_attribution=attribution,
            skill_scoped_secrets=secrets,
            skill_secret_provider=lambda: None,
            host_execution_approval_port=approval_port,
            host_execution_agent_path=("lead", "subagent:context-probe"),
        ),
        channel_identity_mode="set",
        agent_prompt_bundle=None,
        runtime_skills=(),
    )
    executor = _SubagentGraphRunner(
        config=SubagentConfig(
            name="context-probe",
            description="context carrier probe",
            model="probe-model",
        ),
        tools=[],
        delegated_context=projection,
    )
    agent = _Agent()

    async def build_initial_state(self, task):
        del self, task
        return {"messages": []}, [], None

    def create_agent(self, *args, **kwargs):
        del self, args, kwargs
        return agent

    def consume_guard_stop_reason(self):
        del self
        return None

    executor._build_initial_state = MethodType(  # type: ignore[method-assign]
        build_initial_state,
        executor,
    )
    executor._create_agent = MethodType(  # type: ignore[method-assign]
        create_agent,
        executor,
    )
    executor._consume_guard_stop_reason = MethodType(  # type: ignore[method-assign]
        consume_guard_stop_reason,
        executor,
    )

    result = await executor._aexecute(
        "probe",
        executor._create_lifecycle_result_holder(
            execution_id=uuid.uuid4(),
            changes=SubagentChangeSignal(),
        ),
    )
    assert agent.context is not None
    installed_context = agent.context
    installed_secrets = installed_context["__skill_scoped_secrets"]
    installed_attribution = installed_context["__guardrail_attribution"]

    # Private Lead Runs carry an explicit channel-identity clear. Delegation
    # must preserve that three-state value instead of collapsing None to
    # "absent", which could inherit stale Worker environment state.
    executor._delegated_context = DelegatedRuntimeContextProjection(
        _carrier=replace(projection._carrier, channel_user_id=None),
        channel_identity_mode="unset",
        agent_prompt_bundle=None,
        runtime_skills=(),
    )
    cleared_agent = _Agent()
    agent = cleared_agent
    await executor._aexecute(
        "probe-explicit-clear",
        executor._create_lifecycle_result_holder(
            execution_id=uuid.uuid4(),
            changes=SubagentChangeSignal(),
        ),
    )
    assert cleared_agent.context is not None
    explicit_clear = "channel_user_id" in cleared_agent.context and cleared_agent.context["channel_user_id"] is None
    return {
        "status": result.status.value,
        "keys": sorted(installed_context),
        "is_subagent": installed_context["is_subagent"],
        "guardrail_is_subagent": installed_attribution["is_subagent"],
        "secret_copy": installed_secrets is not secrets and installed_secrets["/mnt/skills/custom/example/SKILL.md"] is not secrets["/mnt/skills/custom/example/SKILL.md"],
        "approval_port_identity": (installed_context["__host_execution_approval_port"] is approval_port),
        "approval_agent_path": installed_context["__host_execution_agent_path"],
        "approval_artifact": result.host_execution_approval_artifact,
        "explicit_channel_identity_clear": explicit_clear,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_run())))
