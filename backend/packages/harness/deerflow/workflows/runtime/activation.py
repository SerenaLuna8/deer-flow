"""Stable iteration-aware activation identity."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


def _canonical_uuid(value: str, field: str) -> None:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical lowercase UUID")


@dataclass(frozen=True, slots=True)
class WorkflowActivationIdentity:
    run_id: str
    node_id: str
    iteration_path: tuple[int, ...]
    logical_activation: int = 0
    attempt: int = 1

    def __post_init__(self) -> None:
        _canonical_uuid(self.run_id, "run_id")
        _canonical_uuid(self.node_id, "node_id")
        if not self.iteration_path or any(type(component) is not int or component <= 0 for component in self.iteration_path):
            raise ValueError("iteration_path must contain positive integers")
        if type(self.logical_activation) is not int or self.logical_activation < 0:
            raise ValueError("logical_activation must be non-negative")
        if type(self.attempt) is not int or self.attempt <= 0:
            raise ValueError("attempt must be positive")

    @property
    def key(self) -> str:
        path = ".".join(str(component) for component in self.iteration_path)
        # Attempt is an execution bucket beneath the stable logical
        # activation and therefore cannot participate in this key.
        return f"{self.run_id}:{self.node_id}:{path}:{self.logical_activation}"

    @property
    def attempt_bucket_key(self) -> str:
        return f"{self.key}:attempt:{self.attempt}"
