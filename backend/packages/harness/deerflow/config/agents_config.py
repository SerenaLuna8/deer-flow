"""Configuration and loaders for custom agents.

Custom agents are stored per-user under ``{base_dir}/users/{user_id}/agents/{name}/``.
A shared layout at ``{base_dir}/agents/{name}/`` remains a read-only compatibility
source. New writes always target the per-user layout.
"""

import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from deerflow.config.paths import get_paths
from deerflow.runtime.user_context import get_effective_user_id

logger = logging.getLogger(__name__)

SOUL_FILENAME = "SOUL.md"
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
MAX_AGENT_OUTPUT_TOKENS = 200_000


class AgentModelSettings(BaseModel):
    """Strict immutable settings carried by one exact Agent version."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(
        default=None,
        ge=1,
        le=MAX_AGENT_OUTPUT_TOKENS,
    )
    thinking_enabled: bool | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None

    @field_validator("temperature")
    @classmethod
    def _canonical_temperature(
        cls,
        value: float | None,
    ) -> float | None:
        # PostgreSQL JSONB normalizes negative zero. Canonicalize before the
        # v3 checksum so a stored row always hashes exactly as authored.
        if value == 0:
            return 0.0
        return value

    @property
    def is_empty(self) -> bool:
        return not self.model_dump(exclude_none=True)

    def sampling_overrides(self) -> dict[str, float | int]:
        return {
            key: value
            for key, value in self.model_dump(
                include={"temperature", "max_tokens"},
                exclude_none=True,
            ).items()
            if isinstance(value, (float, int)) and not isinstance(value, bool)
        }

    @model_serializer(mode="wrap")
    def _serialize_without_nulls(self, handler):
        """Keep the nested HTTP/JSON contract aligned with checksum JSON."""

        return {key: value for key, value in handler(self).items() if value is not None}


class GitHubTriggerConfig(BaseModel):
    """Per-event trigger filter inside a :class:`GitHubBinding`."""

    # If set, only these GitHub action values fire the agent. None means "any
    # action allowed". Example: ["opened"] for pull_request restricts the agent
    # to only respond to brand-new PRs.
    actions: list[str] | None = None
    # If True, comment events only fire when the bot login is @-mentioned in
    # the comment body. Ignored on non-comment events.
    require_mention: bool = False
    # GitHub logins whose events bypass require_mention. Lets a repo owner
    # talk to the bot without typing the handle every time.
    allow_authors: list[str] = Field(default_factory=list)
    # Override the global default bot mention login for this trigger only.
    # Useful when one agent answers as @bot-a and another as @bot-b.
    mention_login: str | None = None


class GitHubBinding(BaseModel):
    """One (agent, repo) binding with per-event trigger overrides."""

    # GitHub "owner/name" string.
    repo: str
    # Event name → trigger override. Missing keys fall back to the dispatcher's
    # default trigger for that event.
    triggers: dict[str, GitHubTriggerConfig] = Field(default_factory=dict)


class GitHubAgentConfig(BaseModel):
    """Top-level ``github:`` block on a custom agent's ``config.yaml``."""

    # GitHub App installation id used to mint per-repo access tokens. The
    # ``ChannelManager`` mints a 1h installation token from this and injects it
    # into ``run_context["github_token"]``, which the ``bash`` tool exposes to
    # the agent's sandbox as ``GH_TOKEN`` / ``GITHUB_TOKEN``. The agent then
    # uses ``gh`` to read repo state, push branches, and post comments itself.
    # None means no token is minted: the agent still runs but cannot push or
    # post (effectively read-only via unauthenticated ``gh`` for public repos,
    # or fully blind for private ones).
    installation_id: int | None = None
    # GitHub App login this agent posts as (e.g. ``llm-gateway-ai`` for the
    # ``llm-gateway-ai[bot]`` App identity, without the ``[bot]`` suffix).
    # The dispatcher's self-event gate uses this to recognize webhook
    # deliveries triggered by this agent's own activity, regardless of what
    # ``mention_login`` the agent uses for trigger matching. None means
    # "fall back to mention_login / agent name", which is fine when those
    # match the bot identity, but should be set explicitly when they differ.
    bot_login: str | None = None
    # Override the default github-channel ``recursion_limit`` (250). GitHub
    # runs are autonomous and long-running by nature — clone, explore, edit,
    # test, push, comment — but the right ceiling varies a lot by workload:
    # a review-only agent might be happy at 50, a multi-file refactor agent
    # might need 500+. Setting None means "use the channel default (250)".
    # Any positive integer is honored verbatim — including values below the
    # channel default and below the global 100-step floor — so an explicit
    # safety setting like ``recursion_limit: 50`` halts the agent at 50
    # super-steps as configured. Values <=0 are ignored (treated as None)
    # — a negative/zero limit would halt the agent before the first step.
    recursion_limit: int | None = None
    # Repos this agent is bound to. Empty list = bound to nothing = the agent
    # never fires from a webhook, even if it has a ``github:`` block.
    bindings: list[GitHubBinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_binding_repos(self) -> "GitHubAgentConfig":
        """Reject duplicate ``repo`` values across ``bindings``.

        At most one binding per repo is allowed. The per-event ``triggers``
        map on a single binding already expresses "this agent listens to N
        events on this repo", so multiple bindings for the same repo would
        either duplicate events (silent first-wins / double-registration —
        see PR feedback R3) or fragment them across rows for no benefit.
        Since this is the initial implementation and no existing operator
        config relies on duplicate-repo bindings, we fail loudly at config
        load instead of papering over the ambiguity at dispatch time.
        """
        seen: set[str] = set()
        dupes: set[str] = set()
        for binding in self.bindings:
            if binding.repo in seen:
                dupes.add(binding.repo)
            seen.add(binding.repo)
        if dupes:
            raise ValueError(f"Agent github.bindings has duplicate repos {sorted(dupes)}. Each repo must appear at most once — merge their `triggers:` maps into a single binding.")
        return self


def validate_agent_name(name: str | None) -> str | None:
    """Validate a custom agent name before using it in filesystem paths."""
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError("Invalid agent name. Expected a string or None.")
    if not AGENT_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Invalid agent name '{name}'. Must match pattern: {AGENT_NAME_PATTERN.pattern}")
    return name


class AgentConfig(BaseModel):
    """Configuration for a custom agent."""

    name: str
    description: str = ""
    model: str | None = None
    tool_groups: list[str] | None = None
    # skills controls which skills are loaded into the agent's prompt:
    # - None (or omitted): load all enabled skills (default fallback behavior)
    # - [] (explicit empty list): disable all skills
    # - ["skill1", "skill2"]: load only the specified skills
    skills: list[str] | None = None
    model_settings: AgentModelSettings | None = None
    # Optional binding to GitHub repositories so this agent can respond to
    # webhook events from the gateway dispatcher. None means "no GitHub
    # integration", which is the case for every existing agent.
    github: GitHubAgentConfig | None = None


# Fields explicitly managed by PostgreSQL shared-asset authoring.
# Anything else declared on :class:`AgentConfig` — currently
# ``github``, and any future field — is preserved verbatim by
# :func:`preserve_non_managed_fields` so neither surface can silently
# drop hand-authored configuration. ``name`` is included because the
# updaters always re-emit it from the directory name (it must never come
# from the request body).
MANAGED_AGENT_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "model",
        "tool_groups",
        "skills",
        "model_settings",
    }
)


