"""Helpers for attaching persisted channel connection ownership to inbound messages."""

from __future__ import annotations

from typing import Any

from app.channels.message_bus import InboundMessage
from deerflow.runtime.private_scope import PrivateResourceScope


async def attach_connection_identity(
    inbound: InboundMessage,
    *,
    repo: Any,
    provider: str,
    workspace_id: str | None,
) -> InboundMessage:
    """Attach only the exact server-resolved connection coordinate.

    The immutable private scope attached here is limited to PostgreSQL
    conversation-alias lookup. The private-work resolver re-reads connection,
    project, owner, membership and capability immediately before creating a
    Thread or Run, so this mutable message never becomes execution authority.
    """
    inbound.workspace_id = workspace_id
    inbound.connection_id = None
    inbound.private_scope = None
    if repo is None:
        return inbound
    connection = await repo.find_connection_by_external_identity(
        provider=provider,
        external_account_id=inbound.user_id,
        workspace_id=workspace_id,
    )
    if connection is not None:
        try:
            # Older repository adapters may not expose the advisory version;
            # execution authority is re-resolved later and the conversation
            # repository binds only Project + Owner coordinates.
            membership_version = int(connection.get("membership_version", 1))
            scope = PrivateResourceScope(
                project_id=str(connection["project_id"]),
                owner_user_id=str(connection["owner_user_id"]),
                membership_version=membership_version,
            )
            if membership_version < 1:
                raise ValueError
            connection_id = str(connection["id"])
            if not connection_id:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return inbound
        inbound.connection_id = connection_id
        inbound.private_scope = scope
    return inbound
