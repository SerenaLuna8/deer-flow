"""Closed projection of one parent execution into a delegated Agent context.

The graph-local task Adapter selects the delegated Agent's graph/profile inputs
and asks this module to project the exact per-call parent binding.  The result
is opaque and immutable; the internal graph runner consumes it without
re-selecting raw runtime-context keys.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, cast

from deerflow.file_authority import (
    AuthorityManifest,
    AuthorityManifestEntry,
    RunFileAuthority,
)
from deerflow.guardrails.provider import copy_guardrail_attribution
from deerflow.runtime.context_carrier import RuntimeContextCarrier
from deerflow.runtime.context_keys import RuntimeContextKeys
from deerflow.runtime.host_execution_approval import (
    HostExecutionApprovalPort,
    HostExecutionApprovalResult,
    HostExecutionChannelIdentityMode,
    HostExecutionOutcome,
    HostExecutionPlan,
)
from deerflow.runtime.recovered_llm_failures import (
    RunRecoveredLLMFailureRecorder,
)
from deerflow.sandbox.sandbox_provider import RunMountReleaseOutcome
from deerflow.subagents.binding import (
    ConfiguredLeadParentExecutionProfile,
    EmbeddedParentExecutionProfile,
    ParentExecutionBinding,
    PrivateRunParentExecutionProfile,
    SdkParentExecutionProfile,
    invoke_parent_operation_on_owner_loop,
)
from deerflow.trace_context import normalize_trace_id


def _trusted_skill_scoped_secrets(
    parent_context: Mapping[str, Any],
) -> dict[str, dict[str, str]] | None:
    """Copy only the closed Worker-installed Skill secret carrier."""

    raw = parent_context.get(RuntimeContextKeys.SKILL_SCOPED_SECRETS)
    if not isinstance(raw, Mapping):
        return None
    copied: dict[str, dict[str, str]] = {}
    for path, values in raw.items():
        if not isinstance(path, str) or not path or not isinstance(values, Mapping):
            return None
        env: dict[str, str] = {}
        for name, value in values.items():
            if not isinstance(name, str) or not isinstance(value, str):
                return None
            env[name] = value
        copied[path] = env
    return copied


def _profile_app_config(profile: object) -> object | None:
    if type(profile) in {
        EmbeddedParentExecutionProfile,
        ConfiguredLeadParentExecutionProfile,
        PrivateRunParentExecutionProfile,
    }:
        return profile.app_config
    if type(profile) is SdkParentExecutionProfile:
        return None
    raise TypeError("unsupported parent execution profile")


class _OwnerLoopAuthorityProxy:
    """Marshal opaque authorization-boundary methods to their owner loop."""

    def __init__(self, target: object, binding: ParentExecutionBinding) -> None:
        self._target = target
        self._binding = binding

    def __getattr__(self, name: str):
        target = getattr(self._target, name)
        if not callable(target):
            return target

        async def invoke(*args, **kwargs):
            return await invoke_parent_operation_on_owner_loop(
                self._binding,
                target,
                *args,
                **kwargs,
            )

        return invoke


class _OwnerLoopFileAuthorityProxy:
    """Keep async private Run file operations on their owning Worker loop."""

    def __init__(
        self,
        target: RunFileAuthority,
        binding: ParentExecutionBinding,
        *,
        delegated_output_root: str | None = None,
    ) -> None:
        self._target = target
        self._binding = binding
        if delegated_output_root is not None:
            prefix = "/mnt/user-data/workspace/.deerflow/subagents/"
            path = PurePosixPath(delegated_output_root)
            relative = delegated_output_root.removeprefix(prefix)
            parts = relative.split("/")
            if (
                not delegated_output_root.startswith(prefix)
                or path.as_posix() != delegated_output_root
                or ".." in path.parts
                or len(parts) != 2
                or len(parts[0]) != 32
                or any(character not in "0123456789abcdef" for character in parts[0])
                or parts[1] != "outputs"
            ):
                raise ValueError("Invalid delegated output root")
        self._delegated_output_root = delegated_output_root

    @property
    def sandbox_id(self) -> str | None:
        return self._target.sandbox_id

    @property
    def delegated_output_root(self) -> str | None:
        return self._delegated_output_root

    async def restore(self) -> AuthorityManifest:
        return await invoke_parent_operation_on_owner_loop(
            self._binding,
            self._target.restore,
        )

    def thread_data_paths(self) -> dict[str, str]:
        paths = self._target.thread_data_paths()
        if self._delegated_output_root is None:
            return paths
        return {
            **paths,
            "outputs_path": self._delegated_output_root,
        }

    def visible_uploads(self) -> tuple[dict[str, object], ...]:
        return self._target.visible_uploads()

    def record_current_upload_ids(self, file_ids: tuple[str, ...]) -> None:
        self._target.record_current_upload_ids(file_ids)

    def current_upload_ids(self) -> tuple[str, ...]:
        return self._target.current_upload_ids()

    def current_uploads(self) -> tuple[AuthorityManifestEntry, ...]:
        return self._target.current_uploads()

    def authorizes_run_read_only_mount_path(
        self,
        *,
        run_id: str,
        path: str,
    ) -> bool:
        return self._target.authorizes_run_read_only_mount_path(
            run_id=run_id,
            path=path,
        )

    async def write_output(self, relative_path: str, content: bytes) -> str:
        if self._delegated_output_root is not None:
            writer = getattr(self._target, "write_delegated_output", None)
            if not callable(writer):
                raise RuntimeError("Private file authority is unavailable")
            physical_path = await invoke_parent_operation_on_owner_loop(
                self._binding,
                writer,
                self._delegated_output_root,
                relative_path,
                content,
            )
            return self._delegated_alias(
                physical_path,
                physical_root=self._delegated_output_root,
                alias_root="/mnt/user-data/outputs",
            )
        return await invoke_parent_operation_on_owner_loop(
            self._binding,
            self._target.write_output,
            relative_path,
            content,
        )

    async def write_internal(self, relative_path: str, content: bytes) -> str:
        if self._delegated_output_root is not None:
            writer = getattr(self._target, "write_delegated_internal", None)
            if not callable(writer):
                raise RuntimeError("Private file authority is unavailable")
            storage_relative_path = relative_path if relative_path.startswith(".tool-results/") else f".tool-results/{relative_path}"
            physical_path = await invoke_parent_operation_on_owner_loop(
                self._binding,
                writer,
                self._delegated_output_root,
                storage_relative_path,
                content,
            )
            capture_root = str(PurePosixPath(self._delegated_output_root).parent)
            return self._delegated_alias(
                physical_path,
                physical_root=f"{capture_root}/internal/.tool-results",
                alias_root="/mnt/user-data/workspace/.tool-results",
            )
        return await invoke_parent_operation_on_owner_loop(
            self._binding,
            self._target.write_internal,
            relative_path,
            content,
        )

    @staticmethod
    def _delegated_alias(
        physical_path: object,
        *,
        physical_root: str,
        alias_root: str,
    ) -> str:
        """Expose a canonical alias only after exact-scope path validation."""

        if type(physical_path) is not str:
            raise RuntimeError("Private file authority returned an invalid path")
        path = PurePosixPath(physical_path)
        root = PurePosixPath(physical_root)
        if not path.is_absolute() or path.as_posix() != physical_path or ".." in path.parts:
            raise RuntimeError("Private file authority returned an invalid path")
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(
                "Private file authority returned an invalid path",
            ) from exc
        if not relative.parts:
            raise RuntimeError("Private file authority returned an invalid path")
        return f"{alias_root}/{relative.as_posix()}"

    async def record_presented_paths(
        self,
        presented_paths: tuple[str, ...],
        *,
        tool_call_id: str,
    ) -> None:
        if self._delegated_output_root is not None:
            raise ValueError(
                "Sub-Agent scratch files must be promoted by the Lead before presentation",
            )
        await invoke_parent_operation_on_owner_loop(
            self._binding,
            self._target.record_presented_paths,
            presented_paths,
            tool_call_id=tool_call_id,
        )

    async def output_delivery_status(self) -> str:
        return await invoke_parent_operation_on_owner_loop(
            self._binding,
            self._target.output_delivery_status,
        )

    async def finalize(self) -> object:
        return await invoke_parent_operation_on_owner_loop(
            self._binding,
            self._target.finalize,
        )

    async def mark_failed(self) -> None:
        await invoke_parent_operation_on_owner_loop(
            self._binding,
            self._target.mark_failed,
        )

    async def release(self) -> RunMountReleaseOutcome | None:
        return await invoke_parent_operation_on_owner_loop(
            self._binding,
            self._target.release,
        )


class _OwnerLoopHostExecutionApprovalProxy:
    """Preserve the typed approval port across the subagent loop boundary."""

    def __init__(
        self,
        target: HostExecutionApprovalPort,
        binding: ParentExecutionBinding,
    ) -> None:
        self._target = target
        self._binding = binding

    async def request_host_execution(
        self,
        plan: HostExecutionPlan,
    ) -> HostExecutionApprovalResult:
        return await invoke_parent_operation_on_owner_loop(
            self._binding,
            self._target.request_host_execution,
            plan,
        )

    async def complete_host_execution(
        self,
        approval_id: str,
        outcome: HostExecutionOutcome,
    ) -> None:
        await invoke_parent_operation_on_owner_loop(
            self._binding,
            self._target.complete_host_execution,
            approval_id,
            outcome,
        )


class _OwnerLoopCheckerProxy:
    """Marshal a trusted callable authorization fallback to its owner loop."""

    def __init__(self, target: object, binding: ParentExecutionBinding) -> None:
        self._target = target
        self._binding = binding

    async def __call__(self):
        return await invoke_parent_operation_on_owner_loop(
            self._binding,
            self._target,
        )


class _OwnerLoopSkillSecretProviderProxy:
    """Marshal private Skill secret refresh back to the owner Worker loop."""

    def __init__(self, target: object, binding: ParentExecutionBinding) -> None:
        self._target = target
        self._binding = binding

    async def __call__(self, *args, **kwargs):
        return await invoke_parent_operation_on_owner_loop(
            self._binding,
            self._target,
            *args,
            **kwargs,
        )


@dataclass(frozen=True, slots=True, repr=False)
class DelegatedRuntimeContextProjection:
    """Opaque frozen projection consumed by one delegated graph runner."""

    _carrier: RuntimeContextCarrier
    channel_identity_mode: HostExecutionChannelIdentityMode
    agent_prompt_bundle: object | None
    runtime_skills: tuple[object, ...]

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<opaque>)"

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("delegated runtime-context projection is not serializable")

    @property
    def app_config(self) -> object | None:
        return self._carrier.app_config

    @property
    def thread_id(self) -> str | None:
        return self._carrier.thread_id

    @property
    def run_id(self) -> str | None:
        return self._carrier.run_id

    @property
    def user_id(self) -> str | None:
        return self._carrier.user_id

    @property
    def deerflow_trace_id(self) -> str | None:
        return self._carrier.trace_id

    @property
    def token_usage_tracking_enabled(self) -> bool:
        value = self._carrier.token_usage_tracking_enabled
        return True if value is None else value

    def build(self) -> dict[str, Any]:
        """Build a fresh child context with exact channel-identity presence."""

        context = self._carrier.build()
        if self.channel_identity_mode == "unset":
            context[RuntimeContextKeys.CHANNEL_USER_ID] = None
        elif self.channel_identity_mode == "absent":
            context.pop(RuntimeContextKeys.CHANNEL_USER_ID, None)
        return context


def project_delegated_runtime_context(
    binding: ParentExecutionBinding,
    *,
    subagent_name: str,
    fallback_user_id: str,
    fallback_trace_id: str | None,
    agent_prompt_bundle: object | None,
    runtime_skills: tuple[object, ...],
    delegated_output_root: str | None = None,
) -> DelegatedRuntimeContextProjection:
    """Project one graph-authoritative parent binding into child execution."""

    if type(binding) is not ParentExecutionBinding:
        raise TypeError("binding must be ParentExecutionBinding")
    if not isinstance(subagent_name, str) or not subagent_name:
        raise ValueError("subagent_name is required")

    profile = binding.profile
    private_run = type(profile) is PrivateRunParentExecutionProfile
    parent_context = binding.context
    metadata = binding.config.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    configurable = binding.config.get("configurable")
    configurable = configurable if isinstance(configurable, Mapping) else {}

    thread_id = parent_context.get(RuntimeContextKeys.THREAD_ID)
    if thread_id is None:
        thread_id = configurable.get(RuntimeContextKeys.THREAD_ID)

    guardrail_attribution = (
        copy_guardrail_attribution(
            parent_context.get(RuntimeContextKeys.GUARDRAIL_ATTRIBUTION),
        )
        if private_run
        else None
    )
    user_id: object = fallback_user_id
    user_role = parent_context.get(RuntimeContextKeys.USER_ROLE)
    oauth_provider = parent_context.get(RuntimeContextKeys.OAUTH_PROVIDER)
    oauth_id = parent_context.get(RuntimeContextKeys.OAUTH_ID)
    run_id = parent_context.get(RuntimeContextKeys.RUN_ID)
    if guardrail_attribution is not None:
        user_id = guardrail_attribution.get("user_id")
        user_role = guardrail_attribution.get("user_role")
        oauth_provider = guardrail_attribution.get("oauth_provider")
        oauth_id = guardrail_attribution.get("oauth_id")
        run_id = guardrail_attribution.get("run_id")

    if RuntimeContextKeys.CHANNEL_USER_ID not in parent_context:
        channel_identity_mode: HostExecutionChannelIdentityMode = "absent"
        channel_user_id = None
    else:
        raw_channel_user_id = parent_context.get(RuntimeContextKeys.CHANNEL_USER_ID)
        if isinstance(raw_channel_user_id, str) and raw_channel_user_id:
            channel_identity_mode = "set"
            channel_user_id = raw_channel_user_id
        else:
            channel_identity_mode = "unset"
            channel_user_id = None

    trace_id = normalize_trace_id(parent_context.get(RuntimeContextKeys.TRACE_ID)) or normalize_trace_id(metadata.get(RuntimeContextKeys.TRACE_ID)) or normalize_trace_id(fallback_trace_id)
    run_read_only_mounts = parent_context.get(RuntimeContextKeys.RUN_READ_ONLY_MOUNTS)
    if not private_run or not isinstance(run_read_only_mounts, tuple):
        run_read_only_mounts = None

    file_authority = None
    authorization_boundary = None
    authorization_checker = None
    skill_secret_provider = None
    if private_run:
        raw_file_authority = parent_context.get(RuntimeContextKeys.FILE_AUTHORITY)
        if raw_file_authority is not None:
            file_authority = _OwnerLoopFileAuthorityProxy(
                cast(RunFileAuthority, raw_file_authority),
                binding,
                delegated_output_root=delegated_output_root,
            )
        raw_authorization_boundary = parent_context.get(
            RuntimeContextKeys.AUTHORIZATION_BOUNDARY,
        )
        if raw_authorization_boundary is not None:
            authorization_boundary = _OwnerLoopAuthorityProxy(
                raw_authorization_boundary,
                binding,
            )
        raw_authorization_checker = parent_context.get(
            RuntimeContextKeys.AUTHORIZATION_CHECKER,
        )
        if callable(raw_authorization_checker):
            authorization_checker = _OwnerLoopCheckerProxy(
                raw_authorization_checker,
                binding,
            )
        raw_skill_secret_provider = parent_context.get(
            RuntimeContextKeys.SKILL_SECRET_PROVIDER,
        )
        if callable(raw_skill_secret_provider):
            skill_secret_provider = _OwnerLoopSkillSecretProviderProxy(
                raw_skill_secret_provider,
                binding,
            )

    skill_scoped_secrets = None
    if private_run and skill_secret_provider is None:
        skill_scoped_secrets = _trusted_skill_scoped_secrets(parent_context)

    raw_recovered_llm_failure_recorder = parent_context.get(
        RuntimeContextKeys.RECOVERED_LLM_FAILURE_RECORDER,
    )
    recovered_llm_failure_recorder = (
        raw_recovered_llm_failure_recorder
        if isinstance(
            raw_recovered_llm_failure_recorder,
            RunRecoveredLLMFailureRecorder,
        )
        else None
    )
    raw_token_usage_tracking_enabled = parent_context.get(
        RuntimeContextKeys.TOKEN_USAGE_TRACKING_ENABLED,
    )
    if type(raw_token_usage_tracking_enabled) is bool:
        token_usage_tracking_enabled = raw_token_usage_tracking_enabled
    else:
        profile_app_config = _profile_app_config(profile)
        token_usage_tracking_enabled = bool(
            getattr(
                getattr(profile_app_config, "token_usage", None),
                "enabled",
                True,
            )
        )

    host_execution_approval_port = None
    host_execution_agent_path = None
    raw_approval_port = parent_context.get(
        RuntimeContextKeys.HOST_EXECUTION_APPROVAL_PORT,
    )
    if isinstance(raw_approval_port, HostExecutionApprovalPort):
        host_execution_approval_port = _OwnerLoopHostExecutionApprovalProxy(
            raw_approval_port,
            binding,
        )
        raw_parent_path = parent_context.get(
            RuntimeContextKeys.HOST_EXECUTION_AGENT_PATH,
        )
        parent_path = raw_parent_path if isinstance(raw_parent_path, tuple) and raw_parent_path and all(isinstance(part, str) and part for part in raw_parent_path) else ("lead",)
        host_execution_agent_path = (
            *parent_path,
            f"subagent:{subagent_name}",
        )

    carrier = RuntimeContextCarrier(
        thread_id=thread_id if isinstance(thread_id, str) else None,
        run_id=run_id if isinstance(run_id, str) else None,
        app_config=_profile_app_config(profile),
        user_id=user_id if isinstance(user_id, str) else None,
        user_role=user_role if isinstance(user_role, str) else None,
        oauth_provider=(oauth_provider if isinstance(oauth_provider, str) else None),
        oauth_id=oauth_id if isinstance(oauth_id, str) else None,
        channel_user_id=channel_user_id,
        is_subagent=True,
        private_scope=(parent_context.get(RuntimeContextKeys.PRIVATE_SCOPE) if private_run else None),
        authorization_checker=authorization_checker,
        authorization_boundary=authorization_boundary,
        file_authority=file_authority,
        guardrail_attribution=guardrail_attribution,
        run_read_only_mounts=run_read_only_mounts,
        skill_scoped_secrets=skill_scoped_secrets,
        skill_secret_provider=skill_secret_provider,
        trace_id=trace_id,
        token_usage_tracking_enabled=token_usage_tracking_enabled,
        recovered_llm_failure_recorder=recovered_llm_failure_recorder,
        host_execution_approval_port=host_execution_approval_port,
        host_execution_agent_path=host_execution_agent_path,
    )
    return DelegatedRuntimeContextProjection(
        _carrier=carrier,
        channel_identity_mode=channel_identity_mode,
        agent_prompt_bundle=agent_prompt_bundle,
        runtime_skills=tuple(runtime_skills),
    )


__all__ = [
    "DelegatedRuntimeContextProjection",
    "project_delegated_runtime_context",
]
