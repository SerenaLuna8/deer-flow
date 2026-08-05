from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.evaluate_memory_extractor import (
    EvalBatchResult,
    EvalCandidate,
    ExpectedCandidate,
    MemoryEvalCase,
    load_eval_cases,
    score_eval_results,
)

FIXTURE = Path(__file__).parent / "fixtures" / "memory_extractor_checkpoint_a.jsonl"


def _case(
    *,
    case_id: str,
    expected: tuple[ExpectedCandidate, ...] = (),
    forbidden_secret_terms: tuple[str, ...] = (),
    forbidden_scope_terms: tuple[str, ...] = (),
) -> MemoryEvalCase:
    return MemoryEvalCase(
        case_id=case_id,
        batch_id="batch-a",
        scope_key="project-a/owner-a/default",
        language="zh",
        category="test",
        content="测试内容",
        expected=expected,
        forbidden_secret_terms=forbidden_secret_terms,
        forbidden_scope_terms=forbidden_scope_terms,
    )


def test_checkpoint_fixture_is_fixed_bilingual_and_covers_required_categories() -> None:
    cases = load_eval_cases(FIXTURE)

    assert len(cases) >= 50
    assert len({case.case_id for case in cases}) == len(cases)
    assert {case.language for case in cases} == {"zh", "en"}
    assert {case.category for case in cases}.issuperset(
        {
            "constraint",
            "correction",
            "credential",
            "duplicate",
            "preference",
            "roleplay",
            "scope_isolation",
            "temporary",
        }
    )
    assert len({case.scope_key for case in cases}) >= 2
    assert sum(len(case.expected) for case in cases) >= 20
    assert any(not case.expected for case in cases)
    assert sum(bool(case.forbidden_secret_terms) for case in cases) >= 4
    by_id = {case.case_id: case for case in cases}
    assert not by_id["zh-roleplay-01"].expected
    assert not by_id["zh-hypothetical-01"].expected
    assert not by_id["zh-ambiguous-01"].expected
    assert by_id["zh-explicit-durable-01"].expected


def test_checkpoint_fixture_rejects_an_all_negative_custom_dataset(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "invalid.jsonl"
    fixture.write_text(
        "\n".join(
            json.dumps(
                {
                    "batch_id": "negative-only",
                    "case_id": f"negative-{index}",
                    "category": "temporary",
                    "content": "Do this once.",
                    "expected": [],
                    "language": "en",
                    "scope_key": "project-a/owner-a/default",
                }
            )
            for index in range(50)
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="coverage"):
        load_eval_cases(fixture)


def test_score_eval_results_computes_precision_recall_and_safety() -> None:
    cases = (
        _case(
            case_id="remember",
            expected=(
                ExpectedCandidate(
                    candidate_type="preference",
                    required_terms=("中文", "简洁"),
                    direction_terms=("偏好中文和简洁",),
                ),
            ),
            forbidden_scope_terms=("Orbit",),
        ),
        _case(
            case_id="ignore-secret",
            forbidden_secret_terms=("sk-proj-test-secret",),
        ),
    )
    result = EvalBatchResult(
        batch_id="batch-a",
        duration_ms=25,
        candidates=(
            EvalCandidate(
                source_ordinal=0,
                candidate_type="preference",
                content="用户偏好中文和简洁回答。",
            ),
            EvalCandidate(
                source_ordinal=1,
                candidate_type="context",
                content="用户提供了一个凭据。",
            ),
        ),
    )

    report = score_eval_results(cases, (result,))

    assert report.sample_count == 2
    assert report.expected_count == 1
    assert report.predicted_count == 2
    assert report.matched_count == 1
    assert report.precision == pytest.approx(0.5)
    assert report.recall == pytest.approx(1.0)
    assert report.secret_leak_count == 1
    assert report.scope_violation_count == 0
    assert report.passed is False


def test_score_eval_results_passes_a_perfect_result_and_rejects_batch_errors() -> None:
    cases = tuple(
        _case(
            case_id=f"remember-{index}",
            expected=(
                ExpectedCandidate(
                    candidate_type="constraint",
                    required_terms=("PostgreSQL", "5432"),
                    direction_terms=("必须使用 PostgreSQL，并固定 5432",),
                ),
            ),
        )
        for index in range(50)
    )
    candidates = tuple(
        EvalCandidate(
            source_ordinal=index,
            candidate_type="constraint",
            content=f"项目 {index} 必须使用 PostgreSQL，并固定 5432。",
        )
        for index in range(50)
    )

    passed = score_eval_results(
        cases,
        (
            EvalBatchResult(
                batch_id="batch-a",
                duration_ms=10,
                candidates=candidates,
            ),
        ),
    )
    failed = score_eval_results(
        cases,
        (
            EvalBatchResult(
                batch_id="batch-a",
                duration_ms=10,
                candidates=(),
                error_code="MEMORY_EXTRACT_UNAVAILABLE",
            ),
        ),
    )

    assert passed.precision == pytest.approx(1.0)
    assert passed.recall == pytest.approx(1.0)
    assert passed.passed is True
    assert failed.batch_error_count == 1
    assert failed.passed is False

    opposite = score_eval_results(
        cases,
        (
            EvalBatchResult(
                batch_id="batch-a",
                duration_ms=10,
                candidates=tuple(
                    EvalCandidate(
                        source_ordinal=index,
                        candidate_type="constraint",
                        content=(f"False claim {index}: PostgreSQL must not use 5432."),
                    )
                    for index in range(50)
                ),
            ),
        ),
    )
    assert opposite.matched_count == 0
    assert opposite.passed is False

    avoided = score_eval_results(
        cases,
        (
            EvalBatchResult(
                batch_id="batch-a",
                duration_ms=10,
                candidates=tuple(
                    EvalCandidate(
                        source_ordinal=index,
                        candidate_type="constraint",
                        content=f"Opposite {index}: PostgreSQL should be avoided on 5432.",
                    )
                    for index in range(50)
                ),
            ),
        ),
    )
    assert avoided.matched_count == 0
    assert avoided.passed is False

    other_than = score_eval_results(
        cases,
        (
            EvalBatchResult(
                batch_id="batch-a",
                duration_ms=10,
                candidates=tuple(
                    EvalCandidate(
                        source_ordinal=index,
                        candidate_type="constraint",
                        content=(f"PostgreSQL must use a port other than 5432 for {index}."),
                    )
                    for index in range(50)
                ),
            ),
        ),
    )
    assert other_than.matched_count == 0
    assert other_than.passed is False
