"""Security helpers for sandbox capability gating."""

from enum import StrEnum
from functools import lru_cache

_LOCAL_SANDBOX_PROVIDER_MARKERS = frozenset(
    {
        "deerflow.sandbox.local:LocalSandboxProvider",
        "deerflow.sandbox.local.local_sandbox_provider:LocalSandboxProvider",
    },
)
_ISOLATED_SANDBOX_PROVIDER_MARKERS = frozenset(
    {
        "deerflow.community.aio_sandbox:AioSandboxProvider",
        "deerflow.community.aio_sandbox.aio_sandbox_provider:AioSandboxProvider",
        "deerflow.community.boxlite:BoxliteProvider",
        "deerflow.community.boxlite.provider:BoxliteProvider",
        "deerflow.community.e2b_sandbox:E2BSandboxProvider",
        "deerflow.community.e2b_sandbox.e2b_sandbox_provider:E2BSandboxProvider",
    },
)

LOCAL_HOST_BASH_DISABLED_MESSAGE = (
    "Host bash execution is disabled for LocalSandboxProvider because it is not a secure "
    "sandbox boundary. Switch to AioSandboxProvider for isolated bash access, or set "
    "sandbox.host_execution_approval.mode: approval_required for one-time host "
    "approval, or set sandbox.allow_host_bash: true only in a fully trusted local "
    "environment."
)

LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE = (
    "Bash subagent is disabled for LocalSandboxProvider because host bash execution is not "
    "a secure sandbox boundary. Switch to AioSandboxProvider for isolated bash access, or "
    "set sandbox.host_execution_approval.mode: approval_required for one-time host "
    "approval, or set sandbox.allow_host_bash: true only in a fully trusted local "
    "environment."
)


class HostBashExecutionMode(StrEnum):
    """Resolved process-launch behavior for the configured provider."""

    ISOLATED_DIRECT = "isolated_direct"
    LOCAL_DISABLED = "local_disabled"
    LOCAL_APPROVAL_REQUIRED = "local_approval_required"
    LOCAL_LEGACY_ALLOW = "local_legacy_allow"


@lru_cache(maxsize=64)
def _resolved_provider_class(provider_use: str) -> type | None:
    """Resolve one configured provider class without granting capabilities."""

    if not isinstance(provider_use, str) or not provider_use:
        return None
    try:
        from deerflow.reflection import resolve_class
        from deerflow.sandbox.sandbox_provider import SandboxProvider

        return resolve_class(provider_use, SandboxProvider)
    except Exception:
        return None


@lru_cache(maxsize=64)
def _provider_class_lineage(provider_use: str) -> frozenset[str]:
    """Resolve one configured provider into stable class-lineage markers.

    Provider class paths are extensible, so a Local provider may be re-exported
    or subclassed outside ``deerflow.sandbox.local``.  String matching alone
    would classify that host-backed provider as isolated and silently bypass
    Local approval.  Resolution failures deliberately return no capabilities;
    callers therefore fail closed instead of assuming isolation.
    """

    provider_class = _resolved_provider_class(provider_use)
    if provider_class is None:
        return frozenset()
    return frozenset(f"{base.__module__}:{base.__name__}" for base in provider_class.__mro__)


def _provider_class_identity(provider_use: str) -> str | None:
    provider_class = _resolved_provider_class(provider_use)
    if provider_class is None:
        return None
    return f"{provider_class.__module__}:{provider_class.__name__}"


def _provider_use(config: object) -> str:
    sandbox_cfg = getattr(config, "sandbox", None)
    sandbox_use = getattr(sandbox_cfg, "use", "")
    return sandbox_use if isinstance(sandbox_use, str) else ""


def uses_local_sandbox_provider_use(provider_use: str) -> bool:
    """Classify one provider class path without requiring an AppConfig."""

    if provider_use in _LOCAL_SANDBOX_PROVIDER_MARKERS:
        return True
    if provider_use in _ISOLATED_SANDBOX_PROVIDER_MARKERS:
        return False
    return bool(_provider_class_lineage(provider_use) & _LOCAL_SANDBOX_PROVIDER_MARKERS)


