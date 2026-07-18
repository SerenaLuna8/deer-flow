from __future__ import annotations

from app.gateway.github.registry import (
    GitHubAgentMatch,
    build_github_agent_registry,
    lookup_agents,
)
from deerflow.config.agents_config import AgentConfig, GitHubTriggerConfig


def test_ownerless_registry_fails_closed() -> None:
    assert build_github_agent_registry() == {}


def test_lookup_agents_preserves_provider_backed_registry_contract() -> None:
    match = GitHubAgentMatch(
        user_id="account-1",
        agent=AgentConfig(name="reviewer"),
        trigger=GitHubTriggerConfig(),
    )
    registry = {("owner/repo", "pull_request"): [match]}

    assert lookup_agents(registry, "owner/repo", "pull_request") == [match]
    assert lookup_agents(registry, "owner/repo", "issues") == []
