"""M10 T14: frozen evaluation corpus contract and metric worked examples.

The fixture is the reviewable source of truth. Queries are identified by
``source_id + position + content digest``, never by a database UUID.
Splits are frozen: tuning (none in this delivery) may use ``dev`` only;
acceptance reports only ``holdout``.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest
from eval_metrics import (
    NDCG_K,
    dcg_at_k,
    false_recall_rate,
    gain,
    ndcg_at_k,
    recall_hit,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "m10_retrieval_cases.json"

REQUIRED_CATEGORIES = {
    "identifier",
    "natural_language",
    "tail",
    "parent_child",
    "cross_base",
    "metadata",
    "no_answer",
    "question_style",
}


@pytest.fixture(scope="module")
def corpus() -> dict:
    assert FIXTURE_PATH.is_file(), f"missing frozen corpus {FIXTURE_PATH}"
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class TestMetricWorkedExamples:
    def test_gain_matches_design_formula(self) -> None:
        assert gain(0) == 0.0
        assert gain(1) == 1.0
        assert gain(2) == 3.0

    def test_ndcg_perfect_first_place_is_one(self) -> None:
        assert ndcg_at_k({"a": 2}, ["a", "b"], k=10) == pytest.approx(1.0)

    def test_ndcg_grade2_at_rank2_is_worked_example(self) -> None:
        # DCG = 3 / log2(3); IDCG = 3 / log2(2) = 3
        expected = (3.0 / 1.584962500721156) / 3.0
        assert ndcg_at_k({"a": 2}, ["b", "a"], k=10) == pytest.approx(expected, abs=1e-9)

    def test_idcg_zero_no_answer_is_excluded_not_zeroed(self) -> None:
        assert ndcg_at_k({}, ["noise"], k=10) is None

    def test_dcg_discount_uses_log2_rank_plus_one(self) -> None:
        assert dcg_at_k([2], k=1) == pytest.approx(3.0)
        # rank 2 discount is log2(3) ≈ 1.5849625
        assert dcg_at_k([2, 1], k=2) == pytest.approx(3.0 + 1.0 / 1.5849625007211563, abs=1e-9)

    def test_recall_is_any_grade2_presence(self) -> None:
        assert recall_hit(["src:1:aaa"], ["other", "src:1:aaa"]) is True
        assert recall_hit(["src:1:aaa"], ["other"]) is False

    def test_no_answer_false_recall_is_share_of_nonempty_returns(self) -> None:
        assert false_recall_rate([0, 0, 3, 1]) == pytest.approx(0.5)


class TestFrozenCorpusContract:
    def test_existing_m10_queries_and_documents_remain_frozen(self, corpus: dict) -> None:
        legacy = {
            "queries": [item for item in corpus["queries"] if item["category"] != "question_style"],
            "documents": [item for item in corpus["documents"] if not item["source_id"].startswith("m11-question-")],
        }
        expected = {
            "queries": (65, "fb20703fa950571050300e172ec0457f2250b04340e3f1356401652fda4394e5"),
            "documents": (20, "f27ca1a442f7d4a2bd8726915ebff54f7e1f560840e133ad57ad8301711ea52b"),
        }
        for kind, (count, digest) in expected.items():
            assert len(legacy[kind]) == count
            canonical = json.dumps(legacy[kind], ensure_ascii=False, sort_keys=True).encode("utf-8")
            assert hashlib.sha256(canonical).hexdigest() == digest

    def test_generated_cases_match_the_frozen_fixture(self, corpus: dict) -> None:
        from _generate_m10_eval_corpus import documents, queries, question_documents, question_queries

        assert corpus["documents"] == documents() + question_documents()
        assert corpus["queries"] == queries() + question_queries()

    def test_question_style_has_ten_independent_cases_in_each_split(self, corpus: dict) -> None:
        cases = [item for item in corpus["queries"] if item["category"] == "question_style"]
        split_sources: dict[str, set[str]] = {"dev": set(), "holdout": set()}
        for split in split_sources:
            selected = [item for item in cases if item["split"] == split]
            assert len(selected) >= 10
            for item in selected:
                split_sources[split].update(judgment["source_id"] for judgment in item["judgments"])
        assert split_sources["dev"].isdisjoint(split_sources["holdout"])
        documents = {document["source_id"]: document for document in corpus["documents"]}
        for case in cases:
            other_split = "holdout" if case["split"] == "dev" else "dev"
            for source in split_sources[other_split]:
                assert all(case["answer_marker"] not in segment["content"] for segment in documents[source]["segments"])

    def test_question_style_uses_long_factual_passages_and_three_grades(self, corpus: dict) -> None:
        documents = {document["source_id"]: document for document in corpus["documents"]}
        cases = [item for item in corpus["queries"] if item["category"] == "question_style"]
        assert cases
        for query in cases:
            assert {item["grade"] for item in query["judgments"]} == {0, 1, 2}
            assert query["query"].endswith("？")
            marker = query["answer_marker"]
            assert marker not in query["query"]
            for judgment in query["judgments"]:
                document = documents[judgment["source_id"]]
                segment = next(item for item in document["segments"] if item["position"] == judgment["position"])
                if judgment["grade"] == 2:
                    assert len(segment["content"]) >= 200
                    assert marker in segment["content"]
                    assert query["query"].rstrip("？") not in segment["content"]
                else:
                    assert marker not in segment["content"]

    def test_m11_gates_are_additive_and_preserve_m10_thresholds(self, corpus: dict) -> None:
        gates = corpus["gates"]
        assert gates["identifier_recall_candidate_min"] == 0.95
        assert gates["identifier_recall_at_10_min"] == 0.95
        assert gates["natural_language_recall_regression_max"] == 0.02
        assert gates["natural_language_ndcg_regression_max"] == 0.02
        assert gates["p95_regression_review_ratio"] == 1.5
        assert gates["question_recall_candidate_uplift_pp"] == 5
        assert gates["question_recall_at_10_uplift_pp"] == 5
        assert gates["overall_ndcg_regression"] == 0.02
        assert gates["no_answer_false_recall_not_worse"] is True
        assert gates["existing_category_recall_not_worse"] is True
        assert gates["p95_regression_review_ratio_m11"] == 1.2

    def test_source_is_synthetic_and_desensitized(self, corpus: dict) -> None:
        source = corpus["source"]
        assert source["kind"] == "synthetic_desensitized"
        assert source["pii"] is False
        assert "method" in source
        assert corpus["annotation_unit"] == "parent_segment"

    def test_query_count_and_holdout_floors(self, corpus: dict) -> None:
        queries = corpus["queries"]
        assert len(queries) >= 60
        holdout = [item for item in queries if item["split"] == "holdout"]
        dev = [item for item in queries if item["split"] == "dev"]
        assert len(holdout) >= 30
        assert len(dev) >= 1
        assert {item["split"] for item in queries} <= {"dev", "holdout"}

    def test_holdout_identifier_floor(self, corpus: dict) -> None:
        holdout_ids = [item for item in corpus["queries"] if item["split"] == "holdout" and item["category"] == "identifier"]
        assert len(holdout_ids) >= 20

    def test_categories_cover_design_section_11(self, corpus: dict) -> None:
        present = {item["category"] for item in corpus["queries"]}
        assert REQUIRED_CATEGORIES <= present

    def test_both_splits_are_stratified(self, corpus: dict) -> None:
        by_split: dict[str, set[str]] = {"dev": set(), "holdout": set()}
        for item in corpus["queries"]:
            by_split[item["split"]].add(item["category"])
        for split, categories in by_split.items():
            missing = REQUIRED_CATEGORIES - categories
            assert not missing, f"{split} missing {sorted(missing)}"

    def test_identities_are_source_position_digest_not_uuids(self, corpus: dict) -> None:
        documents = {document["source_id"]: document for document in corpus["documents"]}
        seen_query_ids: set[str] = set()
        for query in corpus["queries"]:
            assert query["id"] not in seen_query_ids
            seen_query_ids.add(query["id"])
            if query["category"] == "no_answer":
                assert query["judgments"] == []
                continue
            assert query["judgments"], query["id"]
            grades = {item["grade"] for item in query["judgments"]}
            assert grades <= {0, 1, 2}
            if query["category"] != "no_answer":
                assert 2 in grades or query["category"] == "no_answer"
            for judgment in query["judgments"]:
                document = documents[judgment["source_id"]]
                segment = next(item for item in document["segments"] if item["position"] == judgment["position"])
                digest = hashlib.sha256(segment["content"].encode("utf-8")).hexdigest()
                assert judgment["content_sha256"] == digest
                assert "segment_id" not in judgment
                assert "uuid" not in judgment

    def test_parent_child_and_tail_have_distinct_shapes(self, corpus: dict) -> None:
        documents = {document["source_id"]: document for document in corpus["documents"]}
        pc_queries = [item for item in corpus["queries"] if item["category"] == "parent_child"]
        assert pc_queries
        for query in pc_queries:
            target = query["judgments"][0]
            document = documents[target["source_id"]]
            assert document["chunking_mode"] == "parent_child"
            segment = next(item for item in document["segments"] if item["position"] == target["position"])
            assert len(segment.get("children") or []) >= 2

        tail_queries = [item for item in corpus["queries"] if item["category"] == "tail"]
        assert tail_queries
        for query in tail_queries:
            target = next(item for item in query["judgments"] if item["grade"] == 2)
            document = documents[target["source_id"]]
            segment = next(item for item in document["segments"] if item["position"] == target["position"])
            assert len(segment["content"]) > 320
            marker = query["answer_marker"]
            assert marker in segment["content"]
            assert segment["content"].rindex(marker) > 320

    def test_scale_target_is_at_least_ten_thousand_units(self, corpus: dict) -> None:
        assert corpus["parameters"]["scale_retrieval_units"] >= 10_000
        assert corpus["parameters"]["top_k"] == NDCG_K
        assert corpus["parameters"]["score_threshold"] == 0.2

    def test_identifier_tokens_are_unique_across_grade2_targets(self, corpus: dict) -> None:
        documents = {document["source_id"]: document for document in corpus["documents"]}
        tokens: list[str] = []
        for query in corpus["queries"]:
            if query["category"] != "identifier":
                continue
            token = query["identifier_token"]
            tokens.append(token)
            grade2 = [item for item in query["judgments"] if item["grade"] == 2]
            assert grade2, query["id"]
            for judgment in grade2:
                document = documents[judgment["source_id"]]
                segment = next(item for item in document["segments"] if item["position"] == judgment["position"])
                assert token in segment["content"]
        assert len(tokens) == len(set(tokens))
        counts = Counter(tokens)
        assert counts.most_common(1)[0][1] == 1
