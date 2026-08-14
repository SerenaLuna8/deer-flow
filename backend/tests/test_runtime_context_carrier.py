"""Typed runtime-context authority carrier contracts."""

from __future__ import annotations

import pickle

import pytest

from deerflow.agents.memory.authority_resolution import (
    MEMORY_AUTHORITY_CONTEXT_KEY,
)
from deerflow.agents.memory.snip import MEMORY_ARCHIVE_CONTEXT_KEY
from deerflow.guardrails.provider import GUARDRAIL_ATTRIBUTION_CONTEXT_KEY
from deerflow.runtime.context_carrier import RuntimeContextCarrier
from deerflow.runtime.context_keys import (
    CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY,
    RuntimeContextKeys,
)
from deerflow.runtime.secret_context import (
    _SLASH_SKILL_ACTIVATION_RUN_KEY,
    REDACTED_CONTEXT_KEYS,
    SECRETS_CONTEXT_KEY,
    SKILL_SCOPED_SECRETS_CONTEXT_KEY,
    SKILL_SECRET_PROVIDER_CONTEXT_KEY,
)
from deerflow.runtime.skill_context_authority import (
    LEAD_MODEL_CALL_SEQ_CONTEXT_KEY,
    VERIFIED_SKILL_SOURCE_CONTEXT_KEY,
)
from deerflow.subagents.runtime_catalog import (
    RUNTIME_AGENT_CATALOG_CONTEXT_KEY,
)
from deerflow.trace_context import DEERFLOW_TRACE_METADATA_KEY


def test_runtime_context_keys_reuse_existing_canonical_values() -> None:
    assert RuntimeContextKeys.CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS == CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY
    assert RuntimeContextKeys.GUARDRAIL_ATTRIBUTION == GUARDRAIL_ATTRIBUTION_CONTEXT_KEY
    assert RuntimeContextKeys.MEMORY_AUTHORITY == MEMORY_AUTHORITY_CONTEXT_KEY
    assert RuntimeContextKeys.MEMORY_ARCHIVE_CONTEXT == MEMORY_ARCHIVE_CONTEXT_KEY
    assert RuntimeContextKeys.RUNTIME_AGENT_CATALOG == RUNTIME_AGENT_CATALOG_CONTEXT_KEY
    assert RuntimeContextKeys.SECRETS == SECRETS_CONTEXT_KEY
    assert RuntimeContextKeys.SKILL_SCOPED_SECRETS == SKILL_SCOPED_SECRETS_CONTEXT_KEY
    assert RuntimeContextKeys.SKILL_SECRET_PROVIDER == SKILL_SECRET_PROVIDER_CONTEXT_KEY
    assert RuntimeContextKeys.VERIFIED_SKILL_SOURCE == VERIFIED_SKILL_SOURCE_CONTEXT_KEY
    assert RuntimeContextKeys.LEAD_MODEL_CALL_SEQ == LEAD_MODEL_CALL_SEQ_CONTEXT_KEY
    assert RuntimeContextKeys.TRACE_ID == DEERFLOW_TRACE_METADATA_KEY
    assert RuntimeContextKeys.INSTALL_KEYS <= RuntimeContextKeys.SERVER_OWNED_KEYS
    assert RuntimeContextKeys.CALLER_PASSTHROUGH_KEYS.isdisjoint(
        RuntimeContextKeys.SERVER_OWNED_KEYS,
    )
    assert (REDACTED_CONTEXT_KEYS - {SECRETS_CONTEXT_KEY}) <= RuntimeContextKeys.SERVER_OWNED_KEYS
    assert {
        CURRENT_RUN_PRE_EXISTING_MESSAGE_IDS_KEY,
        _SLASH_SKILL_ACTIVATION_RUN_KEY,
        VERIFIED_SKILL_SOURCE_CONTEXT_KEY,
        LEAD_MODEL_CALL_SEQ_CONTEXT_KEY,
        MEMORY_AUTHORITY_CONTEXT_KEY,
        RuntimeContextKeys.LEGACY_MEMORY_AUTHORITY,
        GUARDRAIL_ATTRIBUTION_CONTEXT_KEY,
        RUNTIME_AGENT_CATALOG_CONTEXT_KEY,
        RuntimeContextKeys.STOP_REASON,
    } <= RuntimeContextKeys.SERVER_OWNED_KEYS