def uses_local_sandbox_provider(config=None) -> bool:
    """Return True when the active sandbox provider is the host-local provider."""
    if config is None:
        from deerflow.config import get_app_config

        config = get_app_config()

    return uses_local_sandbox_provider_use(_provider_use(config))


def uses_isolated_sandbox_provider(config=None) -> bool:
    """Return whether the configured provider is one explicitly trusted class.

    Re-exports resolve to the same built-in class identity.  Subclasses remain
    untrusted because they can override dispatch and remove the isolation
    boundary while inheriting the built-in class lineage.
    """

    if config is None:
        from deerflow.config import get_app_config

        config = get_app_config()
    sandbox_use = _provider_use(config)
    if sandbox_use in _ISOLATED_SANDBOX_PROVIDER_MARKERS:
        return True
    return _provider_class_identity(sandbox_use) in _ISOLATED_SANDBOX_PROVIDER_MARKERS


def resolve_local_host_bash_execution_mode(config=None) -> HostBashExecutionMode:
    """Resolve Local host execution without trusting the configured class path."""

    if config is None:
        from deerflow.config import get_app_config

        config = get_app_config()
    sandbox_cfg = getattr(config, "sandbox", None)
    if sandbox_cfg is None:
        return HostBashExecutionMode.LOCAL_DISABLED
    if bool(getattr(sandbox_cfg, "allow_host_bash", False)):
        return HostBashExecutionMode.LOCAL_LEGACY_ALLOW
    approval = getattr(sandbox_cfg, "host_execution_approval", None)
    if getattr(approval, "mode", "disabled") == "approval_required":
        return HostBashExecutionMode.LOCAL_APPROVAL_REQUIRED
    return HostBashExecutionMode.LOCAL_DISABLED


def is_host_bash_allowed(config=None) -> bool:
    """Return whether bash may execute immediately without user approval."""
    return resolve_host_bash_execution_mode(config) in {
        HostBashExecutionMode.ISOLATED_DIRECT,
        HostBashExecutionMode.LOCAL_LEGACY_ALLOW,
    }


def resolve_host_bash_execution_mode(config=None) -> HostBashExecutionMode:
    """Resolve isolated direct, Local disabled, approval, and legacy modes."""
    if config is None:
        from deerflow.config import get_app_config

        config = get_app_config()

    sandbox_cfg = getattr(config, "sandbox", None)
    if sandbox_cfg is None:
        return HostBashExecutionMode.LOCAL_DISABLED
    if uses_local_sandbox_provider(config):
        return resolve_local_host_bash_execution_mode(config)
    if uses_isolated_sandbox_provider(config):
        return HostBashExecutionMode.ISOLATED_DIRECT
    # Unknown providers do not inherit isolated process-launch authority merely
    # because their class path is not the built-in Local path.
    return HostBashExecutionMode.LOCAL_DISABLED


def is_host_bash_available(config=None) -> bool:
    """Return whether a real bash path can be exposed to an Agent."""
    return resolve_host_bash_execution_mode(config) is not HostBashExecutionMode.LOCAL_DISABLED


def requires_host_bash_approval(config=None) -> bool:
    """Return whether every Local bash launch must cross the approval port."""
    return resolve_host_bash_execution_mode(config) is HostBashExecutionMode.LOCAL_APPROVAL_REQUIRED


__all__ = [
    "HostBashExecutionMode",
    "LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE",
    "LOCAL_HOST_BASH_DISABLED_MESSAGE",
    "is_host_bash_allowed",
    "is_host_bash_available",
    "requires_host_bash_approval",
    "resolve_host_bash_execution_mode",
    "resolve_local_host_bash_execution_mode",
    "uses_isolated_sandbox_provider",
    "uses_local_sandbox_provider",
    "uses_local_sandbox_provider_use",
]
