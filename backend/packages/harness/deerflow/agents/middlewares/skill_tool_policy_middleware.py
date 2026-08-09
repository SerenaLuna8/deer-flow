"""Apply Skill ``allowed-tools`` only to active, exact runtime Skills."""

from __future__ import annotations

import json
import logging
import posixpath
import secrets
from collections.abc import Awaitable, Callable, Collection
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.agents.middlewares.skill_context import _tool_call_path
from deerflow.config.summarization_config import (
    DEFAULT_SKILL_FILE_READ_TOOL_NAMES,
)
from deerflow.runtime.secret_context import (
    SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY,
    read_slash_skill_source_path,
)
from deerflow.runtime.skill_context_authority import (
    read_lead_model_call_seq,
    read_verified_skill_source_entries,
    read_verified_skill_source_paths,
    write_verified_skill_source_path,
)
from deerflow.skills.tool_policy import (
    ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES,
    allowed_tool_names_for_skills,
)
from deerflow.skills.types import Skill

logger = logging.getLogger(__name__)

_POLICY_DECISION_VERSION = 2
_POLICY_SOURCE_PASSIVE = "passive"
_POLICY_SOURCE_SLASH = "slash"
_POLICY_SOURCE_VERIFIED_READ = "verified_read"
_POLICY_SOURCE_INVALID = "invalid"
_POLICY_SOURCES = frozenset(
    {
        _POLICY_SOURCE_PASSIVE,
        _POLICY_SOURCE_SLASH,
        _POLICY_SOURCE_VERIFIED_READ,
        _POLICY_SOURCE_INVALID,
    }
)
_MISSING_POLICY_DECISION = object()
_TOOL_SEARCH_NAME = "tool_search"
_INVALID_ACTIVE_REFERENCE = "<invalid-skill-read-evidence>"

type _PolicySignature = tuple[str, tuple[str, ...]]


