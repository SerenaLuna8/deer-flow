"""Duplicate-identity errors for email and username collisions."""

from __future__ import annotations

from typing import Literal


class DuplicateUserIdentity(ValueError):
    """Raised when a create/update collides on email or username."""

    def __init__(self, field: Literal["email", "username"], value: str) -> None:
        self.field = field
        self.value = value
        super().__init__(f"{field} already registered: {value}")


__all__ = ["DuplicateUserIdentity"]