def preserve_non_managed_fields(existing_cfg: AgentConfig) -> dict[str, object]:
    """Return every top-level field on ``existing_cfg`` not in :data:`MANAGED_AGENT_CONFIG_FIELDS`.

    Used by PostgreSQL shared-asset authoring to carry forward any field it
    does not expose directly — currently ``github``,
    and any field added to :class:`AgentConfig` in the future — that the
    update API does not expose as an argument. Without this, operators who
    hand-author a ``github:`` block on a custom agent would silently lose
    it the next time a project asset editor touched ``description`` /
    ``model`` / ``tool_groups`` / ``skills``.

    ``exclude_unset=True`` is recursive in Pydantic v2, so a sub-field the
    user did not write (and that defaulted to a Pydantic default) is not
    materialized into the dict — the file round-trips visually intact.
    """
    return existing_cfg.model_dump(exclude_unset=True, exclude=MANAGED_AGENT_CONFIG_FIELDS)


def resolve_agent_dir(name: str, *, user_id: str | None = None) -> Path:
    """Return the on-disk directory for an agent, preferring the per-user layout.

    Resolution order:
    1. ``{base_dir}/users/{user_id}/agents/{name}/`` (per-user, current layout).
    2. ``{base_dir}/agents/{name}/`` (legacy shared layout — read-only fallback).

    If neither exists, the per-user path is returned so callers that intend to
    create the agent write into the new layout.

    Args:
        name: Validated agent name.
        user_id: Owner of the agent. Defaults to the effective user from the
            request context (or ``"default"`` in no-auth mode).
    """
    paths = get_paths()
    effective_user = user_id or get_effective_user_id()
    user_path = paths.user_agent_dir(effective_user, name)
    # Require config.yaml to confirm this is a genuine agent directory,
    # not a leftover from memory/storage writes (see #3390).
    if user_path.exists() and (user_path / "config.yaml").exists():
        return user_path

    legacy_path = paths.agent_dir(name)
    if legacy_path.exists() and (legacy_path / "config.yaml").exists():
        return legacy_path

    return user_path


