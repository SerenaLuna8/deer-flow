"""Middleware for skill activation: explicit slash + in-context secret binding."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import posixpath
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, override

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from deerflow.constants import DEFAULT_SKILLS_CONTAINER_PATH
from deerflow.private_scope import PrivateResourceScope
from deerflow.runtime.secret_context import (
    _SECRETS_BINDING_AUDIT_KEY,
    _SLASH_SKILL_ACTIVATION_RUN_KEY,
    ACTIVE_SECRET_SOURCES_CONTEXT_KEY,
    ACTIVE_SECRETS_CONTEXT_KEY,
    SKILL_SECRET_PROVIDER_CONTEXT_KEY,
    extract_request_secrets,
    extract_skill_scoped_secrets,
    read_slash_skill_source_path,
    write_slash_skill_source_path,
)
from deerflow.runtime.skill_context_authority import (
    read_verified_skill_source_paths,
)
from deerflow.skills.slash import parse_slash_skill_reference, resolve_slash_skill
from deerflow.skills.types import SKILL_MD_FILE, SecretRequirement, Skill, SkillCategory
from deerflow.utils.messages import get_original_user_content_text, is_real_user_message

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_SLASH_SKILL_ACTIVATION_KEY = "slash_skill_activation"
_SLASH_SKILL_ACTIVATION_TARGET_ID_KEY = "slash_skill_activation_target_id"
_SLASH_SKILL_ACTIVATION_MARKER_VERSION = 1

# _SECRETS_BINDING_AUDIT_KEY: last audited binding (skill and secret names only,
# never values) so unchanged bindings are not re-recorded each call.
# The shared slash-source context contract holds the latest slash activation,
# ONLY the activated skill's canonical container path (never its declared
# secrets — those are read from the exact Run registry on each call). The
# injection set is recomputed every model call, but a slash-activated skill must
# stay bound for the rest of the run — the model's tool loop issues many model
# calls after the single activation call (#3861 semantics). Both live in
# secret_context so they are covered by REDACTED_CONTEXT_KEYS in one place.
# _SLASH_SKILL_ACTIVATION_RUN_KEY records the authenticated private Run/message
# identity whose read, reminder, and activation audit already fired. It never
# suppresses the per-call secret-binding recomputation.


@dataclass(frozen=True, slots=True)
class _Activation:
    skill_name: str
    category: str
    container_file_path: str
    skill_content: str
    content_hash: str
    remaining_text: str
    editable: bool
    required_secrets: tuple[SecretRequirement, ...] = ()


@dataclass(frozen=True, slots=True)
class _ActivationResolution:
    activation: _Activation | None = None
    failure_message: str | None = None


def is_slash_skill_activation_reminder(message: object) -> bool:
    """Return whether a message is hidden slash-skill activation context."""
    return isinstance(message, HumanMessage) and bool(message.additional_kwargs.get(_SLASH_SKILL_ACTIVATION_KEY))


def _is_user_activation_target(message: object) -> bool:
    return is_real_user_message(message)


class SkillActivationMiddleware(AgentMiddleware):
    """Inject full SKILL.md content when the user explicitly types /skill-name."""

    def __init__(
        self,
        *,
        available_skills: set[str] | None = None,
        app_config: AppConfig | None = None,
        user_id: str | None = None,
        runtime_skills: tuple[Skill, ...] | None = None,
        runtime_skills_root: Path | None = None,
        runtime_skills_container_path: str | None = None,
        slash_source_owner_token: str | None = None,
    ) -> None:
        super().__init__()
        if slash_source_owner_token is not None and (not isinstance(slash_source_owner_token, str) or not slash_source_owner_token):
            raise ValueError("slash_source_owner_token must be a non-empty string")
        self._available_skills = set(available_skills) if available_skills is not None else None
        self._app_config = app_config
        self._user_id = user_id
        self._runtime_skills = tuple(runtime_skills or ())
        self._runtime_skills_root = runtime_skills_root
        self._runtime_skills_container_path = runtime_skills_container_path
        self._slash_source_owner_token = slash_source_owner_token or secrets.token_urlsafe(24)

    @staticmethod
    def _read_skill_content(skill_file: Path, skills_root: Path) -> str:
        if skill_file.name != SKILL_MD_FILE:
            raise ValueError(f"Expected {SKILL_MD_FILE}, got {skill_file.name}")
        resolved_file = skill_file.resolve()
        resolved_root = skills_root.resolve()
        try:
            resolved_file.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("Resolved skill file must stay within the run Skill root.") from exc
        if not resolved_file.is_file():
            raise FileNotFoundError(resolved_file)
        return resolved_file.read_text(encoding="utf-8")

    def _resolve_activation(self, text: str) -> _ActivationResolution | None:
        reference = parse_slash_skill_reference(text)
        if reference is None:
            return None

        skills = list(self._runtime_skills)
        skill = next((candidate for candidate in skills if candidate.name == reference.name), None)
        if skill is None:
            return _ActivationResolution(failure_message=f"Skill `/{reference.name}` is not installed.")
        if not skill.enabled:
            return _ActivationResolution(failure_message=f"Skill `/{reference.name}` is installed but disabled. Enable it before using slash activation.")
        if self._available_skills is not None and reference.name not in self._available_skills:
            return _ActivationResolution(failure_message=f"Skill `/{reference.name}` is not available for this agent.")

        resolved = resolve_slash_skill(
            text,
            skills,
            available_skills=self._available_skills,
            container_base_path=(self._runtime_skills_container_path or str(self._runtime_skills_root or DEFAULT_SKILLS_CONTAINER_PATH)),
        )
        if resolved is None:
            return _ActivationResolution(failure_message=f"Skill `/{reference.name}` could not be resolved.")

        try:
            if self._runtime_skills_root is None:
                raise ValueError("run Skill root is unavailable")
            skills_root = self._runtime_skills_root
            skill_content = self._read_skill_content(
                resolved.skill.skill_file,
                skills_root,
            )
        except (OSError, ValueError):
            logger.exception("Failed to read slash-activated skill %s", resolved.skill.name)
            return _ActivationResolution(failure_message=f"Skill `/{reference.name}` could not be loaded safely. Please check the skill installation.")

        content_hash = hashlib.sha256(skill_content.encode("utf-8")).hexdigest()
        # Persisted run-exact project Skills are immutable even though their
        # isolated filesystem layout uses the CUSTOM category directory.
        editable = resolved.skill.category == SkillCategory.CUSTOM and not resolved.skill.runtime_read_only
        return _ActivationResolution(
            activation=_Activation(
                skill_name=resolved.skill.name,
                category=str(resolved.skill.category),
                container_file_path=resolved.container_file_path,
                skill_content=skill_content,
                content_hash=content_hash,
                remaining_text=resolved.remaining_text,
                editable=editable,
                required_secrets=tuple(resolved.skill.required_secrets or ()),
            )
        )

    @staticmethod
    def _build_activation_reminder(activation: _Activation) -> str:
        user_request = activation.remaining_text or ("No additional task text was provided after the slash skill command. Ask the user what they want to do with this skill if the next step is unclear.")
        escaped_user_request = html.escape(user_request, quote=False)
        escaped_skill_content = html.escape(activation.skill_content, quote=False)
        escaped_skill_name = html.escape(activation.skill_name, quote=True)
        escaped_category = html.escape(activation.category, quote=True)
        escaped_path = html.escape(activation.container_file_path, quote=True)
        escaped_content_hash = html.escape(activation.content_hash, quote=True)
        editable_str = "true" if activation.editable else "false"
        return f"""<slash_skill_activation>
