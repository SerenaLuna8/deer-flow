"""Typed durable owner-private lifecycle facts shared by admission storage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AccountPrivateGeneration:
    """Exact active account-private generation issued under the User lock."""

    owner_user_id: str
    generation: int

    def __post_init__(self) -> None:
        try:
            owner_user_id = str(uuid.UUID(str(self.owner_user_id)))
        except (TypeError, ValueError):
            raise ValueError("account-private generation owner is invalid") from None
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("account-private generation must be positive")
        object.__setattr__(self, "owner_user_id", owner_user_id)


__all__ = ["AccountPrivateGeneration"]
