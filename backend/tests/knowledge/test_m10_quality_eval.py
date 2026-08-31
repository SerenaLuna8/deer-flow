"""M10 T14 quality evaluation: offline gate arithmetic and opt-in real run."""

from __future__ import annotations

import os

import eval_quality as quality
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


def _good_quality_evidence(*, hybrid_ms=20.0):
    rows = [
        QueryOutcome(
            f"{category}-{mode}",
            "holdout",
            category,
            mode,
            recall_candidate=True,
            recall_at_10=True,
            ndcg_at_10=1.0,
            returned=0 if category == "no_answer" else 1,
            non_provider_ms=10.0 if mode == "semantic" else hybrid_ms,
        )
        for category in ("identifier", "natural_language", "no_answer", "tail")
        for mode in ("semantic", "hybrid")
    ]
    return summarize(rows), quality.load_legacy_m10_corpus()


def test_good_m10_quality_with_slow_p95_requires_explicit_review_by_default():
    summary, corpus = _good_quality_evidence()
    result = evaluate_gates(summary, corpus)
    assert result["quality_passed"] is True
    assert result["all_passed"] is False
    assert result["p95_review_pending"] is True and result["p95_review_recorded"] is False
    assert result["checks"]["p95_non_provider"]["passed"] is False
    assert result["checks"]["p95_non_provider"]["review"] is None


@pytest.mark.parametrize("review", [None, "", " \t "])
def test_m10_missing_or_blank_review_cannot_approve_slow_p95(review):
    summary, corpus = _good_quality_evidence()
    result = evaluate_gates(summary, corpus, latency_review=review)
    assert result["all_passed"] is False and result["p95_review_recorded"] is False


def test_m10_explicit_operator_review_can_approve_performance_without_changing_quality():
    summary, corpus = _good_quality_evidence()
    result = evaluate_gates(summary, corpus, latency_review=" Operator approved this measured P95 for the test deployment. ")
    assert result["quality_passed"] is True and result["all_passed"] is True
    assert result["p95_review_recorded"] is True and result["p95_review_pending"] is False
    assert result["checks"]["p95_non_provider"]["review"] == "Operator approved this measured P95 for the test deployment."


@pytest.mark.parametrize("hybrid_ms,review,expected_text", [(20.0, None, "待操作者显式复审"), (20.0, "Accepted measured P95 in test.", "已记录操作者显式复审"), (11.0, None, "未触发复审")])
def test_m10_report_distinguishes_performance_review_state(tmp_path, monkeypatch, hybrid_ms, review, expected_text):
    summary, corpus = _good_quality_evidence(hybrid_ms=hybrid_ms)
    gates = evaluate_gates(summary, corpus) if review is None else evaluate_gates(summary, corpus, latency_review=review)
    report = {
        "ran_at": "test-run",
        "hardware": "test-hardware",
        "models": {"primary_embedding": "primary", "secondary_embedding": "secondary", "reranker": "rerank"},
        "corpus_queries": 4,
        "holdout_queries": 4,
        "holdout_identifiers": 1,
        "retrieval_units": 10000,
        "usage": {"embed_texts": 4, "embed_tokens": 10, "rerank_docs": 4, "rerank_tokens": 10, "estimated_cny": 0.01},
        "gates": gates,
        "summary": summary,
        "deployment_note": "Isolated test data only.",
    }
    monkeypatch.setattr(quality, "REPORT_JSON_PATH", tmp_path / "report.json")
    monkeypatch.setattr(quality, "REPORT_MD_PATH", tmp_path / "report.md")
    quality.write_report(report)
    rendered = (tmp_path / "report.md").read_text()
    assert expected_text in rendered
    assert "未经当前验证" in rendered and "不是实测账单" in rendered
    assert "产品接受该成本" not in rendered and "增量来自库内词法扫描" not in rendered


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.provider_integration
async def test_m10_holdout_quality_gates_against_real_models(postgres_database_url: str) -> None:
    if os.environ.get("ACT_WEAVE_KNOWLEDGE_QUALITY_EVAL") != "1":
        pytest.skip("set ACT_WEAVE_KNOWLEDGE_QUALITY_EVAL=1 to run the real-model quality gate")
    api_key = resolve_provider_api_key()
    if not api_key:
        pytest.fail("real-model quality eval is opted in but no SiliconFlow key is available")
    report = await run_quality_eval(postgres_database_url, api_key=api_key, latency_review=os.environ.get("ACT_WEAVE_KNOWLEDGE_M10_P95_REVIEW"))
    assert report["retrieval_units"] >= 10_000
    assert report["holdout_identifiers"] >= 20
    assert report["gates"]["all_passed"], report["gates"]
    if report["gates"].get("p95_review_recorded"):
        assert report["gates"]["checks"]["p95_non_provider"].get("review")
