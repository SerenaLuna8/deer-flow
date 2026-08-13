from __future__ import annotations

import hashlib

import pytest

from app.private_work.memory_injection import (
    MemoryInjectionCandidate,
    assess_memory_injection,
)
from deerflow.memory_contract import (
    DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
    EMPTY_MEMORY_DOCUMENT,
    MemoryDocumentInvalid,
)


def _candidate(
    content: str = EMPTY_MEMORY_DOCUMENT,
    *,
    digest: str | None = None,
    sections: object = DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
) -> MemoryInjectionCandidate:
    return MemoryInjectionCandidate(
        content=content,
        content_digest=digest or hashlib.sha256(content.encode("utf-8")).hexdigest(),
        sections=sections,
    )


@pytest.mark.parametrize(
    ("platform_enabled", "account_enabled", "reason"),
    [
        (False, True, "platform_disabled"),
        (True, False, "account_disabled"),
    ],
)
def test_assessment_reports_current_switches_as_inactive_advisory(
    platform_enabled: bool,
    account_enabled: bool,
    reason: str,
) -> None:
    assessment = assess_memory_injection(
        platform_enabled=platform_enabled,
        account_enabled=account_enabled,
        max_injection_tokens=2_000,
        candidate=_candidate(),
    )

    assert assessment.status == "inactive"
    assert assessment.reason == reason
    assert assessment.legacy_status == "ok"


def test_assessment_distinguishes_no_document_from_an_eligible_document() -> None:
    absent = assess_memory_injection(
        platform_enabled=True,
        account_enabled=True,
        max_injection_tokens=2_000,
        candidate=None,
    )
    eligible = assess_memory_injection(
        platform_enabled=True,
        account_enabled=True,
        max_injection_tokens=2_000,
        candidate=_candidate(),
    )

    assert (absent.status, absent.reason) == ("inactive", "no_document")
    assert (eligible.status, eligible.reason) == ("eligible", "within_budget")


def test_assessment_classifies_only_valid_over_budget_documents_as_skipped() -> None:
    content = EMPTY_MEMORY_DOCUMENT + "\n" + ("超" * 200)

    assessment = assess_memory_injection(
        platform_enabled=True,
        account_enabled=True,
        max_injection_tokens=100,
        candidate=_candidate(content),
    )

    assert assessment.status == "skipped_over_budget"
    assert assessment.reason == "over_budget"
    assert assessment.legacy_status == "skipped_over_budget"


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(digest="b" * 64),
        _candidate(sections=("不匹配章节一", "不匹配章节二")),
    ],
)
def test_assessment_never_downgrades_integrity_damage_to_an_advisory_status(
    candidate: MemoryInjectionCandidate,
) -> None:
    with pytest.raises(MemoryDocumentInvalid):
        assess_memory_injection(
            platform_enabled=True,
            account_enabled=True,
            max_injection_tokens=2_000,
            candidate=candidate,
        )


def test_disabled_assessment_matches_admission_short_circuit() -> None:
    assessment = assess_memory_injection(
        platform_enabled=False,
        account_enabled=False,
        max_injection_tokens=2_000,
        candidate=_candidate(digest="b" * 64),
    )

    assert (assessment.status, assessment.reason) == (
        "inactive",
        "platform_disabled",
    )
