from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from app.shared_assets.agent_payload_checksum import (
    agent_payload_checksum,
    agent_payload_checksum_matches,
)
from app.shared_assets.agent_service import AgentService
from app.shared_assets.models import AgentModelSettings, AgentPayload

_MODEL_REF = "88888888-8888-4888-8888-888888888891"


def _payload(
    *,
    payload_schema_version: int,
    model_settings: AgentModelSettings | None = None,
) -> AgentPayload:
    return AgentPayload(
        description="描述",
        soul="soul",
        model_ref=_MODEL_REF,
        tool_groups=("file:read", "task"),
        skill_version_ids=(uuid.UUID("11111111-1111-1111-1111-111111111111"),),
        mcp_version_ids=(uuid.UUID("22222222-2222-2222-2222-222222222222"),),
        agents_instructions="agents",
        identity="identity",
        user_context="user",
        payload_schema_version=payload_schema_version,
        model_settings=model_settings or AgentModelSettings(),
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            _payload(payload_schema_version=1),
            "0bec4cafa2aa2762ed4ed4d8b15e39570f92c776f59f3616ed8b5aa2aadb3cc8",
        ),
        (
            _payload(payload_schema_version=2),
            "e44d78be94287d8a211d7bbe966469977b4705a113c41b01f332dabd4f874a01",
        ),
        (
            _payload(
                payload_schema_version=3,
                model_settings=AgentModelSettings(
                    temperature=0.5,
                    max_tokens=123,
                ),
            ),
            "d70c97307b92b1a805e3759ace3bfe514e2d085424ae83e2e64bdbc0e97f712d",
        ),
    ],
)
def test_agent_payload_checksum_preserves_schema_contracts(
    payload: AgentPayload,
    expected: str,
) -> None:
    assert agent_payload_checksum(payload) == expected
    assert (
        AgentService._payload_checksum(  # noqa: SLF001 - compatibility wrapper contract
            payload,
            payload_schema_version=payload.payload_schema_version,
        )
        == expected
    )


def test_agent_payload_checksum_covers_only_fields_introduced_by_each_schema() -> None:
    v1 = _payload(payload_schema_version=1)
    v2 = replace(v1, payload_schema_version=2)
    v3 = replace(
        v2,
        payload_schema_version=3,
        model_settings=AgentModelSettings(temperature=0.5),
    )

    assert agent_payload_checksum(replace(v1, agents_instructions="legacy field is not authoritative")) == agent_payload_checksum(v1)
    assert agent_payload_checksum(replace(v2, agents_instructions="tampered")) != agent_payload_checksum(v2)
    assert agent_payload_checksum(replace(v3, model_settings=AgentModelSettings(temperature=0.75))) != agent_payload_checksum(v3)


def test_agent_payload_checksum_rejects_invalid_schema_settings_pair() -> None:
    with pytest.raises(ValueError, match="model settings"):
        agent_payload_checksum(
            _payload(
                payload_schema_version=2,
                model_settings=AgentModelSettings(max_tokens=128),
            )
        )
    with pytest.raises(ValueError, match="schema version"):
        agent_payload_checksum(replace(_payload(payload_schema_version=1), payload_schema_version=4))


def test_agent_payload_checksum_matcher_fails_closed() -> None:
    payload = _payload(payload_schema_version=2)
    checksum = agent_payload_checksum(payload)

    assert agent_payload_checksum_matches(payload, checksum)
    assert not agent_payload_checksum_matches(
        replace(payload, soul="tampered"),
        checksum,
    )
    assert not agent_payload_checksum_matches(payload, "not-a-checksum")
