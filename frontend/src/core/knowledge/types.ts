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

const knowledgeSha256Schema = z.string().regex(/^[0-9a-f]{64}$/u);

const knowledgeSourcePositionValueSchema = z.union([
  z.string(),
  z.number().int(),
]);

const KNOWLEDGE_SOURCE_POSITION_KEYS = new Set([
  "page",
  "paragraph",
  "table",
  "row",
  "row_end",
  "column",
  "sheet",
  "slide",
  "chapter",
  "line",
  "line_end",
  "element",
  "image_index",
  "table_path",
  "encoding",
]);
const KNOWLEDGE_NUMERIC_SOURCE_POSITION_KEYS = new Set(
  [...KNOWLEDGE_SOURCE_POSITION_KEYS].filter(
    (key) => !["sheet", "table_path", "encoding"].includes(key),
  ),
);

export const knowledgeSourcePositionSchema = z
  .record(knowledgeSourcePositionValueSchema)
  .superRefine((position, context) => {
    for (const [key, value] of Object.entries(position)) {
      if (!KNOWLEDGE_SOURCE_POSITION_KEYS.has(key)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "source position contains an unsafe key",
          path: [key],
        });
      } else if (
        KNOWLEDGE_NUMERIC_SOURCE_POSITION_KEYS.has(key) &&
        (typeof value !== "number" || value < 1)
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "numeric source positions start at one",
          path: [key],
        });
      } else if (
        key === "table_path" &&
        (typeof value !== "string" ||
          !/^[1-9][0-9]*(?:\.[1-9][0-9]*)*$/u.test(value))
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "table path must be a numeric hierarchy",
          path: [key],
        });
      }
    }
  });

export const knowledgeSourceSpanSchema = z
  .object({
    block_id: z.string().min(1),
    start: z.number().int().nonnegative(),
    end: z.number().int().nonnegative(),
    location: knowledgeSourcePositionSchema,
    role: z.enum(["source", "context_prefix"]),
  })
  .strict()
  .superRefine((span, context) => {
    if (span.end < span.start) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "source span end must not precede start",
        path: ["end"],
      });
    }
  });

export const knowledgeParseWarningSchema = z
  .object({
    code: z.string().min(1),
    message: z.string().min(1),
    source_position: knowledgeSourcePositionSchema,
  })
  .strict();

export const knowledgeHeaderRuleSchema = z
  .object({
    sheet: z.string().nullable(),
    mode: z.enum(["auto", "none", "explicit"]),
    row: z.number().int().positive().nullable(),
  })
  .strict()
  .superRefine((rule, context) => {
    if ((rule.mode === "explicit") !== (rule.row !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "explicit header requires a row",
        path: ["row"],
      });
    }
  });
export type KnowledgeHeaderRule = z.infer<typeof knowledgeHeaderRuleSchema>;

export const knowledgeProcessingProfileSchema = z
  .object({
    parse: z
      .object({
        etl_type: z.enum(["dify", "unstructured_local"]),
        extractor_id: z.string().min(1),
        extractor_version: z.string().min(1),
        normalization_version: z.string().min(1),
        image_policy_version: z.string().min(1),
        header_rules: z.array(knowledgeHeaderRuleSchema),
      })
      .strict(),
    chunk: z
      .object({
        unit: z.enum(["character", "token"]),
        mode: knowledgeChunkingModeSchema,
        size: z.number().int().positive(),
        overlap: z.number().int().nonnegative(),
        separator: z.string(),
        child_size: z.number().int().positive(),
        child_separator: z.string(),
        remove_extra_spaces: z.boolean(),
        remove_urls_emails: z.boolean(),
        tokenizer_profile_id: z.string().nullable(),
        tokenizer_digest: knowledgeSha256Schema.nullable(),
        cleaner_version: z.string().min(1),
        splitter_version: z.string().min(1),
      })
      .strict()
      .superRefine((chunk, context) => {
        if (chunk.overlap >= chunk.size) {
          context.addIssue({
            code: z.ZodIssueCode.custom,
            message: "chunk overlap must be smaller than size",
            path: ["overlap"],
          });
        }
      }),
  })
  .strict();
export type KnowledgeProcessingProfile = z.infer<
  typeof knowledgeProcessingProfileSchema
>;

export const knowledgeFileCapabilitiesSchema = z
  .object({
    effective_etl: z.enum(["dify", "unstructured_local"]),
    capability_revision: knowledgeSha256Schema,
    formats: z.array(
      z
        .object({
          extension: z.string().regex(/^\.[a-z0-9]+$/u),
          parser_id: z.string().min(1),
          available: z.boolean(),
          reason_code: z.string().min(1).nullable(),
          embedded_images: z.boolean(),
        })
        .strict(),
    ),
    chunk_limits: z
      .object({
        unit: z.literal("token"),
        tokenizer_profile_id: z.string().min(1),
        parent_min: z.number().int().positive(),
        parent_max: z.number().int().positive(),
        parent_max_chars: z.number().int().positive(),
        overlap_max: z.number().int().nonnegative(),
        child_min: z.number().int().positive(),
        child_max: z.number().int().positive(),
      })
      .strict(),
  })
  .strict();
