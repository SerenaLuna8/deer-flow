"""Verified-read Skill activation TTL (U7).

Evidence entries capture the lead model-call ordinal; the tool policy and the
secret-binding boundary consume the same evidence and expire together. Expiry
only restores the pre-activation default tool set (D10), and slash activation
is exempt because it encodes explicit user intent.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from deerflow.agents.middlewares.skill_activation_middleware import (
    SkillActivationMiddleware,
)
from deerflow.agents.middlewares.skill_tool_policy_middleware import (
    SkillToolPolicyMiddleware,
)
from deerflow.config.app_config import AppConfig
from deerflow.private_scope import PrivateResourceScope
from deerflow.runtime.secret_context import (
    ACTIVE_SECRETS_CONTEXT_KEY,
    write_slash_skill_source_path,
)
from deerflow.runtime.skill_context_authority import (
    LEAD_MODEL_CALL_SEQ_CONTEXT_KEY,
    VERIFIED_SKILL_SOURCE_CONTEXT_KEY,
    advance_lead_model_call_seq,
    read_lead_model_call_seq,
    read_verified_skill_source_entries,
    read_verified_skill_source_paths,
    write_verified_skill_source_path,
)
from deerflow.skills.types import SecretRequirement, Skill, SkillCategory

_CONTAINER = "/skills"
_OWNER_TOKEN = "chain-owner-token"
_DEFAULT_TOOL_NAMES = ("read_file", "grep", "bash", "tool_search", "describe_skill")


def _skill(
    name: str = "restricted",
    *,
    allowed_tools: tuple[str, ...] | None = ("grep",),
    required_secrets: tuple[SecretRequirement, ...] = (),
) -> Skill:
    return Skill(
        name=name,
        description="",
        license=None,
        skill_dir=Path(f"/tmp/{name}"),
        skill_file=Path(f"/tmp/{name}/SKILL.md"),
        relative_path=Path(name),
        category=SkillCategory.PUBLIC,
        allowed_tools=allowed_tools,
        enabled=True,
        required_secrets=required_secrets,
    )


def _context() -> dict:
    return {
        "run_id": "run-1",
        "private_scope": PrivateResourceScope(
            project_id="project-1",
            owner_user_id="user-1",
            membership_version=1,
        ),
    }


def _middleware(skill: Skill, *, ttl: int) -> SkillToolPolicyMiddleware:
    return SkillToolPolicyMiddleware(
        runtime_skills=(skill,),
        runtime_skill_version_ids=("skill-version-1",),
        runtime_skills_container_path=_CONTAINER,
        slash_source_owner_token=_OWNER_TOKEN,
        read_evidence_ttl_calls=ttl,
    )


class _FakeModelRequest:
    def __init__(self, context: dict, tools: list) -> None:
        self.runtime = SimpleNamespace(context=context)
        self.tools = list(tools)

    def override(self, *, tools: list) -> _FakeModelRequest:
        return _FakeModelRequest(self.runtime.context, tools)


def _tools() -> list[SimpleNamespace]:
    return [SimpleNamespace(name=name) for name in _DEFAULT_TOOL_NAMES]


def _tool_request(
    context: dict,
    name: str,
    *,
    tool_call_id: str = "call-1",
    path: str | None = None,
) -> SimpleNamespace:
    args: dict = {"path": path} if path is not None else {}
    return SimpleNamespace(
        runtime=SimpleNamespace(context=context),
        tool_call={"name": name, "args": args, "id": tool_call_id, "type": "tool_call"},
    )


def _read_skill_entry(
    middleware: SkillToolPolicyMiddleware,
    context: dict,
    entry_path: str,
) -> None:
    request = _tool_request(context, "read_file", path=entry_path)
    result = middleware.wrap_tool_call(
        request,
        lambda _request: ToolMessage(
            content="# SKILL.md contents",
            tool_call_id="call-1",
            name="read_file",
        ),
    )
    assert isinstance(result, ToolMessage)
    assert result.status != "error"


def _must_not_execute(_request: object) -> ToolMessage:
    raise AssertionError("the blocked tool must never execute")


def _filtered_names(middleware: SkillToolPolicyMiddleware, context: dict) -> set[str]:
    filtered = middleware._filter_model_request(
        _FakeModelRequest(context, _tools()),
        refresh_decision=True,
    )
    return {tool.name for tool in filtered.tools}


def test_lead_model_call_seq_advances_only_on_well_formed_context() -> None:
    context: dict = {}
    assert read_lead_model_call_seq(context) == 0
    assert advance_lead_model_call_seq(context) == 1
    assert advance_lead_model_call_seq(context) == 2
    assert read_lead_model_call_seq(context) == 2

    assert advance_lead_model_call_seq("not a context") == 0
    assert read_lead_model_call_seq("not a context") == 0

    context[LEAD_MODEL_CALL_SEQ_CONTEXT_KEY] = "forged"
    assert read_lead_model_call_seq(context) == 0
    assert advance_lead_model_call_seq(context) == 1


def test_evidence_entries_capture_seq_and_a_reread_refreshes_the_window() -> None:
    context = _context()
    skill_path = "/skills/public/restricted/SKILL.md"
    advance_lead_model_call_seq(context)
    advance_lead_model_call_seq(context)
    assert write_verified_skill_source_path(context, skill_path, owner_token=_OWNER_TOKEN)
    assert read_verified_skill_source_entries(context, owner_token=_OWNER_TOKEN) == ((skill_path, 2),)

    advance_lead_model_call_seq(context)
    assert read_verified_skill_source_paths(context, owner_token=_OWNER_TOKEN, ttl_calls=1) == ()
    assert read_verified_skill_source_paths(context, owner_token=_OWNER_TOKEN, ttl_calls=2) == (skill_path,)
    assert read_verified_skill_source_paths(context, owner_token=_OWNER_TOKEN, ttl_calls=0) == (skill_path,)

    assert write_verified_skill_source_path(context, skill_path, owner_token=_OWNER_TOKEN)
    assert read_verified_skill_source_entries(context, owner_token=_OWNER_TOKEN) == ((skill_path, 3),)
    assert read_verified_skill_source_paths(context, owner_token=_OWNER_TOKEN, ttl_calls=1) == (skill_path,)


def test_evidence_read_rejects_malformed_entries_and_ttl() -> None:
    context = _context()
    advance_lead_model_call_seq(context)
    assert write_verified_skill_source_path(context, "/skills/public/restricted/SKILL.md", owner_token=_OWNER_TOKEN)

    with pytest.raises(ValueError):
        read_verified_skill_source_paths(context, owner_token=_OWNER_TOKEN, ttl_calls=-1)

    for entries in (
        [],
        [["/skills/public/restricted/SKILL.md", -1]],
        [["/skills/public/restricted/SKILL.md", True]],
        [["/skills/public/restricted/SKILL.md", "1"]],
        [["/skills/public/restricted/SKILL.md", 1], ["/skills/public/restricted/SKILL.md", 2]],
        [["", 1]],
        [["/skills/public/../restricted/SKILL.md", 1]],
        [["/skills/public/restricted/SKILL.md"]],
    ):
        evidence = dict(context[VERIFIED_SKILL_SOURCE_CONTEXT_KEY])
        evidence["entries"] = entries
        tampered = dict(context)
        tampered[VERIFIED_SKILL_SOURCE_CONTEXT_KEY] = evidence
        assert read_verified_skill_source_paths(tampered, owner_token=_OWNER_TOKEN) is None

    legacy_v1 = dict(context)
    legacy_v1[VERIFIED_SKILL_SOURCE_CONTEXT_KEY] = {
        "version": 1,
        "owner_token": _OWNER_TOKEN,
        "identity": ["private-v1", "project-1", "user-1", "run-1"],
        "paths": ["/skills/public/restricted/SKILL.md"],
    }
    assert read_verified_skill_source_paths(legacy_v1, owner_token=_OWNER_TOKEN) is None


def test_restriction_applies_within_ttl_and_the_block_message_explains_itself() -> None:
    skill = _skill()
    middleware = _middleware(skill, ttl=3)
    context = _context()
    entry_path = skill.get_container_file_path(_CONTAINER)

    advance_lead_model_call_seq(context)
    _read_skill_entry(middleware, context, entry_path)

    advance_lead_model_call_seq(context)
    assert _filtered_names(middleware, context) == {
        "grep",
        "read_file",
        "tool_search",
        "describe_skill",
    }

    blocked = middleware.wrap_tool_call(
        _tool_request(context, "bash", tool_call_id="call-2"),
        _must_not_execute,
    )
    assert isinstance(blocked, ToolMessage)
    assert blocked.status == "error"
    assert "'bash' is not allowed" in blocked.content
    assert "restricted" in blocked.content
    assert "allowed-tools" in blocked.content
    assert "SKILL.md" in blocked.content
    assert "/slash" in blocked.content
    assert "expires automatically after 3 model calls" in blocked.content


def test_expired_evidence_restores_the_default_tool_set() -> None:
    skill = _skill()
    middleware = _middleware(skill, ttl=3)
    context = _context()
    entry_path = skill.get_container_file_path(_CONTAINER)

    advance_lead_model_call_seq(context)
    _read_skill_entry(middleware, context, entry_path)

    # Calls 2..3 are inside the window; call 4 is the first expired call.
    advance_lead_model_call_seq(context)
    advance_lead_model_call_seq(context)
    assert "bash" not in _filtered_names(middleware, context)

    advance_lead_model_call_seq(context)
    assert _filtered_names(middleware, context) == set(_DEFAULT_TOOL_NAMES)

    executed = middleware.wrap_tool_call(
        _tool_request(context, "bash", tool_call_id="call-3"),
        lambda _request: ToolMessage(content="ran", tool_call_id="call-3", name="bash"),
    )
    assert isinstance(executed, ToolMessage)
    assert executed.status != "error"
    assert executed.content == "ran"


def test_slash_activation_is_exempt_from_the_ttl() -> None:
    skill = _skill()
    middleware = _middleware(skill, ttl=3)
    context = _context()
    entry_path = skill.get_container_file_path(_CONTAINER)

    write_slash_skill_source_path(context, entry_path, owner_token=_OWNER_TOKEN)
    for _ in range(50):
        advance_lead_model_call_seq(context)

    assert _filtered_names(middleware, context) == {
        "grep",
        "read_file",
        "tool_search",
        "describe_skill",
    }
    blocked = middleware.wrap_tool_call(
        _tool_request(context, "bash", tool_call_id="call-4"),
        _must_not_execute,
    )
    assert isinstance(blocked, ToolMessage)
    assert blocked.status == "error"
    assert "slash-activated" in blocked.content
    assert "expires automatically" not in blocked.content


def test_ttl_zero_keeps_run_long_activation() -> None:
    skill = _skill()
    middleware = _middleware(skill, ttl=0)
    context = _context()
    entry_path = skill.get_container_file_path(_CONTAINER)

    advance_lead_model_call_seq(context)
    _read_skill_entry(middleware, context, entry_path)
    for _ in range(100):
        advance_lead_model_call_seq(context)

    assert "bash" not in _filtered_names(middleware, context)


def test_secret_binding_expires_together_with_read_evidence() -> None:
    skill = _skill(
        name="secretive",
        allowed_tools=None,
        required_secrets=(SecretRequirement(name="API_KEY"),),
    )
    app_config = AppConfig.model_validate(
        {
            "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            "skills": {"read_evidence_ttl_calls": 3},
        }
    )
    activation = SkillActivationMiddleware(
        app_config=app_config,
        runtime_skills=(skill,),
        runtime_skills_container_path=_CONTAINER,
        slash_source_owner_token=_OWNER_TOKEN,
    )
    context = _context()
    context["secrets"] = {"API_KEY": "secret-value"}
    entry_path = skill.get_container_file_path(_CONTAINER)
    request = SimpleNamespace(runtime=SimpleNamespace(context=context))

    advance_lead_model_call_seq(context)
    assert write_verified_skill_source_path(context, entry_path, owner_token=_OWNER_TOKEN)

    advance_lead_model_call_seq(context)
    activation._resolve_secret_bindings(request, None, hook="test")
    assert context[ACTIVE_SECRETS_CONTEXT_KEY] == {"API_KEY": "secret-value"}

    advance_lead_model_call_seq(context)
    advance_lead_model_call_seq(context)
    activation._resolve_secret_bindings(request, None, hook="test")
    assert ACTIVE_SECRETS_CONTEXT_KEY not in context


def test_middleware_rejects_a_malformed_ttl() -> None:
    with pytest.raises(ValueError):
        _middleware(_skill(), ttl=-1)
    with pytest.raises(ValueError):
        SkillToolPolicyMiddleware(
            runtime_skills=(_skill(),),
            runtime_skill_version_ids=("skill-version-1",),
            runtime_skills_container_path=_CONTAINER,
            slash_source_owner_token=_OWNER_TOKEN,
            read_evidence_ttl_calls=True,  # type: ignore[arg-type]
        )
