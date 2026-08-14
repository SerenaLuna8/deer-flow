"""Canonical username identity helpers.

Usernames are a second login identifier beside email. They are stored
lowercase, unique among human accounts, and reject Chinese or other
non-ASCII / special characters.
"""

from __future__ import annotations

import re

from app.gateway.auth.email import normalize_email

USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
_EMAIL_LOCAL_KEEP = re.compile(r"[^a-z0-9_]")


class UsernameInvalid(ValueError):
    """Raised when a username does not match the public login contract."""


def normalize_username(username: str) -> str:
    """Return the trimmed lowercase form used for storage and comparison."""

    if not isinstance(username, str):
        raise TypeError("username must be a string")
    return username.strip().lower()


def parse_username(username: str) -> str:
    """Validate and canonicalize a public username."""

    canonical = normalize_username(username)
    if USERNAME_PATTERN.fullmatch(canonical) is None:
        raise UsernameInvalid(
            "Username must be 3-32 characters, start with a letter, and use only letters, digits, or underscore",
        )
    return canonical


def username_from_email_local_part(email: str) -> str:
    """Derive a valid username from an email local part for OIDC provisioning."""

    local = normalize_email(email).split("@", 1)[0]
    cleaned = _EMAIL_LOCAL_KEEP.sub("", local)
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"user_{cleaned}" if cleaned else "user"
    if len(cleaned) < 3:
        cleaned = f"{cleaned}_user"
    return cleaned[:32]


__all__ = [
    "USERNAME_PATTERN",
    "UsernameInvalid",
    "normalize_username",
    "parse_username",
    "username_from_email_local_part",
]
