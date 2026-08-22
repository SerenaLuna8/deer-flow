"""Construct the minimal host environment exposed to Sandbox processes.

Skill scripts must not inherit arbitrary Worker or host variables.  A denylist
cannot enforce that boundary because an operator or library can introduce a
secret under an otherwise innocent name.  Only the small process-bootstrapping
allowlist below crosses the boundary; Skill-declared values are layered on top
after their exact recipient has been authorized upstream.
"""

from __future__ import annotations

import os

# Values needed to locate ordinary executables, a writable temporary directory,
# the account home, and locale/terminal behavior.  Runtime loaders, language
# package paths, proxy settings, service endpoints, and application variables
# are intentionally absent.
_INHERITED_ENV_NAMES: frozenset[str] = frozenset(
    {
        "COLORTERM",
        "COMSPEC",
        "FORCE_COLOR",
        "HOME",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "SHELL",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "TZ",
        "USER",
        "WINDIR",
    }
)


def is_blocked_env_name(name: str) -> bool:
    """Return whether a host variable is outside the Sandbox allowlist."""
    return name.upper() not in _INHERITED_ENV_NAMES


def build_sandbox_env(injected: dict[str, str] | None = None) -> dict[str, str]:
    """Build the environment dict for a sandbox subprocess.

    Copies only explicitly safe process-bootstrapping values, then layers the
    request-scoped values authorized for the exact Skill definition.  Injection
    deliberately wins over a same-name host value.
    """
    env = {key: value for key, value in os.environ.items() if key.upper() in _INHERITED_ENV_NAMES}
    if injected:
        env.update(injected)
    return env
