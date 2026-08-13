"""Pure advisory assessment shared by Memory reads and Run admission."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from deerflow.memory_contract import (
    MemoryDocumentInvalid,
    MemoryDocumentOverBudget,
    validate_memory_document,
)

MemoryInjectionStatus = Literal[
    "eligible",
    "skipped_over_budget",
    "inactive",
]
MemoryInjectionReason = Literal[
    "within_budget",
    "over_budget",
    "platform_disabled",
    "account_disabled",
    "no_document",
]

_VALID_ASSESSMENTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("eligible", "within_budget"),
        ("skipped_over_budget", "over_budget"),
        ("inactive", "platform_disabled"),
        ("inactive", "account_disabled"),
        ("inactive", "no_document"),
    }
)


@dataclass(frozen=True, slots=True)
class MemoryInjectionCandidate:
    """A document or frozen snapshot considered for one injection decision."""

    content: str
    content_digest: str
    sections: object


@dataclass(frozen=True, slots=True)
class MemoryInjectionAssessment:
    """Current evidence, not a reservation or promise about a future Run."""

    status: MemoryInjectionStatus
    reason: MemoryInjectionReason

    def __post_init__(self) -> None:
        if (self.status, self.reason) not in _VALID_ASSESSMENTS:
            raise ValueError("Memory injection assessment fields do not match")

    @property
    def legacy_status(self) -> Literal["ok", "skipped_over_budget"]:
        """Preserve the original two-state wire contract for older clients."""

        if self.status == "skipped_over_budget":
            return "skipped_over_budget"
        return "ok"


def assess_memory_injection(
    *,
    platform_enabled: bool,
    account_enabled: bool,
    max_injection_tokens: int,
    candidate: MemoryInjectionCandidate | None,
) -> MemoryInjectionAssessment:
    """Assess current inputs in the same order used by new-Run admission.

    Disabled switches short-circuit before document inspection, matching Run
    admission. Once injection is enabled, integrity damage raises instead of
    being normalized into an advisory ``invalid`` status.
    """

    if type(max_injection_tokens) is not int or max_injection_tokens < 1:
        raise ValueError("Memory injection token budget must be positive")
    disabled_reason = memory_injection_disabled_reason(
        platform_enabled=platform_enabled,
        account_enabled=account_enabled,
    )
    if disabled_reason == "platform_disabled":
        return MemoryInjectionAssessment(
            status="inactive",
            reason="platform_disabled",
        )
    if disabled_reason == "account_disabled":
        return MemoryInjectionAssessment(
            status="inactive",
            reason="account_disabled",
        )
    if candidate is None:
        return MemoryInjectionAssessment(
            status="inactive",
            reason="no_document",
        )
    if type(candidate) is not MemoryInjectionCandidate:
        raise TypeError("MemoryInjectionCandidate is required")
    if not isinstance(candidate.content, str) or not isinstance(
        candidate.content_digest,
        str,
    ):
        raise MemoryDocumentInvalid("Memory document digest is invalid")
    digest = hashlib.sha256(candidate.content.encode("utf-8")).hexdigest()
    if digest != candidate.content_digest:
        raise MemoryDocumentInvalid("Memory document digest drift")
    try:
        validate_memory_document(
            candidate.content,
            max_injection_tokens,
            sections=candidate.sections,
        )
    except MemoryDocumentOverBudget:
        return MemoryInjectionAssessment(
            status="skipped_over_budget",
            reason="over_budget",
        )
    return MemoryInjectionAssessment(
        status="eligible",
        reason="within_budget",
    )


def memory_injection_disabled_reason(
    *,
    platform_enabled: bool,
    account_enabled: bool,
) -> Literal["platform_disabled", "account_disabled"] | None:
    """Return the first current switch that prevents a new-Run injection."""

    if type(platform_enabled) is not bool or type(account_enabled) is not bool:
        raise ValueError("Memory injection switches must be boolean")
    if not platform_enabled:
        return "platform_disabled"
    if not account_enabled:
        return "account_disabled"
    return None


__all__ = [
    "MemoryInjectionAssessment",
    "MemoryInjectionCandidate",
    "MemoryInjectionReason",
    "MemoryInjectionStatus",
    "assess_memory_injection",
    "memory_injection_disabled_reason",
]
