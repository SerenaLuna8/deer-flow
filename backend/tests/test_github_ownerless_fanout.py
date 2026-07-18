from __future__ import annotations

from pathlib import Path

import pytest

from app.channels.message_bus import MessageBus
from app.gateway.github.dispatcher import fanout_event
from deerflow.config import agents_config


@pytest.mark.asyncio
async def test_ownerless_webhook_fanout_fails_closed_without_filesystem_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ownerless webhook must not discover legacy agents from disk."""

    def forbidden_filesystem_access(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("GitHub webhook fan-out must not inspect legacy agent files")

    monkeypatch.setattr(Path, "exists", forbidden_filesystem_access)
    monkeypatch.setattr(Path, "iterdir", forbidden_filesystem_access)
    monkeypatch.setattr(Path, "stat", forbidden_filesystem_access)
    monkeypatch.setattr(
        agents_config,
        "load_agent_config",
        forbidden_filesystem_access,
    )

    bus = MessageBus()
    result = await fanout_event(
        bus,
        "pull_request",
        "delivery-ownerless",
        {
            "action": "opened",
            "pull_request": {"number": 17, "title": "Do not scan disk"},
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "octocat"},
        },
    )

    assert result == {"matched_agents": [], "fired_agents": [], "skipped": []}
    assert bus.inbound_queue.empty()
