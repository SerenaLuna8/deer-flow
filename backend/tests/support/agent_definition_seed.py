from __future__ import annotations

import uuid

from app.shared_assets.agent_payload_checksum import agent_payload_checksum
from app.shared_assets.models import AgentModelSettings, AgentPayload


def direct_agent_definition_fields(
    *,
    updated_by_user_id: str,
    description: str = "",
    agents_instructions: str = "",
    soul: str = "",
    identity: str = "",
    user_context: str = "",
    model_ref: str = "default",
    model_settings: AgentModelSettings | None = None,
    tool_groups: tuple[str, ...] = (),
) -> dict[str, object]:
    """Return the complete inline Definition fields for a direct test Agent seed."""

    settings = model_settings or AgentModelSettings()
    payload = AgentPayload(
        description=description,
        agents_instructions=agents_instructions,
        soul=soul,
        identity=identity,
        user_context=user_context,
        model_ref=model_ref,
        model_settings=settings,
        tool_groups=tool_groups,
        skill_refs=(),
        mcp_version_ids=(),
        payload_schema_version=4,
    )
    return {
        "definition_id": uuid.uuid4(),
        "description": payload.description,
        "agents_instructions": payload.agents_instructions,
        "soul": payload.soul,
        "identity": payload.identity,
        "user_context": payload.user_context,
        "model_ref": payload.model_ref,
        "model_settings": settings.model_dump(exclude_none=True),
        "tool_groups": list(payload.tool_groups),
        "payload_schema_version": payload.payload_schema_version,
        "payload_checksum": agent_payload_checksum(payload),
        "updated_by_user_id": updated_by_user_id,
    }
