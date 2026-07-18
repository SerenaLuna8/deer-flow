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
) -> InboundMessage:
    """Attach only the exact server-resolved connection coordinate.

    Project/account/owner authority never travels on the mutable inbound
    message. The private-work resolver re-reads the connection row immediately
    before creating a Thread or Run.
    """
    inbound.workspace_id = workspace_id
    if repo is None:
        return inbound
    connection = await repo.find_connection_by_external_identity(
        provider=provider,
        external_account_id=inbound.user_id,
        workspace_id=workspace_id,
    )
    if connection is not None:
        inbound.connection_id = connection["id"]
    return inbound
