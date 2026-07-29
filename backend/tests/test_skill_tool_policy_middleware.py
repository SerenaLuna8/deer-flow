from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command

from deerflow.private_scope import PrivateResourceScope
from deerflow.runtime.secret_context import (
    SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY,
    read_slash_skill_source_path,
    write_slash_skill_source_path,
)
from deerflow.runtime.skill_context_authority import (
    VERIFIED_SKILL_SOURCE_CONTEXT_KEY,
    read_verified_skill_source_paths,
    write_verified_skill_source_path,
)
from deerflow.skills.types import Skill, SkillCategory

_CONTAINER_ROOT = "/mnt/skills"
_SLASH_SOURCE_OWNER_TOKEN = "test-slash-source-owner"


class NamedTool:
    def __init__(self, name: str):
        self.name = name


class ModelRequestStub:
    def __init__(self, tools, *, state=None, context=None, messages=None):
        self.tools = tools
        self.state = state or {}
        self.runtime = SimpleNamespace(context={} if context is None else context)
        self.messages = messages or []

    def override(self, **updates):
        return ModelRequestStub(
            updates.get("tools", self.tools),
            state=updates.get("state", self.state),
            context=self.runtime.context,
            messages=updates.get("messages", self.messages),
        )


class ToolRequestStub:
    def __init__(self, name: str, *, state=None, context=None, args=None):
        self.tool_call = {
            "name": name,
            "id": "call-1",
            "args": args or {},
        }
        self.state = state or {}
        self.runtime = SimpleNamespace(context={} if context is None else context)


def _skill(
    name: str,
    allowed_tools: list[str] | None,
    *,
    enabled: bool = True,
    relative_path: str | None = None,
) -> Skill:
    skill_dir = Path(f"/run/exact-skills/{relative_path or name}")
    return Skill(
        name=name,
        description=f"Description for {name}",
        license="MIT",
        skill_dir=skill_dir,
        skill_file=skill_dir / "SKILL.md",
        relative_path=Path(relative_path or name),
        category=SkillCategory.CUSTOM,
        allowed_tools=None if allowed_tools is None else tuple(allowed_tools),
        enabled=enabled,
        runtime_read_only=True,
    )


def _middleware(skills: list[Skill], *, available_skills: set[str] | None = None):
    from deerflow.agents.middlewares.skill_tool_policy_middleware import (
        SkillToolPolicyMiddleware,
    )

    return SkillToolPolicyMiddleware(
        runtime_skills=tuple(skills),
        runtime_skill_version_ids=tuple(f"exact-version-{index}-{skill.name}" for index, skill in enumerate(skills)),
        runtime_skills_container_path=_CONTAINER_ROOT,
        available_skills=available_skills,
        slash_source_owner_token=_SLASH_SOURCE_OWNER_TOKEN,
    )


def _tool_names(request) -> list[str]:
    return [tool.name for tool in request.tools]


def _active_state(*skills: Skill) -> dict:
    return {
        "skill_context": [
            {
                "name": skill.name,
                "path": skill.get_container_file_path(_CONTAINER_ROOT),
            }
            for skill in skills
        ]
    }


def _private_context() -> dict:
    return {
        "run_id": "run-1",
        "private_scope": PrivateResourceScope(
            project_id="project-1",
            owner_user_id="owner-1",
            membership_version=1,
        ),
    }


def _active_context(*paths: str) -> dict:
    context = _private_context()
    for path in paths:
        assert write_verified_skill_source_path(
            context,
            path,
            owner_token=_SLASH_SOURCE_OWNER_TOKEN,
        )
    return context


def test_caller_supplied_skill_context_is_observational_not_authority():
    restrictive = _skill("restrictive", ["calc"])
    middleware = _middleware([restrictive])
    request = ModelRequestStub(
        [NamedTool("calc"), NamedTool("bash")],
        state=_active_state(restrictive),
        context=_private_context(),
    )

    assert _tool_names(middleware.wrap_model_call(request, lambda filtered: filtered)) == ["calc", "bash"]


