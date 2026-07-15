"""Helpers for attaching persisted channel connection ownership to inbound messages."""

from __future__ import annotations

from typing import Any

from app.channels.message_bus import InboundMessage


async def attach_connection_identity(
    inbound: InboundMessage,
    *,
    repo: Any,
    provider: str,
    workspace_id: str | None,
    fallback_without_workspace: bool = False,
) -> InboundMessage:
    """Copy server lookup hints onto an inbound message.

    These mutable fields are for routing/display compatibility only. The
    private-work resolver independently re-resolves the connection and remains
    the sole project/owner authority before creating a Thread or run.
    """
    if repo is None:
        return inbound

    workspace_candidates: list[str | None] = []
    if workspace_id:
        workspace_candidates.append(workspace_id)
    if fallback_without_workspace:
        workspace_candidates.append(None)
    if not workspace_candidates:
        return inbound

    for candidate in workspace_candidates:
        connection = await repo.find_connection_by_external_identity(
            provider=provider,
            external_account_id=inbound.user_id,
            workspace_id=candidate,
        )
        if connection is None:
            continue

        inbound.connection_id = connection["id"]
        inbound.owner_user_id = connection["owner_user_id"]
        inbound.project_id = connection.get("project_id")
        inbound.workspace_id = connection.get("workspace_id")
        return inbound

    return inbound
