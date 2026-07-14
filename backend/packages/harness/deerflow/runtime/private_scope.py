from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PrivateResourceScope:
    """Authority-free private resource coordinates for harness adapters."""

    project_id: str
    owner_user_id: str
    membership_version: int