def load_agent_config(name: str | None, *, user_id: str | None = None) -> AgentConfig | None:
    """Load a named system Agent from the mandatory PostgreSQL catalog."""

    if name is None:
        return None

    name = validate_agent_name(name)
    from deerflow.assets.catalog import (
        AssetCatalogAgentSnapshot,
        AssetCatalogUnavailable,
        require_asset_catalog_provider,
        require_system_asset,
        run_asset_catalog_lookup,
    )

    snapshot = run_asset_catalog_lookup(require_asset_catalog_provider(), "get_system_agent", name)
    if not isinstance(snapshot, AssetCatalogAgentSnapshot):
        raise AssetCatalogUnavailable("system agent snapshot is invalid")
    require_system_asset(snapshot)
    model_settings = AgentModelSettings.model_validate(dict(snapshot.model_settings))
    return AgentConfig(
        name=snapshot.slug,
        description=snapshot.description,
        model=snapshot.model_ref or None,
        tool_groups=list(snapshot.tool_groups),
        skills=list(snapshot.skill_slugs),
        model_settings=(None if model_settings.is_empty else model_settings),
    )


def load_agent_soul(agent_name: str | None, *, user_id: str | None = None) -> str | None:
    """Return the PostgreSQL system Agent soul, or no soul for the default."""
    if not agent_name:
        return None
    from deerflow.assets.catalog import (
        AssetCatalogAgentSnapshot,
        AssetCatalogUnavailable,
        require_asset_catalog_provider,
        require_system_asset,
        run_asset_catalog_lookup,
    )

    snapshot = run_asset_catalog_lookup(require_asset_catalog_provider(), "get_system_agent", agent_name)
    if not isinstance(snapshot, AssetCatalogAgentSnapshot):
        raise AssetCatalogUnavailable("system agent snapshot is invalid")
    require_system_asset(snapshot)
    return snapshot.soul.strip() or None


def list_custom_agents(*, user_id: str | None = None) -> list[AgentConfig]:
    """List system Agents from the mandatory PostgreSQL catalog."""
    from deerflow.assets.catalog import (
        AssetCatalogAgentSnapshot,
        AssetCatalogUnavailable,
        require_asset_catalog_provider,
        require_system_asset,
        run_asset_catalog_lookup,
    )

    snapshots = run_asset_catalog_lookup(require_asset_catalog_provider(), "list_system_agents")
    if not isinstance(snapshots, tuple):
        raise AssetCatalogUnavailable("system agent catalog is invalid")
    agents: list[AgentConfig] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, AssetCatalogAgentSnapshot):
            raise AssetCatalogUnavailable("system agent snapshot is invalid")
        require_system_asset(snapshot)
        model_settings = AgentModelSettings.model_validate(dict(snapshot.model_settings))
        agents.append(
            AgentConfig(
                name=snapshot.slug,
                description=snapshot.description,
                model=snapshot.model_ref or None,
                tool_groups=list(snapshot.tool_groups),
                skills=list(snapshot.skill_slugs),
                model_settings=(None if model_settings.is_empty else model_settings),
            )
        )
    agents.sort(key=lambda a: a.name)
    return agents
