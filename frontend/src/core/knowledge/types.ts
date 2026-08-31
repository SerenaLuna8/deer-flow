import { z } from "zod";

export const knowledgeBaseStatusSchema = z.enum([
  "active",
  "disabled",
  "deleting",
]);
export type KnowledgeBaseStatus = z.infer<typeof knowledgeBaseStatusSchema>;

export const knowledgeDocumentStatusSchema = z.enum([
  "uploading",
  "queued",
  "processing",
  "ready",
  "failed",
  "deleting",
]);
export type KnowledgeDocumentStatus = z.infer<
  typeof knowledgeDocumentStatusSchema
>;

/** Document statuses the list keeps polling for until they settle. */
export const KNOWLEDGE_DOCUMENT_ACTIVE_STATUSES: readonly KnowledgeDocumentStatus[] =
  ["uploading", "queued", "processing", "deleting"];

/** Escaped form as the user types it; the backend decodes \n\t\r at use time. */
export const DEFAULT_CHUNK_SEPARATOR = "\\n\\n";
export const DEFAULT_CHILD_CHUNK_SEPARATOR = "\\n";

export const knowledgeChunkingModeSchema = z.enum(["general", "parent_child"]);
export type KnowledgeChunkingMode = z.infer<typeof knowledgeChunkingModeSchema>;

export const knowledgeRetrievalModeSchema = z.enum(["semantic", "hybrid"]);
export type KnowledgeRetrievalMode = z.infer<
  typeof knowledgeRetrievalModeSchema
>;

