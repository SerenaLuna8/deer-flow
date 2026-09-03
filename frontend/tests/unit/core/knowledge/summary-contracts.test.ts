import { afterEach, describe, expect, rs, test } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));

import { fetch as authenticatedFetch } from "@/core/api/fetcher";
import { updateKnowledgeBase } from "@/core/knowledge/api";
import {
  knowledgeModelOptionsResponseSchema,
  knowledgeTaskProgressSchema,
  knowledgeSegmentDetailResponseSchema,
  knowledgeSearchDiagnosticsSchema,
} from "@/core/knowledge/types";

const fetchMock = rs.mocked(authenticatedFetch);
afterEach(() => {
  fetchMock.mockReset();
});

const base = {
  id: "40000000-0000-4000-8000-000000000001",
  project_id: "10000000-0000-4000-8000-000000000001",
  name: "Knowledge",
  description: "",
  embedding_model_id: "30000000-0000-4000-8000-000000000001",
  reranker_model_id: null,
  retrieval_mode: "semantic",
  summary_index_enabled: true,
  status: "active",
  document_count: 2,
  default_top_k: 4,
  default_score_threshold: 0.2,
  default_relative_cutoff: null,
  delete_error: null,
  created_at: "2026-08-31T00:00:00Z",
  updated_at: "2026-08-31T00:00:00Z",
};

describe("Knowledge summary model projection", () => {
  test("accepts only the safe summary model identity or an explicit null", () => {
    const payload = {
      embedding_models: [],
      reranker_models: [],
      summary_model: {
        model_name: "40000000-0000-4000-8000-000000000001",
        display_name: "Summary model",
      },
      request_id: "request",
    };
    expect(knowledgeModelOptionsResponseSchema.safeParse(payload).success).toBe(
      true,
    );
    expect(
      knowledgeModelOptionsResponseSchema.safeParse({
        ...payload,
        summary_model: null,
      }).success,
    ).toBe(true);
    expect(
      knowledgeModelOptionsResponseSchema.safeParse({
        ...payload,
        summary_model: { ...payload.summary_model, api_key: "forbidden" },
      }).success,
    ).toBe(false);
  });
});

test("updating a base preserves the atomic backfill receipt", async () => {
  fetchMock.mockResolvedValue(
    new Response(
      JSON.stringify({
        item: base,
        summary_backfill: {
          accepted_document_count: 1,
          skipped_document_ids: ["50000000-0000-4000-8000-000000000001"],
        },
        request_id: "backfill",
      }),
      { status: 200 },
    ),
  );

  const result = await updateKnowledgeBase(base.project_id, base.id, {
    summary_index_enabled: true,
  });

  expect(result.item.summary_index_enabled).toBe(true);
  expect(result.summary_backfill).toEqual({
    accepted_document_count: 1,
    skipped_document_ids: ["50000000-0000-4000-8000-000000000001"],
  });
});

test("a ready document can carry a running summary task", () => {
  expect(
    knowledgeTaskProgressSchema.safeParse({
      kind: "summarize_document",
      status: "running",
      stage: "summarizing",
      completed_units: 1,
      total_units: 3,
      attempt_count: 1,
      max_attempts: 3,
      target_version: 1,
      next_attempt_at: null,
    }).success,
  ).toBe(true);
});

test("segment details accept a read-only summary without exposing its vector", () => {
  const detail = {
    segment: {
      id: base.id,
      document_version: 1,
      position: 1,
      content: "Original passage",
      word_count: 16,
      enabled: true,
      hit_count: 0,
      source_position: {},
      created_at: base.created_at,
      token_count: 2,
      source_spans: [],
    },
    knowledge_base_id: base.id,
    document_id: base.id,
    document_name: "Manual",
    content_state: "current",
    stored_content_version: 1,
    current_document_version: 1,
    children_total: 0,
    child_page: 1,
    children: [],
    attachments: [],
    request_id: "detail",
    summary: { content: "Generated summary", created_at: base.created_at },
  };
  expect(knowledgeSegmentDetailResponseSchema.safeParse(detail).success).toBe(
    true,
  );
  expect(
    knowledgeSegmentDetailResponseSchema.safeParse({ ...detail, summary: null })
      .success,
  ).toBe(true);
  expect(
    knowledgeSegmentDetailResponseSchema.safeParse({
      ...detail,
      summary: { ...detail.summary, embedding: [1, 0] },
    }).success,
  ).toBe(false);
});

test("diagnostics carry bounded source attribution and cache counts", () => {
  const diagnostics = {
    strategy_version: "m10-v1",
    lexical_version: 1,
    target_base_count: 1,
    effective_top_k: 4,
    per_base_route_budget: 20,
    retrieval_mode: "semantic",
    counts: {
      semantic_candidates: 1,
      lexical_candidates: 0,
      summary_candidates: 1,
      query_embedding_cache_hits: 1,
      query_embedding_cache_misses: 0,
      parents_deduplicated: 0,
      threshold_filtered: 0,
      relative_filtered: 0,
      lexical_threshold_exempt: 0,
      stale_filtered: 0,
      returned: 1,
    },
    timings: {
      query_embedding_ms: 0,
      recall_ms: 1,
      rerank_ms: 0,
      final_validation_ms: 1,
    },
    model_ids: [base.embedding_model_id],
    ranking_method: "cosine",
    empty_reason: null,
    heterogeneous_without_lexical_evidence: false,
    lexical_query_token_count: 0,
    lexical_query_truncated: false,
    hit_diagnostics: [
      {
        segment_id: base.id,
        local_score: 0.9,
        local_score_kind: "cosine",
        score_domain: "embedding",
        ranking_method: "cosine",
        ranking_score: 0.9,
        matched_children: [],
        matched_via: "summary",
      },
    ],
  };
  expect(knowledgeSearchDiagnosticsSchema.safeParse(diagnostics).success).toBe(
    true,
  );
  expect(
    knowledgeSearchDiagnosticsSchema.safeParse({
      ...diagnostics,
      hit_diagnostics: [
        { ...diagnostics.hit_diagnostics[0], matched_via: "llm" },
      ],
    }).success,
  ).toBe(false);
});
