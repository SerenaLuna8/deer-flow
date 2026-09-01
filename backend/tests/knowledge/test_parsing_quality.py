"""P4-T6 parsing-quality evaluation contracts."""

from __future__ import annotations

import os
import re
from pathlib import Path

import eval_quality as quality
import pytest
from eval_metrics import mean_or_none, recall_hit, reciprocal_rank_at_k

CASES_PATH = Path(__file__).parent / "fixtures" / "parsing_retrieval_cases.json"


def test_quality_metric_fixture_is_unambiguous() -> None:
    ranked = ["wrong", "correct"]
    assert recall_hit(["correct"], ranked[:5]) is True
    assert reciprocal_rank_at_k(["correct"], ranked, k=5) == 0.5
    assert reciprocal_rank_at_k(["correct"], ["wrong"] * 5 + ["correct"], k=5) == 0.0
    assert mean_or_none([0.5, 0.0]) == 0.25


def test_fixed_quality_set_has_six_source_grounded_categories() -> None:
    from parsing_quality import BASELINE_COMMIT, load_parsing_cases

    cases = load_parsing_cases(CASES_PATH)
    assert cases["baseline_commit"] == BASELINE_COMMIT == "b96581974b057c0ae4d853815130d99c0ed23823"
    assert cases["retrieval"] == {"mode": "hybrid", "top_k": 5, "score_threshold": 0.0}
    categories = {document["category"] for document in cases["documents"]}
    assert categories == {
        "missing_header_column",
        "leading_zero_identifier",
        "long_table_cross_chunk",
        "word_heading_steps",
        "markdown_generic_literal",
        "image_only_no_answer",
    }
    for category in categories:
        queries = [item for item in cases["queries"] if item["category"] == category]
        assert len(queries) >= 3
        for query in queries:
            if category == "image_only_no_answer":
                assert query["expected_no_answer"] is True
                assert query["relevance"] == []
            else:
                assert query["expected_no_answer"] is False
                assert query["relevance"]
                assert all(set(label) == {"source_id", "location", "must_contain"} for label in query["relevance"])
                assert all(label["location"] for label in query["relevance"])
                assert all(label["must_contain"] for label in query["relevance"])
                assert all("segment_id" not in label and "position" not in label for label in query["relevance"])


def test_relevance_mapping_uses_source_location_and_required_original_text() -> None:
    from parsing_quality import relevance_identity, segment_relevance_ids

    label = {
        "source_id": "inventory",
        "location": {"row": 4, "column": 2},
        "must_contain": "不可丢列",
    }
    segment = {
        "content": "- 编号: 00123\n- 列 B: 不可丢列",
        "source_position": {"row": 4},
        "source_spans": [
            {"location": {"row": 4, "column": 1}, "role": "source"},
            {"location": {"row": 4, "column": 2}, "role": "source"},
        ],
    }
    assert segment_relevance_ids(segment, "inventory", [label]) == [relevance_identity(label)]
    assert segment_relevance_ids({**segment, "content": "不可丢列"}, "other", [label]) == []
    assert segment_relevance_ids({**segment, "source_spans": []}, "inventory", [label]) == []


def test_quality_comparison_uses_only_queries_with_labels_on_both_sides() -> None:
    from parsing_quality import paired_quality_comparison

    observations = {
        "baseline": [
            {"id": "paired", "category": "word", "source_labels_available": True, "mrr_at_5": 0.5, "hit_at_5": True, "error": None},
            {"id": "missing", "category": "markdown", "source_labels_available": False, "mrr_at_5": None, "hit_at_5": None, "error": None},
        ],
        "candidate": [
            {"id": "paired", "category": "word", "source_labels_available": True, "mrr_at_5": 1.0, "hit_at_5": True, "error": None},
            {"id": "missing", "category": "markdown", "source_labels_available": True, "mrr_at_5": 1.0, "hit_at_5": True, "error": None},
        ],
    }
    comparison = paired_quality_comparison(observations)
    assert comparison["overall"] == {
        "paired_queries": 1,
        "baseline_hit_at_5": 1.0,
        "candidate_hit_at_5": 1.0,
        "hit_at_5_change": 0.0,
        "baseline_mrr_at_5": 0.5,
        "candidate_mrr_at_5": 1.0,
        "mrr_at_5_change": 0.5,
    }
    assert comparison["categories"]["markdown"]["paired_queries"] == 0