class SkillToolPolicyMiddleware(AgentMiddleware[AgentState]):
    """Restrict lead tools to active Skills from the admitted run snapshot.

    An admitted Skill is passive metadata until either the user slash-activates
    it or this middleware observes a successful ``read_file`` execution for
    its exact entry path. Durable ``ThreadState.skill_context`` remains
    observational and is never an authorization source. Slash activation has
    run-long priority, so later reads cannot widen the user-selected Skill's
    authority.

    Verified-read activation is additionally bounded by
    ``read_evidence_ttl_calls``: evidence older than that many lead model calls
    is no longer consumed, which restores the pre-activation default tool set.
    Expiry only ever widens back to the Run-start default — it never grants new
    authority — and slash activation is exempt because it encodes explicit user
    intent (D10).

    The registry is derived exclusively from ``runtime_skills`` materialized
    for the admitted Run. This middleware never consults global or file-backed
    Skill storage.
    """

    def __init__(
        self,
        *,
        runtime_skills: tuple[Skill, ...],
        runtime_skill_version_ids: tuple[str, ...],
        runtime_skills_container_path: str,
        slash_source_owner_token: str,
        available_skills: set[str] | None = None,
        skill_file_read_tool_names: Collection[str] | None = None,
        read_evidence_ttl_calls: int = 12,
    ) -> None:
        super().__init__()
        if not isinstance(slash_source_owner_token, str) or not slash_source_owner_token:
            raise ValueError("slash_source_owner_token must be a non-empty string")
        if not isinstance(runtime_skills_container_path, str) or not runtime_skills_container_path:
            raise ValueError("runtime_skills_container_path must be a non-empty string")
        if len(runtime_skill_version_ids) != len(runtime_skills) or any(not isinstance(version_id, str) or not version_id for version_id in runtime_skill_version_ids):
            raise ValueError("runtime_skill_version_ids must identify every exact runtime Skill")
        if type(read_evidence_ttl_calls) is not int or read_evidence_ttl_calls < 0:
            raise ValueError("read_evidence_ttl_calls must be a non-negative integer")

        self._runtime_skills = tuple(runtime_skills)
        self._runtime_skill_version_ids = tuple(runtime_skill_version_ids)
        self._runtime_skills_container_path = runtime_skills_container_path
        self._available_skills = set(available_skills) if available_skills is not None else None
        self._slash_source_owner_token = slash_source_owner_token
        self._skill_file_read_tool_names = frozenset(DEFAULT_SKILL_FILE_READ_TOOL_NAMES if skill_file_read_tool_names is None else skill_file_read_tool_names)
        self._read_evidence_ttl_calls = read_evidence_ttl_calls
        self._decision_owner_token = secrets.token_urlsafe(24)
        self._registry_by_path = {
            posixpath.normpath(skill.get_container_file_path(runtime_skills_container_path)): (skill, version_id)
            for skill, version_id in zip(
                self._runtime_skills,
                self._runtime_skill_version_ids,
                strict=True,
            )
        }

    def _active_policy(
        self,
        request: ModelRequest | ToolCallRequest,
    ) -> _PolicySignature:
        context = getattr(getattr(request, "runtime", None), "context", None)
        slash_path = read_slash_skill_source_path(
            context,
            owner_token=self._slash_source_owner_token,
        )
        if slash_path is not None:
            return _POLICY_SOURCE_SLASH, (slash_path,)

        paths = read_verified_skill_source_paths(
            context,
            owner_token=self._slash_source_owner_token,
            ttl_calls=self._read_evidence_ttl_calls,
        )
        if paths is None:
            logger.warning("Verified Skill read evidence is malformed or unauthenticated; failing closed")
            return _POLICY_SOURCE_INVALID, (_INVALID_ACTIVE_REFERENCE,)
        if paths:
            return _POLICY_SOURCE_VERIFIED_READ, paths
        return _POLICY_SOURCE_PASSIVE, ()

    def _record_successful_skill_read(
        self,
        request: ToolCallRequest,
        result: ToolMessage | Command,
    ) -> None:
        tool_name = str(request.tool_call.get("name") or "")
        if (
            tool_name not in self._skill_file_read_tool_names
            or not isinstance(result, ToolMessage)
            or getattr(result, "status", "success") == "error"
            or result.name != tool_name
            or not request.tool_call.get("id")
            or str(result.tool_call_id) != str(request.tool_call["id"])
            or not isinstance(result.content, str)
            or result.content.lstrip().startswith("Error:")
        ):
            return
        raw_path = _tool_call_path(request.tool_call)
        if not isinstance(raw_path, str):
            return
        path = posixpath.normpath(raw_path)
        exact_entry = self._registry_by_path.get(path)
        if exact_entry is None:
            return
        skill, _version_id = exact_entry
        if not skill.enabled or (self._available_skills is not None and skill.name not in self._available_skills):
            return
        write_verified_skill_source_path(
            self._runtime_context(request),
            path,
            owner_token=self._slash_source_owner_token,
        )

    def _active_skills_for_paths(
        self,
        paths: tuple[str, ...],
    ) -> tuple[list[Skill], bool]:
        if not paths:
            return [], False

        active: list[Skill] = []
        seen: set[str] = set()
        invalid_reference = False
        for path in paths:
            exact_entry = self._registry_by_path.get(posixpath.normpath(path))
            if exact_entry is None:
                logger.warning(
                    "Active Skill path is absent from the exact Run snapshot: %s",
                    path,
                )
                invalid_reference = True
                continue
            skill, _version_id = exact_entry
            if not skill.enabled:
                logger.warning(
                    "Active Skill is disabled in the exact Run snapshot: %s",
                    path,
                )
                invalid_reference = True
                continue
            if self._available_skills is not None and skill.name not in self._available_skills:
                logger.warning(
                    "Active Skill is outside the Agent allowlist: %s",
                    path,
                )
                invalid_reference = True
                continue
            if skill.name in seen:
                continue
            seen.add(skill.name)
            active.append(skill)

        if invalid_reference or not active:
            logger.warning("Active Skill references were not all authorized; failing closed")
            return [], True
        return active, False

    def _allowed_names_for_paths(
        self,
        paths: tuple[str, ...],
    ) -> set[str] | None:
        active_skills, policy_failed = self._active_skills_for_paths(paths)
        if policy_failed:
            return set(ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES)
        allowed = allowed_tool_names_for_skills(active_skills)
        if allowed is None:
            return None
        return allowed | set(ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES)

    def _exact_versions_for_paths(
        self,
        paths: tuple[str, ...],
    ) -> list[str | None]:
        """Resolve each active path to its admitted PostgreSQL version ID."""

        return [(exact_entry[1] if (exact_entry := self._registry_by_path.get(posixpath.normpath(path))) is not None else None) for path in paths]

    def _restriction_trace_state(
        self,
        policy: _PolicySignature,
        allowed: set[str] | None,
    ) -> dict | None:
        """Return the content-free observable state of one real Skill restriction."""

        if allowed is None:
            return None
        source, paths = policy
        skills: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for path in paths:
            exact_entry = self._registry_by_path.get(posixpath.normpath(path))
            if exact_entry is None:
                continue
            skill, version_id = exact_entry
            if not skill.enabled or skill.allowed_tools is None or (self._available_skills is not None and skill.name not in self._available_skills) or (skill.name, version_id) in seen:
                continue
            seen.add((skill.name, version_id))
            skills.append({"name": skill.name, "version_id": version_id})
        if not skills:
            return None
        return {
            "source": source,
            "skills": skills,
            "allowed_tool_count": len(allowed),
        }

    def _previous_restriction_trace_state(self, context: dict) -> dict | None:
        decision = context.get(SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY)
        if not isinstance(decision, dict) or decision.get("owner_token") != self._decision_owner_token or decision.get("run_id") != context.get("run_id"):
            return None
        state = decision.get("restriction_trace")
        return state if isinstance(state, dict) else None

    def _record_restriction_transition(
        self,
        context: dict,
        *,
        transition: str,
        state: dict,
        hook: str,
    ) -> None:
        journal = context.get("__run_journal")
        record = getattr(journal, "record_middleware", None)
        if not callable(record):
            return
        try:
            record(
                tag="skill_tool_policy",
                name=type(self).__name__,
                hook=hook,
                action=f"restriction_{transition}",
                changes={"transition": transition, **state},
            )
        except Exception:  # noqa: BLE001
            logger.debug("Failed to record Skill tool-policy transition", exc_info=True)

    def _record_restriction_state_change(
        self,
        context: dict,
        *,
        current: dict | None,
        hook: str,
    ) -> None:
        previous = self._previous_restriction_trace_state(context)
        if previous == current:
            return
        if previous is not None:
            self._record_restriction_transition(
                context,
                transition="exited",
                state=previous,
                hook=hook,
            )
        if current is not None:
            self._record_restriction_transition(
                context,
                transition="entered",
                state=current,
                hook=hook,
            )

    @staticmethod
    def _runtime_context(
        request: ModelRequest | ToolCallRequest,
    ) -> dict | None:
        context = getattr(getattr(request, "runtime", None), "context", None)
        return context if isinstance(context, dict) else None

    def _store_policy_decision(
        self,
        request: ModelRequest,
        policy: _PolicySignature,
        allowed: set[str] | None,
        *,
        hook: str,
    ) -> None:
        context = self._runtime_context(request)
        if context is None:
            return
        source, paths = policy
        restriction_trace = self._restriction_trace_state(policy, allowed)
        self._record_restriction_state_change(
            context,
            current=restriction_trace,
            hook=hook,
        )
        context[SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY] = {
            "version": _POLICY_DECISION_VERSION,
            "owner_token": self._decision_owner_token,
            "run_id": context.get("run_id"),
            "source": source,
            "active_paths": list(paths),
            "active_versions": self._exact_versions_for_paths(paths),
            "allowed_names": None if allowed is None else sorted(allowed),
            "restriction_trace": restriction_trace,
        }

    def _read_policy_decision(
        self,
        context: dict | None,
        policy: _PolicySignature,
    ) -> set[str] | None | object:
        if context is None:
            return _MISSING_POLICY_DECISION
        decision = context.get(SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY)
        if not isinstance(decision, dict):
            return _MISSING_POLICY_DECISION
        if type(decision.get("version")) is not int or decision["version"] != _POLICY_DECISION_VERSION:
            return _MISSING_POLICY_DECISION
        if not isinstance(decision.get("owner_token"), str) or decision["owner_token"] != self._decision_owner_token:
            return _MISSING_POLICY_DECISION

        source, paths = policy
        stored_source = decision.get("source")
        if not isinstance(stored_source, str) or stored_source not in _POLICY_SOURCES or stored_source != source:
            return _MISSING_POLICY_DECISION
        stored_paths = decision.get("active_paths")
        if not isinstance(stored_paths, list) or not all(isinstance(path, str) for path in stored_paths) or tuple(stored_paths) != paths:
            return _MISSING_POLICY_DECISION
        stored_versions = decision.get("active_versions")
        if not isinstance(stored_versions, list) or any(version is not None and not isinstance(version, str) for version in stored_versions) or stored_versions != self._exact_versions_for_paths(paths):
            return _MISSING_POLICY_DECISION

        allowed = decision.get("allowed_names")
        if allowed is None:
            return None
        if not isinstance(allowed, list) or not all(isinstance(name, str) for name in allowed):
            return _MISSING_POLICY_DECISION
        return set(allowed)

    def _allowed_names(
        self,
        request: ModelRequest | ToolCallRequest,
        *,
        policy: _PolicySignature | None = None,
    ) -> set[str] | None:
        resolved_policy = self._active_policy(request) if policy is None else policy
        context = self._runtime_context(request)
        decision = self._read_policy_decision(context, resolved_policy)
        if decision is not _MISSING_POLICY_DECISION:
            return decision
        return self._allowed_names_for_paths(resolved_policy[1])

    def _filter_model_request(
        self,
        request: ModelRequest,
        *,
        policy: _PolicySignature | None = None,
        refresh_decision: bool = False,
        hook: str = "model_call",
    ) -> ModelRequest:
        resolved_policy = self._active_policy(request) if policy is None else policy
        paths = resolved_policy[1]
        allowed = self._allowed_names_for_paths(paths) if refresh_decision else self._allowed_names(request, policy=resolved_policy)
        if refresh_decision:
            self._store_policy_decision(
                request,
                resolved_policy,
                allowed,
                hook=hook,
            )
        if allowed is None:
            return request

        tools = [tool for tool in request.tools if getattr(tool, "name", None) in allowed]
        if len(tools) < len(request.tools):
            source, active_paths = resolved_policy
            # Schema filtering is invisible to the model and the user, so every
            # narrowing decision is logged with the exact admitted version IDs
            # and the resulting tool budget to keep "the Agent suddenly lost a
            # tool" diagnosable.
            logger.info(
                "Skill tool policy narrowed lead tools: source=%s active_paths=%s versions=%s allowed=%d of %d ttl_calls=%d",
                source,
                list(active_paths),
                self._exact_versions_for_paths(active_paths),
                len(tools),
                len(request.tools),
                self._read_evidence_ttl_calls,
            )
        return request.override(tools=tools)

    def _restricting_skill_names(self, paths: tuple[str, ...]) -> list[str]:
        active_skills, _policy_failed = self._active_skills_for_paths(paths)
        restricting = sorted(skill.name for skill in active_skills if skill.allowed_tools is not None)
        return restricting or sorted(skill.name for skill in active_skills)

    def _verified_read_remaining_windows(
        self,
        request: ToolCallRequest,
        paths: tuple[str, ...],
    ) -> list[tuple[str, int]]:
        if self._read_evidence_ttl_calls <= 0:
            return []
        context = self._runtime_context(request)
        entries = read_verified_skill_source_entries(
            context,
            owner_token=self._slash_source_owner_token,
        )
        if entries is None:
            return []
        captured_by_path = dict(entries)
        current_seq = read_lead_model_call_seq(context)
        windows: list[tuple[str, int]] = []
        for path in paths:
            exact_entry = self._registry_by_path.get(posixpath.normpath(path))
            captured_seq = captured_by_path.get(path)
            if exact_entry is None or captured_seq is None:
                continue
            skill, _version_id = exact_entry
            if skill.allowed_tools is None:
                continue
            remaining = self._read_evidence_ttl_calls - (current_seq - captured_seq)
            if remaining > 0:
                windows.append((skill.name, remaining))
        return sorted(windows)

    def _blocked_tool_message(
        self,
        request: ToolCallRequest,
        *,
        allowed: set[str] | None,
        policy: _PolicySignature,
    ) -> ToolMessage | None:
        name = str(request.tool_call.get("name") or "")
        if allowed is None or not name or name in allowed:
            return None
        source, paths = policy
        detail: str
        if source == _POLICY_SOURCE_SLASH:
            skill_names = ", ".join(self._restricting_skill_names(paths)) or "the slash-activated Skill"
            detail = f"The slash-activated Skill ({skill_names}) declares 'allowed-tools', which narrows the lead toolset for the rest of this run."
        elif source == _POLICY_SOURCE_VERIFIED_READ:
            skill_names = ", ".join(self._restricting_skill_names(paths)) or "an active Skill"
            detail = (
                f"Active Skill(s) {skill_names} declare 'allowed-tools', which narrows the lead toolset"
                " while their read activation is fresh. Re-read that SKILL.md entry file"
                " (or activate the Skill with its /slash command) to refresh activation."
            )
            windows = self._verified_read_remaining_windows(request, paths)
            if windows:
                detail += " Remaining verified-read window(s): " + "; ".join(f"{skill_name} has {remaining} lead model call(s) remaining" for skill_name, remaining in windows) + "."
        else:
            detail = "Skill read evidence could not be validated, so only always-available tools remain until a Skill entry file is read again."
        return ToolMessage(
            content=(f"Error: Tool '{name}' is not allowed by the active skill policy. {detail}"),
            tool_call_id=str(request.tool_call.get("id") or "missing_tool_call_id"),
            name=name,
            status="error",
        )

    @staticmethod
    def _tool_search_policy_error(
        request: ToolCallRequest,
    ) -> ToolMessage:
        return ToolMessage(
            content=("Error: tool_search returned a result that could not be validated against the active skill policy."),
            tool_call_id=str(request.tool_call.get("id") or "missing_tool_call_id"),
            name=_TOOL_SEARCH_NAME,
            status="error",
        )

    def _filter_tool_search_result(
        self,
        request: ToolCallRequest,
        result: ToolMessage | Command,
        *,
        allowed: set[str] | None,
    ) -> ToolMessage | Command:
        """Remove denied schemas and promotions from ``tool_search`` output."""

        name = str(request.tool_call.get("name") or "")
        if name != _TOOL_SEARCH_NAME or allowed is None:
            return result
        if not isinstance(result, Command) or not isinstance(
            result.update,
            dict,
        ):
            logger.warning("Active-policy tool_search returned an unsupported result shape")
            return self._tool_search_policy_error(request)

        promoted = result.update.get("promoted")
        messages = result.update.get("messages")
        if not isinstance(promoted, dict) or not isinstance(messages, list) or len(messages) != 1:
            logger.warning("Active-policy tool_search omitted promoted/messages updates")
            return self._tool_search_policy_error(request)
        raw_names = promoted.get("names")
        if not isinstance(raw_names, list) or not all(isinstance(item, str) for item in raw_names):
            logger.warning("Active-policy tool_search returned malformed promoted names")
            return self._tool_search_policy_error(request)

        permitted_names = [item for item in raw_names if item in allowed]
        sanitized_messages: list[ToolMessage] = []
        for message in messages:
            if not isinstance(message, ToolMessage) or message.name != _TOOL_SEARCH_NAME:
                logger.warning("Active-policy tool_search returned an unexpected message")
                return self._tool_search_policy_error(request)
            content = message.content
            if raw_names:
                try:
                    schemas = json.loads(content) if isinstance(content, str) else None
                except json.JSONDecodeError:
                    schemas = None
                if not isinstance(schemas, list):
                    logger.warning("Active-policy tool_search schemas could not be filtered")
                    return self._tool_search_policy_error(request)
                filtered_schemas = [schema for schema in schemas if isinstance(schema, dict) and (schema.get("name") in permitted_names or (isinstance(schema.get("function"), dict) and schema["function"].get("name") in permitted_names))]
                content = (
                    json.dumps(
                        filtered_schemas,
                        indent=2,
                        ensure_ascii=False,
                    )
                    if filtered_schemas
                    else "No tools found matching the active skill policy."
                )
            elif not isinstance(content, str):
                logger.warning("Active-policy tool_search returned non-text content without promotions")
                return self._tool_search_policy_error(request)
            else:
                try:
                    unpromoted_schemas = json.loads(content)
                except json.JSONDecodeError:
                    unpromoted_schemas = None
                if unpromoted_schemas not in (None, []):
                    logger.warning("Active-policy tool_search returned schemas without matching promotions")
                    return self._tool_search_policy_error(request)
            sanitized_messages.append(message.model_copy(update={"content": content}))

        sanitized_update = dict(result.update)
        sanitized_update["promoted"] = {
            **promoted,
            "names": permitted_names,
        }
        sanitized_update["messages"] = sanitized_messages
        return Command(
            graph=result.graph,
            update=sanitized_update,
            resume=result.resume,
            goto=result.goto,
        )

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        policy = self._active_policy(request)
        filtered = self._filter_model_request(
            request,
            policy=policy,
            refresh_decision=True,
            hook="wrap_model_call",
        )
        return handler(filtered)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        policy = self._active_policy(request)
        filtered = self._filter_model_request(
            request,
            policy=policy,
            refresh_decision=True,
            hook="awrap_model_call",
        )
        return await handler(filtered)

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest],
            ToolMessage | Command,
        ],
    ) -> ToolMessage | Command:
        policy = self._active_policy(request)
        if not policy[1]:
            result = handler(request)
            self._record_successful_skill_read(request, result)
            return result
        allowed = self._allowed_names(request, policy=policy)
        blocked = self._blocked_tool_message(request, allowed=allowed, policy=policy)
        if blocked is not None:
            return blocked
        result = self._filter_tool_search_result(
            request,
            handler(request),
            allowed=allowed,
        )
        self._record_successful_skill_read(request, result)
        return result

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest],
            Awaitable[ToolMessage | Command],
        ],
    ) -> ToolMessage | Command:
        policy = self._active_policy(request)
        if not policy[1]:
            result = await handler(request)
            self._record_successful_skill_read(request, result)
            return result
        allowed = self._allowed_names(request, policy=policy)
        blocked = self._blocked_tool_message(request, allowed=allowed, policy=policy)
        if blocked is not None:
            return blocked
        result = await handler(request)
        result = self._filter_tool_search_result(
            request,
            result,
            allowed=allowed,
        )
        self._record_successful_skill_read(request, result)
        return result
