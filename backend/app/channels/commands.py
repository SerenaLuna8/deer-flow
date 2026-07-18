"""Shared command definitions used by all channel implementations.

Keeping the authoritative command set in one place ensures that channel
parsers (e.g. Feishu) and the ChannelManager dispatcher stay in sync
automatically — adding or removing a command here is the single edit
required.
"""

from __future__ import annotations

KNOWN_CHANNEL_COMMANDS: frozenset[str] = frozenset({"/help", "/models"})

# These former commands are intentionally tombstoned instead of becoming
# ordinary prompts. Provider adapters no longer advertise or classify them as
# active commands; the manager recognizes them defensively and returns the
# stable unsupported-command response without admitting a project Run.
REMOVED_CHANNEL_COMMANDS: frozenset[str] = frozenset({"/bootstrap", "/goal", "/memory", "/new", "/status"})


def extract_connect_code(text: str) -> str | None:
    """Extract the one-time channel binding code from a connect command."""
    parts = text.strip().split()
    if len(parts) < 2:
        return None
    command = parts[0].lower()
    if command in {"/connect", "connect"}:
        return parts[1]
    return None


def is_known_channel_command(text: str) -> bool:
    """Return whether text starts with a registered channel control command."""
    if not text.startswith("/"):
        return False
    return text.split(maxsplit=1)[0].lower() in KNOWN_CHANNEL_COMMANDS


def is_removed_channel_command(text: str) -> bool:
    """Return whether text starts with a tombstoned channel command."""
    if not text.startswith("/"):
        return False
    return text.split(maxsplit=1)[0].lower() in REMOVED_CHANNEL_COMMANDS