export type KnowledgeFileCapabilities = z.infer<
  typeof knowledgeFileCapabilitiesSchema
>;

export const knowledgeProcessingParametersSchema = z
  .object({
    unit: z.enum(["character", "token"]),
    mode: knowledgeChunkingModeSchema,
    size: z.number().int().min(200).max(4000),
    overlap: z.number().int().min(0).max(500),
    separator: z.string().min(1).max(64),
    child_size: z.number().int().min(100).max(2000),
    child_separator: z.string().min(1).max(64),
    remove_extra_spaces: z.boolean(),
    remove_urls_emails: z.boolean(),
    header_rules: z.array(knowledgeHeaderRuleSchema),
  })
  .strict()
  .superRefine((parameters, context) => {
    if (parameters.overlap >= parameters.size) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "chunk overlap must be smaller than size",
        path: ["overlap"],
      });
    }
    if (
      parameters.mode === "parent_child" &&
      parameters.child_size >= parameters.size
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "child size must be smaller than parent size",
        path: ["child_size"],
      });
    }
    const sheets = new Set<string | null>();
    for (const [index, rule] of parameters.header_rules.entries()) {
      if (sheets.has(rule.sheet)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "duplicate sheet header rule",
          path: ["header_rules", index, "sheet"],
        });
      }
      sheets.add(rule.sheet);
    }
  });
export type KnowledgeProcessingParameters = z.infer<
  typeof knowledgeProcessingParametersSchema
>;

const knowledgeSegmentSourcePositionSchema = z.union([
  knowledgeSourcePositionSchema,
  z.object({ manual: z.literal(true) }).strict(),
]);

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
    summary_index_enabled: z.boolean(),
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

export const knowledgeBaseUpdateResponseSchema =
  knowledgeBaseMutationResponseSchema.extend({
    summary_backfill: z
      .object({
        accepted_document_count: z.number().int().nonnegative(),
        skipped_document_ids: z.array(z.string().uuid()),
      })
      .strict()
      .nullable(),
  });
export type KnowledgeBaseUpdateResponse = z.infer<
  typeof knowledgeBaseUpdateResponseSchema
>;

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
  "summarizing",
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
    kind: z.enum(["ingest_document", "reembed_document", "summarize_document"]),
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
    parsing_profile: knowledgeProcessingProfileSchema.nullable(),
    parse_warnings: z.array(knowledgeParseWarningSchema),
    chunk_size_unit: z.enum(["character", "token"]),
    tokenizer_profile_id: z.string().nullable(),
    content_initialized: z.boolean(),
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

export const knowledgeDocumentAttachmentItemSchema = z
  .object({
    attachment_id: z.string().uuid(),
    ref: knowledgeSha256Schema,
    media_type: z.enum(["image/png", "image/jpeg", "image/webp"]),
    width: z.number().int().positive(),
    height: z.number().int().positive(),
  })
  .strict();
export type KnowledgeDocumentAttachmentItem = z.infer<
  typeof knowledgeDocumentAttachmentItemSchema
>;