def test_successful_paired_read_creates_run_scoped_active_evidence():
    restrictive = _skill("restrictive", ["calc"])
    middleware = _middleware([restrictive])
    context = _private_context()
    exact_path = restrictive.get_container_file_path(_CONTAINER_ROOT)

    result = middleware.wrap_tool_call(
        ToolRequestStub(
            "read_file",
            context=context,
            args={"path": exact_path},
        ),
        lambda _request: ToolMessage(
            content="# Restrictive",
            tool_call_id="call-1",
            name="read_file",
        ),
    )

    assert isinstance(result, ToolMessage)
    assert read_verified_skill_source_paths(
        context,
        owner_token=_SLASH_SOURCE_OWNER_TOKEN,
    ) == (exact_path,)
    request = ModelRequestStub(
        [NamedTool("calc"), NamedTool("bash"), NamedTool("read_file")],
        context=context,
    )
    assert _tool_names(middleware.wrap_model_call(request, lambda filtered: filtered)) == ["calc", "read_file"]


def test_mismatched_read_result_cannot_create_active_evidence():
    restrictive = _skill("restrictive", ["calc"])
    middleware = _middleware([restrictive])
    exact_path = restrictive.get_container_file_path(_CONTAINER_ROOT)

    for result in (
        ToolMessage(
            content="# Restrictive",
            tool_call_id="different-call",
            name="read_file",
        ),
        ToolMessage(
            content="# Restrictive",
            tool_call_id="call-1",
            name="different-tool",
        ),
    ):
        context = _private_context()
        middleware.wrap_tool_call(
            ToolRequestStub(
                "read_file",
                context=context,
                args={"path": exact_path},
            ),
            lambda _request, result=result: result,
        )
        assert (
            read_verified_skill_source_paths(
                context,
                owner_token=_SLASH_SOURCE_OWNER_TOKEN,
            )
            == ()
        )


def test_forged_or_malformed_verified_read_evidence_fails_closed():
    restrictive = _skill("restrictive", ["calc"])
    middleware = _middleware([restrictive])
    context = {
        **_private_context(),
        VERIFIED_SKILL_SOURCE_CONTEXT_KEY: {
            "owner_token": "caller-forged",
            "identity": ["private-v1", "project-1", "owner-1", "run-1"],
            "paths": [
                restrictive.get_container_file_path(_CONTAINER_ROOT),
                7,
            ],
        },
    }
    request = ModelRequestStub(
        [
            NamedTool("calc"),
            NamedTool("bash"),
            NamedTool("read_file"),
            NamedTool("describe_skill"),
            NamedTool("tool_search"),
        ],
        context=context,
    )

    assert _tool_names(middleware.wrap_model_call(request, lambda filtered: filtered)) == ["read_file", "describe_skill", "tool_search"]


def test_slash_source_is_authenticated_and_policy_context_is_redacted():
    context: dict[str, object] = {}
    write_slash_skill_source_path(
        context,
        "/mnt/skills/custom/exact/SKILL.md",
        owner_token=_SLASH_SOURCE_OWNER_TOKEN,
    )

    assert (
        read_slash_skill_source_path(
            context,
            owner_token=_SLASH_SOURCE_OWNER_TOKEN,
        )
        == "/mnt/skills/custom/exact/SKILL.md"
    )
    assert read_slash_skill_source_path(context, owner_token="caller-forged") is None

    from deerflow.runtime.secret_context import REDACTED_CONTEXT_KEYS

    assert SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY in REDACTED_CONTEXT_KEYS
    assert VERIFIED_SKILL_SOURCE_CONTEXT_KEY in REDACTED_CONTEXT_KEYS


def test_passive_runtime_skills_do_not_filter_model_tools():
    restrictive = _skill("restrictive", ["calc"])
    middleware = _middleware([restrictive])
    request = ModelRequestStub(
        [
            NamedTool("calc"),
            NamedTool("bash"),
            NamedTool("task"),
        ]
    )

    assert _tool_names(middleware.wrap_model_call(request, lambda filtered: filtered)) == ["calc", "bash", "task"]