def test_fixed_baseline_export_and_candidate_process_the_same_originals(tmp_path: Path) -> None:
    from parsing_quality import build_parsing_corpora, prepare_baseline_export

    baseline = prepare_baseline_export(tmp_path / "baseline")
    corpora = build_parsing_corpora(
        baseline_path=baseline,
        cases_path=CASES_PATH,
        work_path=tmp_path / "work",
    )
    assert corpora["baseline"]["commit"] == "b96581974b057c0ae4d853815130d99c0ed23823"
    assert corpora["candidate"]["revision"]
    assert corpora["baseline"]["source_sha256"] == corpora["candidate"]["source_sha256"]
    assert set(corpora["baseline"]["source_sha256"]) == {
        "missing-header-column",
        "leading-zero-identifiers",
        "long-table",
        "word-heading-steps",
        "markdown-generics",
        "image-only",
    }
    assert corpora["baseline"]["indexed_segments_by_source"]["image-only"] == 0
    assert corpora["candidate"]["indexed_segments_by_source"]["image-only"] == 0
    assert corpora["candidate"]["source_assertions_matched"] > corpora["baseline"]["source_assertions_matched"]
    assert corpora["baseline"]["source_assertions_missing"]


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_replay_runs_both_corpora_through_production_search_without_quality_claim(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    from parsing_quality import prepare_baseline_export, run_parsing_quality_eval

    report = await run_parsing_quality_eval(
        postgres_database_url,
        baseline_path=prepare_baseline_export(tmp_path / "baseline"),
        cases_path=CASES_PATH,
        api_key=None,
    )
    assert report["execution_mode"] == "replay"
    assert report["quality_comparison"] is None
    assert report["quality_conclusion"] == "not_evaluated"
    assert report["retrieval"]["implementation"] == "KnowledgeSearchService"
    assert report["retrieval"]["top_k"] == 5
    assert report["retrieval"]["search_invocations"] == 30
    assert report["provider_calls"] == 0
    assert report["failed_queries"] == 0
    assert report["answer_queries"] == 15
    assert {key: report["corpus"][key] for key in ("sources", "categories", "answer_queries", "no_answer_queries")} == {
        "sources": 6,
        "categories": 6,
        "answer_queries": 15,
        "no_answer_queries": 3,
    }
    assert len(report["corpus"]["source_sha256"]) == 6
    assert report["profiles"]["baseline"]["unit"] == "character"
    assert {profile["chunk"]["unit"] for profile in report["profiles"]["candidate"].values()} == {"token"}
    assert report["no_answer"] == {
        "category": "image_only_no_answer",
        "queries": 3,
        "baseline_indexed_segments": 0,
        "candidate_indexed_segments": 0,
        "included_in_answer_means": False,
    }
    assert set(report["replay_observations"]) == {"baseline", "candidate"}
    assert report["models"]["same_configuration"] is True
    assert report["models"]["base_bindings"]["baseline"] == report["models"]["base_bindings"]["candidate"]
    for key in ("provider", "embedding", "reranker"):
        assert report["models"][key]["id"]
        assert report["models"][key]["revision"]
        assert re.fullmatch(r"[0-9a-f]{64}", report["models"][key]["config_digest"])
    assert re.fullmatch(r"[0-9a-f]{64}", report["candidate"]["working_tree_digest"])
    assert "base_url" not in report["models"]["provider"]


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.provider_integration
async def test_parsing_quality_against_real_models(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    if os.environ.get("ACT_WEAVE_KNOWLEDGE_PARSING_QUALITY_EVAL") != "1":
        pytest.skip("set ACT_WEAVE_KNOWLEDGE_PARSING_QUALITY_EVAL=1 to run the real-model parsing-quality gate")
    api_key = quality.resolve_provider_api_key()
    if not api_key:
        pytest.fail("parsing-quality Provider evaluation is opted in but no configured key is available")

    from parsing_quality import prepare_baseline_export, run_parsing_quality_eval

    report = await run_parsing_quality_eval(
        postgres_database_url,
        baseline_path=prepare_baseline_export(tmp_path / "baseline"),
        cases_path=CASES_PATH,
        api_key=api_key,
    )
    assert report["execution_mode"] == "provider"
    assert report["failed_queries"] == 0
    assert report["quality_comparison"] is not None
    assert report["quality_conclusion"] in {"improved", "unchanged", "regressed", "incomplete_source_labels"}