export const knowledgeDocumentAttachmentListResponseSchema = z
  .object({
    items: z.array(knowledgeDocumentAttachmentItemSchema),
    document_version: z.number().int().positive(),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeDocumentAttachmentListResponse = z.infer<
  typeof knowledgeDocumentAttachmentListResponseSchema
>;

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

export const knowledgeSummaryModelSchema = z
  .object({
    model_name: z.string(),
    display_name: z.string(),
  })
  .strict();

export const knowledgeModelOptionsResponseSchema = z
  .object({
    embedding_models: z.array(knowledgeModelOptionSchema),
    reranker_models: z.array(knowledgeModelOptionSchema),
    summary_model: knowledgeSummaryModelSchema.nullable(),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeModelOptions = {
  embedding_models: KnowledgeModelOption[];
  reranker_models: KnowledgeModelOption[];
  summary_model: z.infer<typeof knowledgeSummaryModelSchema> | null;
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
    source_position: knowledgeSegmentSourcePositionSchema,
    created_at: z.string(),
    token_count: z.number().int().nonnegative(),
    source_spans: z.array(knowledgeSourceSpanSchema),
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
    matched_via: z.enum(["segment", "child", "summary"]),
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
        summary_candidates: z.number().int(),
        query_embedding_cache_hits: z.number().int(),
        query_embedding_cache_misses: z.number().int(),
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

export const knowledgeSegmentSummarySchema = z
  .object({
    content: z.string(),
    created_at: z.string(),
  })
  .strict();
export type KnowledgeSegmentSummary = z.infer<
  typeof knowledgeSegmentSummarySchema
>;

export const knowledgeSegmentAttachmentSchema = z
  .object({
    attachment_id: z.string().uuid(),
    ref: knowledgeSha256Schema,
    alt_text: z.string(),
    media_type: z.enum(["image/png", "image/jpeg", "image/webp"]),
    width: z.number().int().positive(),
    height: z.number().int().positive(),
  })
  .strict();
export type KnowledgeSegmentAttachment = z.infer<
  typeof knowledgeSegmentAttachmentSchema
>;

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
    attachments: z.array(knowledgeSegmentAttachmentSchema),
    summary: knowledgeSegmentSummarySchema.nullable(),
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
  summary_index_enabled?: boolean;
};

export type UploadKnowledgeDocumentInput = {
  file: File;
  name?: string;
  processing_profile: KnowledgeProcessingParameters;
  expected_preview_fingerprint?: string;
};

export const knowledgeChunkPreviewItemSchema = z
  .object({
    position: z.number().int().nonnegative(),
    content: z.string(),
    word_count: z.number().int().nonnegative(),
    child_contents: z.array(z.string()),
    token_count: z.number().int().nonnegative(),
    source_spans: z.array(knowledgeSourceSpanSchema),
    attachments: z.array(
      z
        .object({
          ref: knowledgeSha256Schema,
          alt_text: z.string(),
        })
        .strict(),
    ),
  })
  .strict();
export type KnowledgeChunkPreviewItem = z.infer<
  typeof knowledgeChunkPreviewItemSchema
>;

export const knowledgePreviewAttachmentSchema = z
  .object({
    ref: knowledgeSha256Schema,
    media_type: z.enum(["image/png", "image/jpeg", "image/webp"]),
    data_base64: z
      .string()
      .min(4)
      .max(4 * Math.ceil((128 * 1024) / 3))
      .superRefine((value, context) => {
        const size = decodedBase64Size(value);
        if (size === null || size > 128 * 1024) {
          context.addIssue({
            code: z.ZodIssueCode.custom,
            message: "invalid or oversized preview attachment",
          });
        }
      }),
  })
  .strict();

export const knowledgePreviewAttachmentsSchema = z
  .array(knowledgePreviewAttachmentSchema)
  .max(20)
  .superRefine((attachments, context) => {
    const total = attachments.reduce((bytes, attachment) => {
      return bytes + (decodedBase64Size(attachment.data_base64) ?? 0);
    }, 0);
    if (total > 2 * 1024 * 1024) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "preview attachment response exceeds its byte budget",
      });
    }
  });

const knowledgePreviewResponseShape = {
  items: z.array(knowledgeChunkPreviewItemSchema).max(10),
  total: z.number().int().nonnegative(),
  preview_fingerprint: knowledgeSha256Schema,
  source_sha256: knowledgeSha256Schema,
  effective_profile: knowledgeProcessingProfileSchema,
  warnings: z.array(knowledgeParseWarningSchema),
  preview_attachments: knowledgePreviewAttachmentsSchema,
  omitted_preview_attachment_count: z.number().int().nonnegative(),
  table_sources: z.array(
    z
      .object({
        sheet: z.string().nullable(),
        header_mode: z.enum(["auto", "none", "explicit"]),
        header_row: z.number().int().positive().nullable(),
        header_cells: z.array(z.string()),
      })
      .strict(),
  ),
  request_id: z.string(),
};

function decodedBase64Size(value: string): number | null {
  if (
    value.length % 4 !== 0 ||
    !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/u.test(
      value,
    )
  ) {
    return null;
  }
  const padding = value.endsWith("==") ? 2 : value.endsWith("=") ? 1 : 0;
  return (value.length / 4) * 3 - padding;
}

export const knowledgeChunkPreviewResponseSchema = z
  .object(knowledgePreviewResponseShape)
  .strict();
export type KnowledgeChunkPreviewResponse = z.infer<
  typeof knowledgeChunkPreviewResponseSchema
>;

export type PreviewKnowledgeChunksInput = {
  file: File;
  processing_profile: KnowledgeProcessingParameters;
};

type KnowledgeAttachmentReadCommon = {
  projectId: string;
  documentId: string;
  segmentId: string;
  attachmentId: string;
  expectedDocumentVersion: number;
  expectedContentDigest: string;
};

/**
 * Published attachment reads keep maintenance and citation authority on their
 * two distinct Gateway paths; neither branch accepts a storage locator.
 */
export type KnowledgeAttachmentRead = KnowledgeAttachmentReadCommon &
  (
    | { purpose: "management"; baseId?: never }
    | { purpose: "citation"; baseId: string }
  );

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
    ...knowledgePreviewResponseShape,
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
