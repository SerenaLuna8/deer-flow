from __future__ import annotations

from app.shared_assets import McpDefinition, McpSecretSlot

from .contracts import McpConfiguredRequest, McpVersionRequest


def _mcp_definition(body: McpVersionRequest | McpConfiguredRequest) -> McpDefinition:
    return McpDefinition(
        description=body.description,
        transport=body.transport,
        command=body.command,
        args=tuple(body.args),
        url=body.url,
        env=dict(body.env),
        headers=dict(body.headers),
        oauth=dict(body.oauth),
        routing=dict(body.routing),
        tool_overrides=dict(body.tool_overrides),
        timeout_seconds=body.timeout_seconds,
        secret_slots=tuple(
            McpSecretSlot(
                name=slot.name,
                purpose=slot.purpose,
                payload_schema={key: tuple(values) for key, values in slot.payload_schema.items()},
                required=slot.required,
            )
            for slot in body.secret_slots
        ),
    )