The user explicitly activated the `{escaped_skill_name}` skill for this turn.
Treat the task text as:
<user_request>
{escaped_user_request}
</user_request>

Follow this skill before choosing a general workflow. Load supporting resources from the same skill directory only when needed.

<skill name="{escaped_skill_name}" category="{escaped_category}" path="{escaped_path}" sha256="{escaped_content_hash}" editable="{editable_str}">
<skill_content encoding="xml-escaped">
{escaped_skill_content}
</skill_content>
</skill>
</slash_skill_activation>"""

    @staticmethod
    def _has_existing_activation_for_target(messages: list, target_index: int, target: HumanMessage) -> bool:
        if target_index <= 0:
            return False

        if target.id:
            for previous in messages[:target_index]:
                if not is_slash_skill_activation_reminder(previous):
                    continue
                target_id = previous.additional_kwargs.get(_SLASH_SKILL_ACTIVATION_TARGET_ID_KEY)
                if target_id == target.id or previous.id == f"{target.id}__slash_activation":
                    return True

        previous = messages[target_index - 1]
        return is_slash_skill_activation_reminder(previous)

    @staticmethod
    def _run_context(request: ModelRequest) -> dict | None:
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        return context if isinstance(context, dict) else None

    @staticmethod
    def _activation_message_key(target: HumanMessage) -> str:
        if isinstance(target.id, str) and target.id:
            return f"id:{target.id}"
        content = get_original_user_content_text(
            target.content,
            target.additional_kwargs,
        )
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _activation_run_marker(
        self,
        run_context: dict | None,
        target: HumanMessage,
    ) -> dict[str, object] | None:
        """Build an authenticated private Run/message identity.

        Run-once deduplication is intentionally unavailable without the exact
        Worker-issued private scope and run id. In that degraded case activation
        continues normally on every model call rather than trusting partial or
        caller-shaped identity.
        """

        if not isinstance(run_context, dict):
            return None
        private_scope = run_context.get("private_scope")
        run_id = run_context.get("run_id")
        if type(private_scope) is not PrivateResourceScope:
            return None
        project_id = private_scope.project_id
        owner_user_id = private_scope.owner_user_id
        if not isinstance(project_id, str) or not project_id or not isinstance(owner_user_id, str) or not owner_user_id or type(private_scope.membership_version) is not int or not isinstance(run_id, str) or not run_id:
            return None
        return {
            "version": _SLASH_SKILL_ACTIVATION_MARKER_VERSION,
            "owner_token": self._slash_source_owner_token,
            "project_id": project_id,
            "owner_user_id": owner_user_id,
            "run_id": run_id,
            "message_key": self._activation_message_key(target),
        }

    @staticmethod
    def _already_activated(
        run_context: dict | None,
        marker: dict[str, object] | None,
    ) -> bool:
        if not isinstance(run_context, dict) or marker is None:
            return False
        recorded = run_context.get(_SLASH_SKILL_ACTIVATION_RUN_KEY)
        return type(recorded) is dict and recorded == marker

    def _find_activation_target(
        self,
        messages: list,
        *,
        run_context: dict | None = None,
    ) -> (
        tuple[
            int,
            HumanMessage,
            _ActivationResolution,
            dict[str, object] | None,
        ]
        | None
    ):
        if not messages:
            return None

        target_index = next((idx for idx in range(len(messages) - 1, -1, -1) if _is_user_activation_target(messages[idx])), None)
        if target_index is None:
            return None

        target = messages[target_index]
        if target is None:
            return None
        if self._has_existing_activation_for_target(messages, target_index, target):
            return None
        marker = self._activation_run_marker(run_context, target)
        if self._already_activated(run_context, marker):
            return None

        content = get_original_user_content_text(target.content, target.additional_kwargs)
        resolution = self._resolve_activation(content)
        if resolution is None:
            return None
        return target_index, target, resolution, marker

    @staticmethod
    def _record_activation(request: ModelRequest, activation: _Activation, *, hook: str) -> None:
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        journal = context.get("__run_journal") if isinstance(context, dict) else None
        if journal is None:
            return
        try:
            journal.record_middleware(
                "skill_activation",
                name="SkillActivationMiddleware",
                hook=hook,
                action="activate",
                changes={
                    "skill_name": activation.skill_name,
                    "category": activation.category,
                    "path": activation.container_file_path,
                    "content_hash": activation.content_hash,
                },
            )
        except Exception:
            logger.debug("Failed to record slash skill activation audit event", exc_info=True)

    def _prepare_model_request(self, request: ModelRequest, *, hook: str) -> tuple[ModelRequest | AIMessage | None, _Activation | None]:
        run_context = self._run_context(request)
        target_and_resolution = self._find_activation_target(
            list(request.messages),
            run_context=run_context,
        )
        if target_and_resolution is None:
            return None, None

        target_index, target, resolution, marker = target_and_resolution
        if resolution.failure_message:
            return AIMessage(content=resolution.failure_message), None

        activation = resolution.activation
        if activation is None:
            return None, None

        logger.info(
            "SkillActivationMiddleware: activating slash skill %s category=%s path=%s hash=%s",
            activation.skill_name,
            activation.category,
            activation.container_file_path,
            activation.content_hash,
        )
        activation_msg = self._make_activation_message(target, self._build_activation_reminder(activation))
        messages = list(request.messages)
        messages.insert(target_index, activation_msg)
        if run_context is not None and marker is not None:
            run_context[_SLASH_SKILL_ACTIVATION_RUN_KEY] = marker
        self._record_activation(request, activation, hook=hook)
        return request.override(messages=messages), activation

    def _handle_model_request(self, request: ModelRequest, *, hook: str) -> ModelRequest | AIMessage:
        prepared, activation = self._prepare_model_request(request, hook=hook)
        if isinstance(prepared, AIMessage):
            return prepared
        effective = prepared if prepared is not None else request
        self._resolve_secret_bindings(effective, activation, hook=hook)
        return effective

    def _resolve_secret_bindings(self, request: ModelRequest, activation: _Activation | None, *, hook: str) -> None:
        """Recompute the per-run secret injection set (binding point A+, #3861/#3914).

        Sources, unioned on every model call:

        - the most recent slash activation of this run (persisted as a source on
          the run context so the whole tool loop after the activation call keeps
          the binding — a new slash activation replaces it). The slash source is
          validated once, at activation (enabled + allowlist checks in
          ``_resolve_activation``), and deliberately NOT re-validated per call:
          slash is a run-scoped commitment made by the user, and it dies with
          the run anyway;
        - exact Skill entry files successfully read during this private Run,
          recorded as middleware-authenticated runtime evidence and
          re-validated against the admitted registry on each call: enabled,
          runtime-allowed for this agent, and not opted out via
          ``secrets-autonomous: false``. Durable ``ThreadState.skill_context``
          is observational only and is never a secret-binding authority.
          Slash activation is exempt from the opt-out because it is the
          explicit-ceremony path.

        The set is recomputed and REPLACED each call, so a skill evicted from
        skill_context, or a caller that stops supplying a value, loses its
        injection on the next call automatically. Injected values come from the
        exact admitted Skill-path carrier supplied by the private Worker,
        falling back to the legacy caller request carrier
        (``context.secrets``) only when that Skill has no scoped entry. They
        never come from the host environment, which
        ``env_policy.build_sandbox_env`` scrubs before injection. Secret
        *values* are never logged; the audit journal records names only. If
        autonomous Skills claim the same env name with incompatible bindings,
        that name fails closed. The current explicit slash Skill takes
        deterministic precedence for names it declares.
        """
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        if not isinstance(context, dict):
            return

        # The slash source records the canonical container path plus a
        # middleware-chain-local owner token — never declared secrets. Both
        # consumers authenticate the source and resolve the exact run registry
        # Skill by path, so caller-mergeable context cannot forge activation.
        if activation is not None:
            write_slash_skill_source_path(
                context,
                activation.container_file_path,
                owner_token=self._slash_source_owner_token,
            )

        request_secrets = extract_request_secrets(context)
        skill_scoped_secrets = extract_skill_scoped_secrets(context)
        private_provider = context.get(SKILL_SECRET_PROVIDER_CONTEXT_KEY) if "private_scope" in context else None
        sources: list[tuple[str, str, tuple[SecretRequirement, ...], bool]] = []
        if request_secrets or skill_scoped_secrets or callable(private_provider):
            registry = self._load_skill_registry_by_path()
            if registry is not None:
                # Verified read sources are collected first. An explicit slash
                # activation is the current user-selected Skill and therefore
                # takes precedence if two active Skills declare the same env
                # name with different scoped values.
                verified_paths = read_verified_skill_source_paths(
                    context,
                    owner_token=self._slash_source_owner_token,
                )
                if verified_paths is None:
                    logger.warning("Verified Skill read evidence is malformed or unauthenticated; suppressing autonomous secret binding")
                else:
                    sources.extend(
                        self._verified_read_secret_sources(
                            verified_paths,
                            registry,
                        )
                    )

                # Slash source: exempt from the ``secrets-autonomous`` opt-out
                # (explicit ceremony), but still enabled + allowlist checked.
                slash_path = read_slash_skill_source_path(
                    context,
                    owner_token=self._slash_source_owner_token,
                )
                slash_skill = self._resolve_registry_skill(registry, slash_path, require_autonomous=False)
                if slash_skill is not None:
                    sources.append((slash_skill.name, posixpath.normpath(slash_path), tuple(slash_skill.required_secrets), True))

        if callable(private_provider):
            # Private Skill Credential values are deliberately absent during
            # model calls.  Persist only the validated name/path activation plan
            # so the async bash boundary can select values from a freshly
            # revalidated one-command carrier.
            context[ACTIVE_SECRET_SOURCES_CONTEXT_KEY] = tuple(
                (
                    skill_name,
                    skill_path,
                    tuple(requirement.name for requirement in requirements),
                    is_explicit,
                )
                for skill_name, skill_path, requirements, is_explicit in sources
            )
            context.pop(ACTIVE_SECRETS_CONTEXT_KEY, None)
            return

        context.pop(ACTIVE_SECRET_SOURCES_CONTEXT_KEY, None)
        injected: dict[str, str] = {}
        bound_skills: set[str] = set()
        missing: dict[str, list[str]] = {}
        claims: dict[str, list[tuple[str, str | None, bool, bool]]] = {}
        for skill_name, skill_path, requirements, is_explicit in sources:
            source_secrets = skill_scoped_secrets[skill_path] if skill_path in skill_scoped_secrets else request_secrets
            for req in requirements:
                if req.name in source_secrets:
                    claims.setdefault(req.name, []).append((skill_name, source_secrets[req.name], True, is_explicit))
                else:
                    claims.setdefault(req.name, []).append((skill_name, None, False, is_explicit))
                    if not req.optional:
                        missing.setdefault(skill_name, []).append(req.name)

        conflicts: dict[str, list[str]] = {}
        for secret_name, secret_claims in claims.items():
            explicit_claims = [claim for claim in secret_claims if claim[3]]
            if explicit_claims:
                # At most one slash Skill is current. Its declaration reserves
                # this env name even when its value is absent, preventing a
                # loaded autonomous Skill's value from crossing into it.
                skill_name, value, supplied, _ = explicit_claims[-1]
                if supplied:
                    injected[secret_name] = value or ""
                    bound_skills.add(skill_name)
                continue

            skill_names = sorted({claim[0] for claim in secret_claims})
            supplied_values = {claim[1] for claim in secret_claims if claim[2]}
            all_supplied = all(claim[2] for claim in secret_claims)
            if len(skill_names) > 1 and (not all_supplied or len(supplied_values) > 1):
                # A flat subprocess env cannot safely represent two active
                # autonomous Skills that claim the same name with different
                # (or partially missing) scoped bindings. Drop that name rather
                # than expose one Skill's Credential to another.
                conflicts[secret_name] = skill_names
                continue
            if all_supplied and supplied_values:
                injected[secret_name] = next(iter(supplied_values)) or ""
                bound_skills.update(skill_names)

        if injected:
            context[ACTIVE_SECRETS_CONTEXT_KEY] = injected
        else:
            context.pop(ACTIVE_SECRETS_CONTEXT_KEY, None)

        audit_state = {
            "skills": sorted(bound_skills),
            "secrets": sorted(injected),
            "missing": {name: sorted(values) for name, values in sorted(missing.items())},
            "conflicts": {name: values for name, values in sorted(conflicts.items())},
        }
        previous = context.get(_SECRETS_BINDING_AUDIT_KEY)
        if previous == audit_state:
            return
        if previous is None and not injected and not missing and not conflicts:
            return
        context[_SECRETS_BINDING_AUDIT_KEY] = audit_state
        for skill_name, names in sorted(missing.items()):
            logger.warning(
                "Skill %s is active but required secrets are missing from the runtime carrier: %s",
                skill_name,
                ", ".join(names),
            )
        for secret_name, skill_names in sorted(conflicts.items()):
            logger.warning(
                "Active Skills have conflicting runtime bindings for secret %s; injection was suppressed for: %s",
                secret_name,
                ", ".join(skill_names),
            )
        self._record_secret_binding(context, audit_state, hook=hook)

    def _load_skill_registry_by_path(self) -> dict[str, Skill]:
        """Build the exact Run registry keyed by normalized container path.

        ``runtime_skills`` is materialized from the immutable PostgreSQL Run
        snapshot. No global or file-backed Skill storage is consulted.

        Paths are normalized so a non-canonical ``container_path`` config (e.g. a
        trailing slash) still matches the canonical path captured in
        ``skill_context``. An unavailable exact registry is represented by an
        empty mapping, so both slash and in-context sources fail closed.
        """
        if not self._runtime_skills or not self._runtime_skills_container_path:
            return {}
        return {posixpath.normpath(skill.get_container_file_path(self._runtime_skills_container_path)): skill for skill in self._runtime_skills}

    def _resolve_registry_skill(self, registry: dict[str, Skill], path: object, *, require_autonomous: bool) -> Skill | None:
        """Resolve a path to an exact runtime Skill eligible for secret
        binding, or ``None``.

        Match strictly by normalized container file path — never by name. A
        by-name fallback would be a confused deputy: ActWeave lets a custom skill
        shadow a same-named public/legacy one (load_skills de-dupes by name,
        custom wins), so a reference to public/foo could bind the custom foo's
        secrets. A path that does not resolve simply binds nothing (the safe
        direction), which also fails closed on a caller-forged path (#3938).

        Gates: the skill must be enabled, declare secrets, and be allowlisted for
        this agent. ``require_autonomous`` additionally enforces the
        ``secrets-autonomous`` opt-out for the in-context path; the slash path
        passes ``False`` because explicit activation is the ceremony that opt-out
        is meant to preserve.
        """
        if not isinstance(path, str) or not path:
            return None
        skill = registry.get(posixpath.normpath(path))
        if skill is None or not skill.enabled or not skill.required_secrets:
            return None
        if require_autonomous and not skill.secrets_autonomous:
            return None
        if self._available_skills is not None and skill.name not in self._available_skills:
            return None
        return skill

    def _verified_read_secret_sources(
        self,
        paths: tuple[str, ...],
        registry: dict[str, Skill],
    ) -> list[tuple[str, str, tuple[SecretRequirement, ...], bool]]:
        """Map authenticated Run-scoped reads to declared-secret sources."""
        sources: list[tuple[str, str, tuple[SecretRequirement, ...], bool]] = []
        seen: set[str] = set()
        for path in paths:
            skill = self._resolve_registry_skill(registry, path, require_autonomous=True)
            if skill is None or skill.name in seen:
                continue
            seen.add(skill.name)
            sources.append((skill.name, posixpath.normpath(path), tuple(skill.required_secrets), False))
        return sources

    @staticmethod
    def _record_secret_binding(context: dict, audit_state: dict, *, hook: str) -> None:
        journal = context.get("__run_journal")
        if journal is None:
            return
        try:
            journal.record_middleware(
                "skill_secrets",
                name="SkillActivationMiddleware",
                hook=hook,
                action="bind_secrets",
                changes=audit_state,
            )
        except Exception:
            logger.debug("Failed to record skill secret binding audit event", exc_info=True)

    @staticmethod
    def _make_activation_message(target: HumanMessage, activation_content: str) -> HumanMessage:
        stable_id = target.id or str(uuid.uuid4())
        additional_kwargs = {
            "hide_from_ui": True,
            _SLASH_SKILL_ACTIVATION_KEY: True,
        }
        if target.id:
            additional_kwargs[_SLASH_SKILL_ACTIVATION_TARGET_ID_KEY] = target.id
        return HumanMessage(
            content=activation_content,
            id=f"{stable_id}__slash_activation",
            additional_kwargs=additional_kwargs,
        )

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | AIMessage:
        prepared = self._handle_model_request(request, hook="wrap_model_call")
        if isinstance(prepared, AIMessage):
            return prepared
        return handler(prepared)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | AIMessage:
        prepared = await asyncio.to_thread(self._handle_model_request, request, hook="awrap_model_call")
        if isinstance(prepared, AIMessage):
            return prepared
        return await handler(prepared)