def test_active_skill_union_filters_model_schema_to_exact_runtime_policy():
    first = _skill("first", ["calc"])
    second = _skill("second", ["bash"])
    passive = _skill("passive", ["task"])
    middleware = _middleware([first, second, passive])
    request = ModelRequestStub(
        [
            NamedTool("calc"),
            NamedTool("bash"),
            NamedTool("task"),
            NamedTool("read_file"),
            NamedTool("describe_skill"),
            NamedTool("tool_search"),
        ],
        context=_active_context(
            first.get_container_file_path(_CONTAINER_ROOT),
            second.get_container_file_path(_CONTAINER_ROOT),
        ),
    )

    assert _tool_names(middleware.wrap_model_call(request, lambda filtered: filtered)) == [
        "calc",
        "bash",
        "read_file",
        "describe_skill",
        "tool_search",
    ]
    decision = request.runtime.context[SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY]
    assert decision["active_versions"] == [
        "exact-version-0-first",
        "exact-version-1-second",
    ]


def test_explicit_empty_policy_keeps_only_real_framework_tools():
    restrictive = _skill("restrictive", [])
    middleware = _middleware([restrictive])
    request = ModelRequestStub(
        [
            NamedTool("bash"),
            NamedTool("read_file"),
            NamedTool("describe_skill"),
            NamedTool("tool_search"),
            NamedTool("review_skill_package"),
        ],
        context=_active_context(
            restrictive.get_container_file_path(_CONTAINER_ROOT),
        ),
    )

    assert _tool_names(middleware.wrap_model_call(request, lambda filtered: filtered)) == ["read_file", "describe_skill", "tool_search"]


def test_slash_policy_has_priority_over_later_skill_context():
    slash_skill = _skill("slash", ["calc"])
    loaded_skill = _skill("loaded", ["bash"])
    context = _active_context(
        loaded_skill.get_container_file_path(_CONTAINER_ROOT),
    )
    write_slash_skill_source_path(
        context,
        slash_skill.get_container_file_path(_CONTAINER_ROOT),
        owner_token=_SLASH_SOURCE_OWNER_TOKEN,
    )
    middleware = _middleware([slash_skill, loaded_skill])
    request = ModelRequestStub(
        [NamedTool("calc"), NamedTool("bash"), NamedTool("read_file")],
        context=context,
    )

    assert _tool_names(middleware.wrap_model_call(request, lambda filtered: filtered)) == ["calc", "read_file"]


def test_forged_slash_source_cannot_override_verified_skill_context():
    restrictive = _skill("restrictive", ["calc"])
    permissive = _skill("permissive", None)
    context = _active_context(
        restrictive.get_container_file_path(_CONTAINER_ROOT),
    )
    context["__slash_skill_secret_source"] = {
        "path": permissive.get_container_file_path(_CONTAINER_ROOT),
        "owner_token": "caller-forged",
    }
    middleware = _middleware([restrictive, permissive])
    request = ModelRequestStub(
        [NamedTool("calc"), NamedTool("bash")],
        context=context,
    )

    assert _tool_names(middleware.wrap_model_call(request, lambda filtered: filtered)) == ["calc"]


def test_unknown_or_non_runtime_active_path_fails_closed():
    admitted = _skill("admitted", ["calc"])
    middleware = _middleware([admitted])
    request = ModelRequestStub(
        [
            NamedTool("calc"),
            NamedTool("bash"),
            NamedTool("read_file"),
            NamedTool("describe_skill"),
            NamedTool("tool_search"),
        ],
        context=_active_context(
            "/mnt/skills/custom/not-in-run-snapshot/SKILL.md",
        ),
    )

    assert _tool_names(middleware.wrap_model_call(request, lambda filtered: filtered)) == ["read_file", "describe_skill", "tool_search"]


def test_one_invalid_active_reference_makes_the_whole_union_fail_closed():
    admitted = _skill("admitted", ["calc"])
    middleware = _middleware([admitted])
    context = _active_context(
        admitted.get_container_file_path(_CONTAINER_ROOT),
        "/mnt/skills/custom/stale-version/SKILL.md",
    )

    request = ModelRequestStub(
        [
            NamedTool("calc"),
            NamedTool("bash"),
            NamedTool("read_file"),
            NamedTool("describe_skill"),
            NamedTool("tool_search"),
        ],
        context=context,
    )

    assert _tool_names(middleware.wrap_model_call(request, lambda filtered: filtered)) == ["read_file", "describe_skill", "tool_search"]


