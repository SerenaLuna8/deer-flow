"""Slash-command registry for the DeerFlow TUI (pure).

Provides one searchable list of TUI-owned built-in commands (``/help``,
``/model``, ``/threads`` …).

The picker filters this list; :func:`resolve` classifies a submitted line as a
built-in command, an unknown command, or a plain message. No Textual dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Command:
    name: str  # without leading slash
    description: str


@dataclass(frozen=True)
class Resolution:
    kind: Literal["builtin", "unknown", "message"]
    name: str = ""
    args: str = ""
    text: str = ""


# Built-in commands, ordered for display in /help and the picker.
BUILTIN_COMMANDS: tuple[Command, ...] = (
    Command("help", "Show commands and keybindings"),
    Command("new", "Start a fresh thread"),
    Command("threads", "Open the thread switcher"),
    Command("switch", "Open the thread switcher"),
    Command("resume", "Resume a thread by id or title"),
    Command("goal", "Set, show or clear the active goal"),
    Command("model", "Open the model picker"),
    Command("tools", "Show tools available to the admitted run"),
    Command("uploads", "Show uploaded files for this thread"),
    Command("artifacts", "Show generated artifacts"),
    Command("details", "Toggle verbose activity rendering"),
    Command("usage", "Show token usage and context"),
    Command("config", "Show resolved config paths and overrides"),
    Command("quit", "Exit the TUI"),
)

_BUILTIN_NAMES = frozenset(c.name for c in BUILTIN_COMMANDS)


def build_registry() -> list[Command]:
    """Return the TUI-owned built-in commands."""
    return list(BUILTIN_COMMANDS)


def filter_commands(commands: list[Command], query: str) -> list[Command]:
    """Filter + rank commands for the picker.

    Ranking: name-prefix matches first, then name-substring, then
    description-substring. Original order is preserved within a rank tier.
    """
    q = query.strip().lower()
    if not q:
        return commands

    prefix: list[Command] = []
    substring: list[Command] = []
    description: list[Command] = []
    for command in commands:
        name = command.name.lower()
        if name.startswith(q):
            prefix.append(command)
        elif q in name:
            substring.append(command)
        elif q in command.description.lower():
            description.append(command)
    return prefix + substring + description


def resolve(text: str) -> Resolution:
    """Classify a submitted input line."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return Resolution(kind="message", text=text)

    body = stripped[1:]
    name, _, args = body.partition(" ")
    name = name.strip()
    args = args.strip()

    if not name:
        return Resolution(kind="unknown", name="")

    if name in _BUILTIN_NAMES:
        return Resolution(kind="builtin", name=name, args=args)

    return Resolution(kind="unknown", name=name, args=args)
