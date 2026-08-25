"""Trusted exact runtime profiles for dynamically admitted subagents.

The catalog is an in-process authority carrier.  It is intentionally not a
Pydantic model or a mapping: request JSON must not be able to manufacture a
value that the task tool will accept as a Worker-admitted Agent profile.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from types import MappingProxyType
from typing import Any

from langchain_core.tools import BaseTool

from deerflow.config.agents_config import AgentModelSettings
from deerflow.skills.types import Skill

RUNTIME_AGENT_CATALOG_CONTEXT_KEY = "__runtime_agent_catalog"

_FACTORY_SEAL = object()
_RUNTIME_AGENT_KEY_PATTERN = re.compile(r"^(?:system|project)/[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")


class RuntimeAgentProfile:
    """One exact, namespaced Agent Definition admitted for subagent execution."""

    __slots__ = (
        "_description",
        "_key",
        "_mcp_tools",
        "_model_name",
        "_model_settings",
        "_prompt_bundle",
        "_runtime_skills",
        "_seal",
        "_tool_groups",
    )

    def __init__(
        self,
        *,
        _seal: object,
        key: str,
        description: str,
        model_name: str,
        model_settings: AgentModelSettings,
        tool_groups: tuple[str, ...],
        prompt_bundle: object,
        runtime_skills: tuple[Skill, ...],
        mcp_tools: tuple[BaseTool, ...],
    ) -> None:
        if _seal is not _FACTORY_SEAL:
            raise TypeError("RuntimeAgentProfile must be created by its factory")
        object.__setattr__(self, "_seal", _seal)
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_description", description)
        object.__setattr__(self, "_model_name", model_name)
        object.__setattr__(self, "_model_settings", model_settings)
        object.__setattr__(self, "_tool_groups", tool_groups)
        object.__setattr__(self, "_prompt_bundle", prompt_bundle)
        object.__setattr__(self, "_runtime_skills", runtime_skills)
        object.__setattr__(self, "_mcp_tools", mcp_tools)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("RuntimeAgentProfile is immutable")

    @property
    def key(self) -> str:
        return self._key

    @property
    def description(self) -> str:
        return self._description

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_settings(self) -> AgentModelSettings:
        return self._model_settings

    @property
    def tool_groups(self) -> tuple[str, ...]:
        return self._tool_groups

    @property
    def prompt_bundle(self) -> object:
        return self._prompt_bundle

    @property
    def runtime_skills(self) -> tuple[Skill, ...]:
        return self._runtime_skills

    @property
    def mcp_tools(self) -> tuple[BaseTool, ...]:
        return self._mcp_tools

    def __repr__(self) -> str:
        return f"RuntimeAgentProfile(key={self.key!r}, model_name={self.model_name!r}, tool_groups={len(self.tool_groups)}, runtime_skills={len(self.runtime_skills)}, mcp_tools={len(self.mcp_tools)})"


class RuntimeAgentCatalog:
    """Immutable lookup of Worker-admitted runtime Agent profiles."""

    __slots__ = ("_by_key", "_names", "_profiles", "_seal")

    def __init__(
        self,
        *,
        _seal: object,
        profiles: tuple[RuntimeAgentProfile, ...],
    ) -> None:
        if _seal is not _FACTORY_SEAL:
            raise TypeError("RuntimeAgentCatalog must be created by its factory")
        by_key = {profile.key: profile for profile in profiles}
        object.__setattr__(self, "_seal", _seal)
        object.__setattr__(self, "_profiles", profiles)
        object.__setattr__(self, "_names", tuple(profile.key for profile in profiles))
        object.__setattr__(self, "_by_key", MappingProxyType(by_key))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("RuntimeAgentCatalog is immutable")

    @property
    def profiles(self) -> tuple[RuntimeAgentProfile, ...]:
        return self._profiles

    @property
    def names(self) -> tuple[str, ...]:
        return self._names

    def get(self, key: str) -> RuntimeAgentProfile | None:
        return self._by_key.get(key)

    def __repr__(self) -> str:
        return f"RuntimeAgentCatalog(names={self.names!r})"


def _validate_exact_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty canonical string")
    return value


def _validate_unique_names(values: Iterable[Any], *, field_name: str) -> None:
    seen: set[str] = set()
    for value in values:
        name = getattr(value, "name", None)
        if not isinstance(name, str) or not name or name in seen:
            raise ValueError(f"{field_name} must have unique non-empty names")
        seen.add(name)


def build_runtime_agent_profile(
    *,
    key: str,
    description: str,
    model_name: str,
    model_settings: AgentModelSettings,
    tool_groups: Iterable[str],
    prompt_bundle: object,
    runtime_skills: Iterable[Skill],
    mcp_tools: Iterable[BaseTool],
) -> RuntimeAgentProfile:
    """Validate and seal one exact dynamic subagent profile."""

    if not isinstance(key, str) or _RUNTIME_AGENT_KEY_PATTERN.fullmatch(key) is None:
        raise ValueError("Runtime Agent key must be system/<slug> or project/<slug>")
    if not isinstance(description, str):
        raise ValueError("description must be a string")
    exact_model_name = _validate_exact_text(model_name, field_name="model_name")
    if not isinstance(model_settings, AgentModelSettings):
        raise TypeError("model_settings must be AgentModelSettings")
    if prompt_bundle is None:
        raise ValueError("prompt_bundle is required")

    exact_groups = tuple(tool_groups)
    if any(not isinstance(group, str) or not group or group != group.strip() for group in exact_groups):
        raise ValueError("tool_groups must contain canonical non-empty strings")
    if len(set(exact_groups)) != len(exact_groups):
        raise ValueError("tool_groups must not contain duplicates")

    exact_skills = tuple(runtime_skills)
    if any(not isinstance(skill, Skill) for skill in exact_skills):
        raise TypeError("runtime_skills must contain Skill objects")
    _validate_unique_names(exact_skills, field_name="runtime_skills")

    exact_mcp_tools = tuple(mcp_tools)
    # Lazy import avoids tools/__init__ -> task_tool -> this module while the
    # catalog module itself is still being initialized.
    from deerflow.tools.mcp_metadata import is_private_mcp_tool

    if any(not isinstance(tool, BaseTool) or not is_private_mcp_tool(tool) for tool in exact_mcp_tools):
        raise TypeError("mcp_tools must contain private MCP proxy tools")
    _validate_unique_names(exact_mcp_tools, field_name="mcp_tools")

    return RuntimeAgentProfile(
        _seal=_FACTORY_SEAL,
        key=key,
        description=description,
        model_name=exact_model_name,
        model_settings=model_settings,
        tool_groups=exact_groups,
        prompt_bundle=prompt_bundle,
        runtime_skills=exact_skills,
        mcp_tools=exact_mcp_tools,
    )


def build_runtime_agent_catalog(
    profiles: Iterable[RuntimeAgentProfile],
) -> RuntimeAgentCatalog:
    """Seal an ordered dynamic Agent catalog, rejecting forged profiles."""

    exact_profiles = tuple(profiles)
    seen: set[str] = set()
    for profile in exact_profiles:
        if type(profile) is not RuntimeAgentProfile or getattr(profile, "_seal", None) is not _FACTORY_SEAL:
            raise TypeError("profiles must contain factory-sealed RuntimeAgentProfile objects")
        if profile.key in seen:
            raise ValueError(f"Duplicate runtime Agent key: {profile.key}")
        seen.add(profile.key)
    return RuntimeAgentCatalog(
        _seal=_FACTORY_SEAL,
        profiles=exact_profiles,
    )


def trusted_runtime_agent_catalog(value: object) -> RuntimeAgentCatalog | None:
    """Return only an intact catalog created by this module's factory."""

    if type(value) is not RuntimeAgentCatalog or getattr(value, "_seal", None) is not _FACTORY_SEAL:
        return None
    catalog = value
    if any(type(profile) is not RuntimeAgentProfile or getattr(profile, "_seal", None) is not _FACTORY_SEAL or catalog.get(profile.key) is not profile for profile in catalog.profiles):
        return None
    if catalog.names != tuple(profile.key for profile in catalog.profiles):
        return None
    return catalog


__all__ = [
    "RUNTIME_AGENT_CATALOG_CONTEXT_KEY",
    "RuntimeAgentCatalog",
    "RuntimeAgentProfile",
    "build_runtime_agent_catalog",
    "build_runtime_agent_profile",
    "trusted_runtime_agent_catalog",
]