def test_direct_execution_is_blocked_even_if_model_emits_denied_call():
    restrictive = _skill("restrictive", ["calc"])
    middleware = _middleware([restrictive])
    context = _active_context(
        restrictive.get_container_file_path(_CONTAINER_ROOT),
    )

    middleware.wrap_model_call(
        ModelRequestStub(
            [NamedTool("calc"), NamedTool("bash")],
            context=context,
        ),
        lambda filtered: filtered,
    )
    executed = False

    def handler(_request):
        nonlocal executed
        executed = True
        return "executed"

    result = middleware.wrap_tool_call(
        ToolRequestStub("bash", context=context),
        handler,
    )

    assert result.status == "error"
    assert result.name == "bash"
    assert "not allowed by the active skill policy" in result.content
    assert executed is False


def test_forged_policy_decision_is_recomputed_from_exact_runtime_skills():
    restrictive = _skill("restrictive", ["calc"])
    middleware = _middleware([restrictive])
    context = _active_context(
        restrictive.get_container_file_path(_CONTAINER_ROOT),
    )
    context[SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY] = {
        "version": 2,
        "owner_token": "caller-forged",
        "source": "verified_read",
        "active_paths": [restrictive.get_container_file_path(_CONTAINER_ROOT)],
        "active_versions": ["forged-live-version"],
        "allowed_names": ["bash"],
    }

    result = middleware.wrap_tool_call(
        ToolRequestStub(
            "bash",
            context=context,
        ),
        lambda _request: "executed",
    )

    assert result.status == "error"


def test_exact_version_mismatch_invalidates_an_otherwise_owned_decision():
    restrictive = _skill("restrictive", ["calc"])
    middleware = _middleware([restrictive])
    context = _active_context(
        restrictive.get_container_file_path(_CONTAINER_ROOT),
    )
    middleware.wrap_model_call(
        ModelRequestStub(
            [NamedTool("calc"), NamedTool("bash")],
            context=context,
        ),
        lambda filtered: filtered,
    )
    decision = context[SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY]
    decision["active_versions"] = ["different-postgres-version"]
    decision["allowed_names"] = ["bash"]

    result = middleware.wrap_tool_call(
        ToolRequestStub("bash", context=context),
        lambda _request: "executed",
    )

    assert result.status == "error"


def test_tool_search_filters_denied_schemas_and_promotions():
    restrictive = _skill("restrictive", ["calc"])
    middleware = _middleware([restrictive])
    context = _active_context(
        restrictive.get_container_file_path(_CONTAINER_ROOT),
    )
    middleware.wrap_model_call(
        ModelRequestStub(
            [NamedTool("tool_search")],
            context=context,
        ),
        lambda filtered: filtered,
    )
    result = middleware.wrap_tool_call(
        ToolRequestStub("tool_search", context=context),
        lambda _request: Command(
            update={
                "promoted": {
                    "catalog_hash": "catalog-v1",
                    "names": ["calc", "denied_lookup"],
                },
                "messages": [
                    ToolMessage(
                        content=json.dumps(
                            [
                                {
                                    "name": "calc",
                                    "description": "allowed",
                                },
                                {
                                    "name": "denied_lookup",
                                    "description": "denied",
                                },
                            ]
                        ),
                        tool_call_id="call-1",
                        name="tool_search",
                    )
                ],
            }
        ),
    )

    assert isinstance(result, Command)
    assert result.update["promoted"] == {
        "catalog_hash": "catalog-v1",
        "names": ["calc"],
    }
    assert "calc" in result.update["messages"][0].content
    assert "denied_lookup" not in result.update["messages"][0].content


