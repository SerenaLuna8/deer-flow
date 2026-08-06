"""Authoritative GitHub webhook binding lookup.

M7 removes legacy filesystem agents as an authority source. GitHub webhooks do
not carry an authenticated account/project binding, and there is not yet a
PostgreSQL project binding for this ownerless route. The only safe behavior is
therefore to fail closed instead of scanning global or per-user directories.
"""

from __future__ import annotations

from dataclasses import dataclass

from deerflow.config.agents_config import AgentConfig, GitHubTriggerConfig


@dataclass(frozen=True)
class GitHubAgentMatch:
    """One future provider-backed GitHub binding match."""

    user_id: str
    agent: AgentConfig
    trigger: GitHubTriggerConfig


_Registry = dict[tuple[str, str], list[GitHubAgentMatch]]


def build_github_agent_registry() -> _Registry:
    """Fail ownerless/global webhook fan-out closed.

    A provider-backed implementation may replace this once the webhook route
    can resolve an authenticated project binding. Until then it must never
    discover agents from any legacy global or per-user filesystem layout.
    """

    return {}


def lookup_agents(
    registry: _Registry,
    repo: str,
    event: str,
) -> list[GitHubAgentMatch]:
    """Return authoritative matches for ``(repo, event)``."""

    return registry.get((repo, event), [])