def test_illegal_caller_context_cannot_override_authoritative_values() -> None:
    forged_context = {key: f"forged:{key}" for key in RuntimeContextKeys.SERVER_OWNED_KEYS}
    forged_context.update(
        {
            "__future_server_authority": "forged-future-value",
            RuntimeContextKeys.AGENT_NAME: "bootstrap-agent",
            "extension_context": "preserved",
        },
    )
    exact_file_authority = object()

    context = RuntimeContextCarrier(
        thread_id="trusted-thread",
        run_id="trusted-run",
        file_authority=exact_file_authority,
    ).build(forged_context)

    assert context[RuntimeContextKeys.THREAD_ID] == "trusted-thread"
    assert context[RuntimeContextKeys.RUN_ID] == "trusted-run"
    assert context[RuntimeContextKeys.FILE_AUTHORITY] is exact_file_authority
    assert context[RuntimeContextKeys.AGENT_NAME] == "bootstrap-agent"
    assert context["extension_context"] == "preserved"
    assert "__future_server_authority" not in context
    installed = {
        RuntimeContextKeys.THREAD_ID,
        RuntimeContextKeys.RUN_ID,
        RuntimeContextKeys.FILE_AUTHORITY,
    }
    assert not (RuntimeContextKeys.SERVER_OWNED_KEYS - installed).intersection(
        context,
    )


def test_carrier_installs_only_non_none_values() -> None:
    context = RuntimeContextCarrier(
        thread_id="thread-1",
        is_subagent=False,
    ).build()

    assert context == {
        RuntimeContextKeys.THREAD_ID: "thread-1",
        RuntimeContextKeys.IS_SUBAGENT: False,
    }


def test_carrier_fields_cover_the_complete_install_key_set() -> None:
    context = RuntimeContextCarrier(
        thread_id="thread-1",
        run_id="run-1",
        app_config=object(),
        model_name="model-1",
        user_id="user-1",
        user_role="runner",
        oauth_provider="provider",
        oauth_id="oauth-1",
        channel_user_id="channel-user-1",
        is_subagent=False,
        private_scope=object(),
        authorization_checker=object(),
        authorization_boundary=object(),
        file_authority=object(),
        memory_authority=object(),
        guardrail_attribution={"user_id": "user-1"},
        run_read_only_mounts=(object(),),
        agent_prompt_bundle=object(),
        runtime_skills=(object(),),
        runtime_mcp_tools=(object(),),
        runtime_agent_catalog=object(),
        skill_scoped_secrets={"/skill/SKILL.md": {"TOKEN": "secret"}},
        skill_secret_provider=object(),
        current_run_pre_existing_message_ids=frozenset({"message-1"}),
        trace_id="trace-1",
        run_journal=object(),
        memory_archive_context=object(),
        host_execution_approval_port=object(),
        host_execution_agent_path=("lead",),
    ).build()

    assert frozenset(context) == RuntimeContextKeys.INSTALL_KEYS


def test_carrier_deep_copies_skill_scoped_secrets() -> None:
    secrets = {
        "/mnt/skills/custom/example/SKILL.md": {
            "EXAMPLE_TOKEN": "secret-value",
        },
    }

    context = RuntimeContextCarrier(
        skill_scoped_secrets=secrets,
    ).build()
    installed = context[RuntimeContextKeys.SKILL_SCOPED_SECRETS]

    assert installed == secrets
    assert installed is not secrets
    assert installed["/mnt/skills/custom/example/SKILL.md"] is not secrets["/mnt/skills/custom/example/SKILL.md"]
    installed["/mnt/skills/custom/example/SKILL.md"]["EXAMPLE_TOKEN"] = "mutated"
    assert secrets["/mnt/skills/custom/example/SKILL.md"]["EXAMPLE_TOKEN"] == "secret-value"


def test_carrier_forces_subagent_guardrail_attribution() -> None:
    attribution = {
        "user_id": "user-1",
        "is_subagent": False,
        "authz_attributes": {
            "roles": ["runner"],
        },
    }

    context = RuntimeContextCarrier(
        is_subagent=True,
        guardrail_attribution=attribution,
    ).build()
    installed = context[RuntimeContextKeys.GUARDRAIL_ATTRIBUTION]

    assert context[RuntimeContextKeys.IS_SUBAGENT] is True
    assert installed["is_subagent"] is True
    assert installed["authz_attributes"] == {"roles": ["runner"]}
    assert installed["authz_attributes"] is not attribution["authz_attributes"]


def test_carrier_is_opaque_and_not_pickle_serializable() -> None:
    secret_value = "must-not-appear"
    carrier = RuntimeContextCarrier(
        file_authority=object(),
        skill_scoped_secrets={"/skill/SKILL.md": {"TOKEN": secret_value}},
    )

    assert secret_value not in repr(carrier)
    with pytest.raises(TypeError, match="runtime authority carrier"):
        pickle.dumps(carrier)
