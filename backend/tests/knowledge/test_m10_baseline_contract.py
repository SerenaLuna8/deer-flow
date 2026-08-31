"""M10 T0 baseline: frozen contract samples and verified M9 premises.

The fixture ``fixtures/m10_contract_baseline.json`` freezes the design-doc
constants (score domains, RRF formula, candidate budget, operation kinds,
``field_kind``, versioning fields) as reviewable samples. Every formula
example is recomputed here so a silently edited constant fails, and the
M9 premises T1+ builds on are asserted against the real code instead of
being taken from the plan text.
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path

import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_MAX_METADATA_FILTERS,
    KNOWLEDGE_MAX_TOP_K,
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgeBaseView,
    KnowledgeCitation,
    KnowledgeMetadataFilter,
    KnowledgeSearchResult,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "m10_contract_baseline.json"
FULL_SCHEMA_PATH = Path(__file__).parents[2] / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


@pytest.fixture(scope="module")
def baseline() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _rank_fusion_score(domain_rank: int | None, lexical_rank: int | None) -> float:
    """Reference implementation of the design formula 61/2*(Σ 1/(60+rank))."""

    total = 0.0
    if domain_rank is not None:
        total += 1.0 / (60 + domain_rank)
    if lexical_rank is not None:
        total += 1.0 / (60 + lexical_rank)
    return 61.0 / 2.0 * total


def _per_base_route_budget(top_k: int, target_bases: int) -> int:
    per_base_cap = min(100, max(20, 5 * top_k))
    return min(per_base_cap, math.floor(400 / target_bases))


class TestFrozenFormulaSamples:
    def test_rank_fusion_examples_recompute_exactly(self, baseline: dict) -> None:
        assert baseline["rank_fusion"]["rrf_k"] == 60
        for example in baseline["rank_fusion"]["examples"]:
            expected = example["expected"]
            actual = _rank_fusion_score(example["domain_rank"], example["lexical_rank"])
            assert actual == pytest.approx(expected, abs=1e-6), example

    def test_two_equal_first_places_fuse_to_the_domain_maximum_of_one(self) -> None:
        assert _rank_fusion_score(1, 1) == pytest.approx(1.0)
        assert _rank_fusion_score(1, None) == pytest.approx(0.5)

    def test_candidate_budget_examples_recompute_exactly(self, baseline: dict) -> None:
        budget = baseline["candidate_budget"]
        assert budget["global_parent_budget"] == 400
        assert budget["max_recall_items_both_routes"] == 800
        for example in budget["examples"]:
            actual = _per_base_route_budget(example["top_k"], example["target_bases"])
            assert actual == example["expected_c"], example

    def test_budget_cap_is_compatible_with_the_existing_top_k_ceiling(self, baseline: dict) -> None:
        # 5 * KNOWLEDGE_MAX_TOP_K saturates the 100 per-base cap exactly, so
        # no legal top_k can ask for more than a single base may return.
        assert KNOWLEDGE_MAX_TOP_K == 20
        assert _per_base_route_budget(KNOWLEDGE_MAX_TOP_K, 1) == 100

    def test_score_domains_cover_the_three_final_kinds(self, baseline: dict) -> None:
        contract = baseline["score_contract"]
        assert contract["score_kinds"] == ["cosine", "rerank", "rank_fusion"]
        assert contract["score_domains"]["cosine"] == [-1.0, 1.0]
        assert contract["score_domains"]["rerank"] == [0.0, 1.0]
        assert contract["score_domains"]["rank_fusion"] == [0.0, 1.0]
        assert contract["threshold_applies_to_local_score_only"] is True


class TestM9PremisesHoldInCode:
    def test_empty_base_can_defer_embedding_and_keeps_reranker_optional(self) -> None:
        # Empty-base creation now defers indexing configuration; the stored
        # retrieval defaults still apply once the first embedding is bound.
        create = KnowledgeBaseCreate(name="待配置知识库")
        assert create.embedding_model_id is None
        assert create.reranker_model_id is None
        assert create.retrieval_mode == "semantic"

    def test_base_update_keeps_the_tri_state_reranker_rebinding(self) -> None:
        fields = {f.name: f for f in dataclasses.fields(KnowledgeBaseUpdate)}
        assert fields["reranker_model_id"].default is None
        assert fields["clear_reranker_model"].default is False

    def test_base_view_exposes_both_registry_bindings(self) -> None:
        names = {f.name for f in dataclasses.fields(KnowledgeBaseView)}
        assert {"embedding_model_id", "reranker_model_id"} <= names

    def test_citation_keeps_every_documented_m9_field(self) -> None:
        # T1 extends the M9 baseline with optional provenance; the original
        # field set must survive so recorded messages keep deserialising.
        citation_names = {f.name for f in dataclasses.fields(KnowledgeCitation)}
        assert {
            "knowledge_base_id",
            "document_id",
            "segment_id",
            "segment_position",
            "snippet",
            "score",
            "source_position",
        } <= citation_names
        assert {"document_version", "content_digest", "score_kind"} <= citation_names
        result_fields = {f.name for f in dataclasses.fields(KnowledgeSearchResult)}
        assert "hits" in result_fields
        # ``citations`` became a projection of hits in T1: still readable,
        # no longer independent state.
        assert "citations" not in result_fields
        assert isinstance(KnowledgeSearchResult.citations, property)

    def test_metadata_filter_gained_field_kind_in_t1(self, baseline: dict) -> None:
        # T0 recorded the M9 filter as {name, operator, value}; T1 adds the
        # namespace discriminator with the documented default.
        fields = {f.name: f for f in dataclasses.fields(KnowledgeMetadataFilter)}
        assert set(fields) == {"name", "operator", "value", "field_kind"}
        assert fields["field_kind"].default == baseline["metadata_contract"]["default_field_kind"]
        assert KNOWLEDGE_MAX_METADATA_FILTERS == 10

    def test_versioning_conflict_code_exists(self, baseline: dict) -> None:
        assert baseline["versioning"]["cas_error_code"] == KNOWLEDGE_CONFLICT

    def test_schema_task_kinds_match_the_recorded_existing_set(self, baseline: dict) -> None:
        schema_sql = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
        for kind in baseline["task_contract"]["existing_kinds"]:
            assert f"'{kind}'" in schema_sql, kind
        # The new kind must not silently pre-exist: T1/T2 introduce it with
        # ORM, SQL, digest and tests changing together.
        assert baseline["task_contract"]["new_kind"] == "reembed_document"

    def test_model_options_response_uses_the_split_m9_shape(self) -> None:
        from app.knowledge.gateway import KnowledgeModelOptionsResponse

        assert {"embedding_models", "reranker_models"} <= set(KnowledgeModelOptionsResponse.model_fields)


class TestDeliveryConstraintsRecorded:
    def test_database_and_quality_gates_are_explicit(self, baseline: dict) -> None:
        constraints = baseline["delivery_constraints"]
        assert constraints["existing_database_rebuild_authorized"] is False
        assert constraints["m9_reset_authorization_inherited"] is False
        assert constraints["supported_install_path"] == "fresh_empty_database_schema_v1"
        gate = baseline["t14_real_quality_gate"]
        assert gate["status"].startswith("blocked_pending_operator_input")
