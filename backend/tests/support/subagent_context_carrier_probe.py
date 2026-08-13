"""Clean-process probe for SubagentExecutor runtime-context installation."""

from __future__ import annotations

import asyncio
import json
from types import MethodType, SimpleNamespace

from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.executor import SubagentExecutor


class _Agent:
    def __init__(self) -> None:
        self.context: dict[str, object] | None = None

    async def astream(self, state, *, config, context, stream_mode):
        del state, config, stream_mode
        self.context = context
        yield {"messages": []}


async def _run() -> dict[str, object]:
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
    executor = SubagentExecutor(
        config=SubagentConfig(
            name="context-probe",
            description="context carrier probe",
            model="probe-model",
        ),
        tools=[],
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
        deerflow_trace_id="trace-1",
        guardrail_attribution=attribution,
        skill_scoped_secrets=secrets,
        skill_secret_provider=lambda: None,
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

    result = await executor._aexecute("probe")
    assert agent.context is not None
    installed_secrets = agent.context["__skill_scoped_secrets"]
    installed_attribution = agent.context["__guardrail_attribution"]
    return {
        "status": result.status.value,
        "keys": sorted(agent.context),
        "is_subagent": agent.context["is_subagent"],
        "guardrail_is_subagent": installed_attribution["is_subagent"],
        "secret_copy": installed_secrets is not secrets and installed_secrets["/mnt/skills/custom/example/SKILL.md"] is not secrets["/mnt/skills/custom/example/SKILL.md"],
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_run())))