export const knowledgeBaseItemSchema = z
  .object({
    id: z.string().uuid(),
    project_id: z.string().uuid(),
    name: z.string(),
    description: z.string(),
    embedding_model_id: z.string().uuid().nullable(),
    reranker_model_id: z.string().uuid().nullable(),
    retrieval_mode: knowledgeRetrievalModeSchema,
    status: knowledgeBaseStatusSchema,
    document_count: z.number().int(),
    default_top_k: z.number().int(),
    default_score_threshold: z.number(),
    delete_error: z.string().nullable(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict();
export type KnowledgeBaseItem = z.infer<typeof knowledgeBaseItemSchema>;

export const knowledgeBaseListResponseSchema = z
  .object({
    items: z.array(knowledgeBaseItemSchema),
    total: z.number().int(),
    page: z.number().int(),
    page_size: z.number().int(),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeBaseListResponse = z.infer<
  typeof knowledgeBaseListResponseSchema
>;

export const knowledgeBaseMutationResponseSchema = z
  .object({
    item: knowledgeBaseItemSchema,
    request_id: z.string(),
  })
  .strict();

/** Re-embed admission outcome: the rebound base plus per-document counts. */
export const knowledgeBaseRebuildResponseSchema = z
  .object({
    item: knowledgeBaseItemSchema,
    accepted_document_count: z.number().int().nonnegative(),
    skipped_document_ids: z.array(z.string().uuid()),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeBaseRebuildResponse = z.infer<
  typeof knowledgeBaseRebuildResponseSchema
>;

/** Values keyed by the base's metadata-field name (string or number). */
export const knowledgeDocumentMetadataSchema = z.record(
  z.union([z.string(), z.number()]),
);
export type KnowledgeDocumentMetadata = z.infer<
  typeof knowledgeDocumentMetadataSchema
>;

/** Real pipeline stage of an indexing task; orthogonal to task status. */
export const knowledgeTaskStageSchema = z.enum([
  "queued",
  "reading_source",
  "extracting_splitting",
  "loading_segments",
  "embedding",
  "publishing",
  "done",
]);
export type KnowledgeTaskStage = z.infer<typeof knowledgeTaskStageSchema>;

/**
 * Progress of the open indexing task bound to the document's current
 * generation. `total_units` is null while no verifiable total exists — never
 * render a simulated percentage from it.
 */
export const knowledgeTaskProgressSchema = z
  .object({
    kind: z.enum(["ingest_document", "reembed_document"]),
    status: z.enum(["queued", "running", "retry_wait", "failed"]),
    stage: knowledgeTaskStageSchema,
    completed_units: z.number().int().nonnegative(),
    total_units: z.number().int().nonnegative().nullable(),
    attempt_count: z.number().int().nonnegative(),
    max_attempts: z.number().int().positive(),
    target_version: z.number().int(),
    next_attempt_at: z.string().nullable(),
  })
  .strict();
export type KnowledgeTaskProgress = z.infer<typeof knowledgeTaskProgressSchema>;

export const knowledgeDocumentItemSchema = z
  .object({
    id: z.string().uuid(),
    project_id: z.string().uuid(),
    knowledge_base_id: z.string().uuid(),
    name: z.string(),
    original_name: z.string(),
    media_type: z.string().nullable(),
    size_bytes: z.number().int(),
    status: knowledgeDocumentStatusSchema,
    enabled: z.boolean(),
    version: z.number().int(),
    chunk_size: z.number().int(),
    chunk_overlap: z.number().int(),
    chunk_separator: z.string(),
    remove_extra_spaces: z.boolean(),
    remove_urls_emails: z.boolean(),
    chunking_mode: knowledgeChunkingModeSchema,
    child_chunk_size: z.number().int(),
    child_chunk_separator: z.string(),
    segment_count: z.number().int(),
    word_count: z.number().int(),
    hit_count: z.number().int(),
    doc_metadata: knowledgeDocumentMetadataSchema,
    error_message: z.string().nullable(),
    delete_error: z.string().nullable(),
    task_progress: knowledgeTaskProgressSchema.nullable(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict();
export type KnowledgeDocumentItem = z.infer<typeof knowledgeDocumentItemSchema>;

export const knowledgeDocumentListResponseSchema = z
  .object({
    items: z.array(knowledgeDocumentItemSchema),
    total: z.number().int(),
    page: z.number().int(),
    page_size: z.number().int(),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeDocumentListResponse = z.infer<
  typeof knowledgeDocumentListResponseSchema
>;

export const knowledgeDocumentMutationResponseSchema = z
  .object({
    item: knowledgeDocumentItemSchema,
    request_id: z.string(),
  })
  .strict();

export const knowledgeDocumentBatchResponseSchema = z
  .object({
    items: z.array(knowledgeDocumentItemSchema),
    request_id: z.string(),
  })
  .strict();

/** One active registry model a member may bind to a Knowledge Base. */
export const knowledgeModelOptionSchema = z
  .object({
    id: z.string().uuid(),
    provider_name: z.string(),
    model_name: z.string(),
    embedding_dimension: z.number().int().nullable(),
  })
  .strict();
export type KnowledgeModelOption = z.infer<typeof knowledgeModelOptionSchema>;

export const knowledgeModelOptionsResponseSchema = z
  .object({
    embedding_models: z.array(knowledgeModelOptionSchema),
    reranker_models: z.array(knowledgeModelOptionSchema),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeModelOptions = {
  embedding_models: KnowledgeModelOption[];
  reranker_models: KnowledgeModelOption[];
};

export const knowledgeSegmentItemSchema = z
  .object({
    id: z.string().uuid(),
    document_version: z.number().int(),
    position: z.number().int(),
    content: z.string(),
    word_count: z.number().int(),
    enabled: z.boolean(),
    hit_count: z.number().int(),
    source_position: z.record(z.unknown()),
    created_at: z.string(),
  })
  .strict();
export type KnowledgeSegmentItem = z.infer<typeof knowledgeSegmentItemSchema>;

export const knowledgeSegmentMutationResponseSchema = z
  .object({
    item: knowledgeSegmentItemSchema,
    request_id: z.string(),
  })
  .strict();

export const knowledgeSegmentListResponseSchema = z
  .object({
    items: z.array(knowledgeSegmentItemSchema),
    total: z.number().int(),
    page: z.number().int(),
    page_size: z.number().int(),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeSegmentListResponse = z.infer<
  typeof knowledgeSegmentListResponseSchema
>;

export const knowledgeHealthResponseSchema = z
  .object({
    enabled: z.boolean(),
    database_ok: z.boolean(),
    storage_ok: z.boolean(),
    message: z.string(),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeHealthResponse = z.infer<
  typeof knowledgeHealthResponseSchema
>;

export const knowledgeScoreKindSchema = z.enum([
  "cosine",
  "rerank",
  "rank_fusion",
]);
export type KnowledgeScoreKind = z.infer<typeof knowledgeScoreKindSchema>;

export const knowledgeSearchCitationSchema = z
  .object({
    knowledge_base_id: z.string().uuid(),
    knowledge_base_name: z.string(),
    document_id: z.string().uuid(),
    document_name: z.string(),
    segment_id: z.string().uuid(),
    segment_position: z.number().int(),
    snippet: z.string(),
    score: z.number(),
    source_position: z.record(z.unknown()),
    // New writes always provide these; historical citations legally lack
    // them and still render as short quotes with unknown provenance.
    document_version: z.number().int().nullable(),
    content_digest: z.string().nullable(),
    score_kind: knowledgeScoreKindSchema.nullable(),
  })
  .strict();
export type KnowledgeSearchCitation = z.infer<
  typeof knowledgeSearchCitationSchema
>;

export const knowledgeMatchedChildSchema = z
  .object({
    child_id: z.string().uuid(),
    position: z.number().int(),
    route: z.enum(["semantic", "lexical"]),
    score: z.number(),
  })
  .strict();
export type KnowledgeMatchedChild = z.infer<typeof knowledgeMatchedChildSchema>;

export const knowledgeHitDiagnosticsSchema = z
  .object({
    segment_id: z.string().uuid(),
    local_score: z.number(),
    local_score_kind: z.enum(["cosine", "rerank"]),
    score_domain: z.string(),
    ranking_method: knowledgeScoreKindSchema,
    ranking_score: z.number(),
    matched_children: z.array(knowledgeMatchedChildSchema),
  })
  .strict();
export type KnowledgeHitDiagnostics = z.infer<
  typeof knowledgeHitDiagnosticsSchema
>;

export const knowledgeSearchDiagnosticsSchema = z
  .object({
    strategy_version: z.string(),
    lexical_version: z.number().int(),
    target_base_count: z.number().int(),
    effective_top_k: z.number().int(),
    per_base_route_budget: z.number().int(),
    retrieval_mode: knowledgeRetrievalModeSchema,
    counts: z
      .object({
        semantic_candidates: z.number().int(),
        lexical_candidates: z.number().int(),
        parents_deduplicated: z.number().int(),
        threshold_filtered: z.number().int(),
        stale_filtered: z.number().int(),
        returned: z.number().int(),
      })
      .strict(),
    timings: z
      .object({
        query_embedding_ms: z.number(),
        recall_ms: z.number(),
        rerank_ms: z.number(),
        final_validation_ms: z.number(),
      })
      .strict(),
    model_ids: z.array(z.string().uuid()),
    ranking_method: knowledgeScoreKindSchema.nullable(),
    empty_reason: z
      .enum(["not_ready", "no_candidates", "filtered_out", "stale_candidates"])
      .nullable(),
    heterogeneous_without_lexical_evidence: z.boolean(),
    hit_diagnostics: z.array(knowledgeHitDiagnosticsSchema),
  })
  .strict();
export type KnowledgeSearchDiagnostics = z.infer<
  typeof knowledgeSearchDiagnosticsSchema
>;

export const knowledgeSearchResponseSchema = z
  .object({
    citations: z.array(knowledgeSearchCitationSchema),
    diagnostics: knowledgeSearchDiagnosticsSchema.nullable(),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeSearchResponse = z.infer<
  typeof knowledgeSearchResponseSchema
>;

export const knowledgeSegmentChildSchema = z
  .object({
    id: z.string().uuid(),
    position: z.number().int(),
    content: z.string(),
    word_count: z.number().int(),
  })
  .strict();
export type KnowledgeSegmentChild = z.infer<typeof knowledgeSegmentChildSchema>;

export const knowledgeSegmentDetailResponseSchema = z
  .object({
    segment: knowledgeSegmentItemSchema,
    knowledge_base_id: z.string().uuid(),
    document_id: z.string().uuid(),
    document_name: z.string(),
    content_state: z.enum(["current", "stale"]),
    stored_content_version: z.number().int(),
    current_document_version: z.number().int(),
    children_total: z.number().int(),
    child_page: z.number().int(),
    children: z.array(knowledgeSegmentChildSchema),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeSegmentDetailResponse = z.infer<
  typeof knowledgeSegmentDetailResponseSchema
>;

export type CreateKnowledgeBaseInput = {
  name: string;
  embedding_model_id?: string;
  reranker_model_id?: string;
  description?: string;
  /** hybrid adds the lexical recall route; the backend defaults to semantic. */
  retrieval_mode?: KnowledgeRetrievalMode;
};

export type UpdateKnowledgeBaseInput = {
  /** Initial binding only; configured bases change models through rebuild. */
  embedding_model_id?: string;
  name?: string;
  description?: string;
  status?: "active" | "disabled";
  default_top_k?: number;
  default_score_threshold?: number;
  /** Rebind the optional reranker, or clear the binding entirely. */
  reranker_model_id?: string;
  clear_reranker_model?: boolean;
  retrieval_mode?: KnowledgeRetrievalMode;
};

export type UploadKnowledgeDocumentInput = {
  file: File;
  name?: string;
  chunk_size?: number;
  chunk_overlap?: number;
  chunk_separator?: string;
  remove_extra_spaces?: boolean;
  remove_urls_emails?: boolean;
  chunking_mode?: KnowledgeChunkingMode;
  child_chunk_size?: number;
  child_chunk_separator?: string;
};

export const knowledgeChunkPreviewItemSchema = z
  .object({
    position: z.number().int(),
    content: z.string(),
    word_count: z.number().int(),
    child_contents: z.array(z.string()),
  })
  .strict();
export type KnowledgeChunkPreviewItem = z.infer<
  typeof knowledgeChunkPreviewItemSchema
>;

export const knowledgeChunkPreviewResponseSchema = z
  .object({
    items: z.array(knowledgeChunkPreviewItemSchema),
    total: z.number().int(),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeChunkPreviewResponse = z.infer<
  typeof knowledgeChunkPreviewResponseSchema
>;

export type PreviewKnowledgeChunksInput = {
  file: File;
  chunk_size?: number;
  chunk_overlap?: number;
  chunk_separator?: string;
  remove_extra_spaces?: boolean;
  remove_urls_emails?: boolean;
  chunking_mode?: KnowledgeChunkingMode;
  child_chunk_size?: number;
  child_chunk_separator?: string;
};

/**
 * Explicit re-parse of the stored original file. `expected_version` pins the
 * document generation this confirmation was based on; the server rejects a
 * stale confirmation with a conflict instead of silently overwriting.
 */
export type KnowledgeReparseInput = {
  expected_version: number;
  chunk_size: number;
  chunk_overlap: number;
  chunk_separator: string;
  remove_extra_spaces: boolean;
  remove_urls_emails: boolean;
  chunking_mode: KnowledgeChunkingMode;
  child_chunk_size?: number;
  child_chunk_separator?: string;
};

export const knowledgeReparsePreviewResponseSchema = z
  .object({
    document_version: z.number().int(),
    items: z.array(knowledgeChunkPreviewItemSchema),
    total: z.number().int(),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeReparsePreviewResponse = z.infer<
  typeof knowledgeReparsePreviewResponseSchema
>;

/** One bounded common metadata patch for documents of one base. */
export type KnowledgeDocumentsMetadataInput = {
  document_ids: string[];
  values: Record<string, string | number | null>;
};

export const knowledgeMetadataFieldTypeSchema = z.enum([
  "string",
  "number",
  "time",
]);
export type KnowledgeMetadataFieldType = z.infer<
  typeof knowledgeMetadataFieldTypeSchema
>;

export const knowledgeMetadataFieldItemSchema = z
  .object({
    id: z.string().uuid(),
    knowledge_base_id: z.string().uuid(),
    name: z.string(),
    field_type: knowledgeMetadataFieldTypeSchema,
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict();
export type KnowledgeMetadataFieldItem = z.infer<
  typeof knowledgeMetadataFieldItemSchema
>;

export const knowledgeMetadataFieldListResponseSchema = z
  .object({
    items: z.array(knowledgeMetadataFieldItemSchema),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeMetadataFieldListResponse = z.infer<
  typeof knowledgeMetadataFieldListResponseSchema
>;

export const knowledgeMetadataFieldMutationResponseSchema = z
  .object({
    item: knowledgeMetadataFieldItemSchema,
    request_id: z.string(),
  })
  .strict();

export const knowledgeMetadataFieldDeleteResponseSchema = z
  .object({
    request_id: z.string(),
  })
  .strict();

export type CreateKnowledgeMetadataFieldInput = {
  name: string;
  field_type: KnowledgeMetadataFieldType;
};

/** Partial update: null removes the key, string/number sets it. */
export type SetKnowledgeDocumentMetadataInput = {
  values: Record<string, string | number | null>;
};

export type RebuildKnowledgeBaseInput = {
  embedding_model_id: string;
};

export const knowledgeMetadataFilterOperatorSchema = z.enum([
  "eq",
  "contains",
  "gte",
  "lte",
]);
export type KnowledgeMetadataFilterOperator = z.infer<
  typeof knowledgeMetadataFilterOperatorSchema
>;

export type KnowledgeMetadataFilterInput = {
  name: string;
  operator: KnowledgeMetadataFilterOperator;
  value: string | number;
};

export type KnowledgeSearchInput = {
  query: string;
  knowledge_base_ids?: string[];
  top_k?: number;
  score_threshold?: number;
  metadata_filters?: KnowledgeMetadataFilterInput[];
  /** Per-call route override; omit to follow each base's configured mode. */
  retrieval_mode?: KnowledgeRetrievalMode;
  /** Adds the bounded safe diagnostics to this one response. */
  debug?: boolean;
};

export type UpdateKnowledgeSegmentInput = {
  content?: string;
  enabled?: boolean;
};

export const knowledgeQuerySourceSchema = z.enum(["agent", "retrieval_test"]);
export type KnowledgeQuerySource = z.infer<typeof knowledgeQuerySourceSchema>;

export const knowledgeQueryItemSchema = z
  .object({
    id: z.string().uuid(),
    knowledge_base_ids: z.array(z.string().uuid()),
    query: z.string(),
    source: knowledgeQuerySourceSchema,
    result_count: z.number().int(),
    top_score: z.number().nullable(),
    created_at: z.string(),
  })
  .strict();
export type KnowledgeQueryItem = z.infer<typeof knowledgeQueryItemSchema>;

export const knowledgeQueryListResponseSchema = z
  .object({
    items: z.array(knowledgeQueryItemSchema),
    total: z.number().int(),
    page: z.number().int(),
    page_size: z.number().int(),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeQueryListResponse = z.infer<
  typeof knowledgeQueryListResponseSchema
>;
