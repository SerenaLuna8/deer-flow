from __future__ import annotations

from app.gateway.github.dispatcher import _is_self_event
from deerflow.config.agents_config import GitHubAgentConfig


def test_self_event_uses_explicit_bot_identity() -> None:
    github = GitHubAgentConfig(bot_login="deerflow-bot", bindings=[])

    assert _is_self_event(
        "issue_comment",
        {"sender": {"login": "deerflow-bot[bot]"}},
        "reviewer",
        github,
    )
    assert not _is_self_event(
        "issue_comment",
        {"sender": {"login": "third-party-bot[bot]"}},
        "reviewer",
        github,
    )