def test_tool_search_fails_closed_on_schema_without_matching_promotion():
    restrictive = _skill("restrictive", ["calc"])
    middleware = _middleware([restrictive])
    context = _active_context(
        restrictive.get_container_file_path(_CONTAINER_ROOT),
    )
    middleware.wrap_model_call(
        ModelRequestStub(
            [NamedTool("tool_search")],
            context=context,
        ),
        lambda filtered: filtered,
    )

    result = middleware.wrap_tool_call(
        ToolRequestStub("tool_search", context=context),
        lambda _request: Command(
            update={
                "promoted": {
                    "catalog_hash": "catalog-v1",
                    "names": [],
                },
                "messages": [
                    ToolMessage(
                        content=json.dumps(
                            [
                                {
                                    "name": "denied_lookup",
                                    "description": "must not leak",
                                }
                            ]
                        ),
                        tool_call_id="call-1",
                        name="tool_search",
                    )
                ],
            }
        ),
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"


def test_activation_and_policy_share_authenticated_slash_source(monkeypatch):
    from deerflow.agents.middlewares.skill_activation_middleware import (
        SkillActivationMiddleware,
        _Activation,
        _ActivationResolution,
    )

    skill = _skill("restrictive", ["calc"])
    activation = _Activation(
        skill_name=skill.name,
        category="custom",
        container_file_path=skill.get_container_file_path(_CONTAINER_ROOT),
        skill_content="# Restrictive",
        content_hash="abc",
        remaining_text="calculate",
        editable=False,
    )
    activation_middleware = SkillActivationMiddleware(
        slash_source_owner_token=_SLASH_SOURCE_OWNER_TOKEN,
    )
    monkeypatch.setattr(
        activation_middleware,
        "_resolve_activation",
        lambda _text: _ActivationResolution(activation=activation),
    )
    policy_middleware = _middleware([skill])
    request = ModelRequestStub(
        [NamedTool("calc"), NamedTool("bash")],
        messages=[HumanMessage(content="/restrictive calculate")],
    )

    filtered = activation_middleware.wrap_model_call(
        request,
        lambda activated: policy_middleware.wrap_model_call(
            activated,
            lambda policy_request: policy_request,
        ),
    )

    assert _tool_names(filtered) == ["calc"]


def test_exact_policy_is_wired_between_activation_and_durable_context(
    monkeypatch,
):
    from deerflow.agents.lead_agent import agent as lead_agent_module
    from deerflow.agents.middlewares.durable_context_middleware import (
        DurableContextMiddleware,
    )
    from deerflow.agents.middlewares.skill_activation_middleware import (
        SkillActivationMiddleware,
    )
    from deerflow.agents.middlewares.skill_tool_policy_middleware import (
        SkillToolPolicyMiddleware,
    )
    from deerflow.config.app_config import AppConfig

    app_config = AppConfig.model_validate(
        {
            "models": [
                {
                    "name": "test-model",
                    "use": "tests.fake:Model",
                    "model": "provider-model",
                }
            ],
            "sandbox": {
                "use": "deerflow.sandbox.local:LocalSandboxProvider",
            },
        }
    )
    restrictive = _skill("restrictive", ["calc"])
    monkeypatch.setattr(
        lead_agent_module,
        "build_lead_runtime_middlewares",
        lambda *, app_config, lazy_init=True: [],
    )
    monkeypatch.setattr(
        lead_agent_module,
        "_create_summarization_middleware",
        lambda *, app_config=None: None,
    )
    monkeypatch.setattr(
        lead_agent_module,
        "_create_todo_list_middleware",
        lambda _is_plan_mode: None,
    )

    middlewares = lead_agent_module.build_middlewares(
        {
            "configurable": {
                "is_plan_mode": False,
                "subagent_enabled": False,
            }
        },
        model_name="test-model",
        app_config=app_config,
        runtime_skills=(restrictive,),
        runtime_skill_version_ids=("postgres-version-1",),
        runtime_skills_container_path=_CONTAINER_ROOT,
    )

    activation_index = next(index for index, middleware in enumerate(middlewares) if isinstance(middleware, SkillActivationMiddleware))
    policy_index = next(index for index, middleware in enumerate(middlewares) if isinstance(middleware, SkillToolPolicyMiddleware))
    durable_index = next(index for index, middleware in enumerate(middlewares) if isinstance(middleware, DurableContextMiddleware))
    assert policy_index == activation_index + 1
    assert durable_index == policy_index + 1
    assert middlewares[activation_index]._slash_source_owner_token == middlewares[policy_index]._slash_source_owner_token
    exact_path = restrictive.get_container_file_path(_CONTAINER_ROOT)
    assert middlewares[policy_index]._registry_by_path[exact_path][1] == ("postgres-version-1")
