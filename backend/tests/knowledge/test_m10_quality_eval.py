"""M10 T14 quality evaluation: offline gate arithmetic and opt-in real run."""

from __future__ import annotations

import os

import pytest
from eval_metrics import recall_hit
from eval_quality import (
    QueryOutcome,
    evaluate_gates,
    identity_key,
    resolve_provider_api_key,
    run_quality_eval,
    summarize,
)


def test_identity_key_uses_digest_not_uuid() -> None:
    key = identity_key("ops-error-codes", 1, "EMG-3088 body")
    assert key.startswith("ops-error-codes:1:")
    assert "-" not in key.split(":")[2] or len(key.split(":")[2]) == 64


def test_summarize_keeps_no_answer_out_of_recall_mean() -> None:
    rows = [
        QueryOutcome("a", "holdout", "identifier", "hybrid", recall_candidate=True, recall_at_10=True, ndcg_at_10=1.0),
        QueryOutcome("b", "holdout", "no_answer", "hybrid", returned=2),
    ]
    summary = summarize(rows)
    assert summary["holdout"]["identifier"]["hybrid"]["recall_at_10"] == 1.0
    assert "false_recall" in summary["holdout"]["no_answer"]["hybrid"]
    assert "recall_at_10" not in summary["holdout"]["no_answer"]["hybrid"]


def test_identifier_gate_fails_when_hybrid_recall_is_below_95() -> None:
    corpus = {
        "gates": {
            "identifier_recall_candidate_min": 0.95,
            "identifier_recall_at_10_min": 0.95,
            "natural_language_recall_regression_max": 0.02,
            "natural_language_ndcg_regression_max": 0.02,
            "p95_regression_review_ratio": 1.5,
        }
    }
    summary = {
        "holdout": {
            "identifier": {
                "hybrid": {"recall_candidate": 0.9, "recall_at_10": 0.9},
                "semantic": {"recall_at_10": 0.4},
            },
            "natural_language": {
                "hybrid": {"recall_at_10": 1.0, "ndcg_at_10": 1.0, "p95_non_provider_ms": 10.0},
                "semantic": {"recall_at_10": 1.0, "ndcg_at_10": 1.0, "p95_non_provider_ms": 10.0},
            },
            "no_answer": {"hybrid": {"false_recall": 0.0}, "semantic": {"false_recall": 0.0}},
            "tail": {"hybrid": {"recall_at_10": 1.0, "misses": []}},
        }
    }
    gates = evaluate_gates(summary, corpus)
    assert gates["quality_passed"] is False
    assert gates["checks"]["identifier_recall_at_10_hybrid"]["passed"] is False


def test_recall_helper_rejects_wrong_identity() -> None:
    assert recall_hit(["src:1:aaa"], ["src:2:aaa"]) is False


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.provider_integration
async def test_m10_holdout_quality_gates_against_real_models(postgres_database_url: str) -> None:
    if os.environ.get("ACT_WEAVE_KNOWLEDGE_QUALITY_EVAL") != "1":
        pytest.skip("set ACT_WEAVE_KNOWLEDGE_QUALITY_EVAL=1 to run the real-model quality gate")
    api_key = resolve_provider_api_key()
    if not api_key:
        pytest.fail("real-model quality eval is opted in but no SiliconFlow key is available")
    report = await run_quality_eval(postgres_database_url, api_key=api_key)
    assert report["retrieval_units"] >= 10_000
    assert report["holdout_identifiers"] >= 20
    assert report["gates"]["quality_passed"], report["gates"]
    if report["gates"].get("p95_review_recorded"):
        assert report["gates"]["checks"]["p95_non_provider"].get("review")
