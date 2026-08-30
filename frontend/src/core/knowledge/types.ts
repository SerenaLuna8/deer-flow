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

export const knowledgeBaseItemSchema = z
  .object({
    id: z.string().uuid(),
    project_id: z.string().uuid(),
    name: z.string(),
    description: z.string(),
    embedding_model_id: z.string().uuid(),
    reranker_model_id: z.string().uuid().nullable(),
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

/** Values keyed by the base's metadata-field name (string or number). */
export const knowledgeDocumentMetadataSchema = z.record(
  z.union([z.string(), z.number()]),
);
export type KnowledgeDocumentMetadata = z.infer<
  typeof knowledgeDocumentMetadataSchema
>;

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
  })
  .strict();
export type KnowledgeSearchCitation = z.infer<
  typeof knowledgeSearchCitationSchema
>;

export const knowledgeSearchResponseSchema = z
  .object({
    citations: z.array(knowledgeSearchCitationSchema),
    request_id: z.string(),
  })
  .strict();
export type KnowledgeSearchResponse = z.infer<
  typeof knowledgeSearchResponseSchema
>;

export type CreateKnowledgeBaseInput = {
  name: string;
  embedding_model_id: string;
  reranker_model_id?: string;
  description?: string;
};

export type UpdateKnowledgeBaseInput = {
  name?: string;
  description?: string;
  status?: "active" | "disabled";
  default_top_k?: number;
  default_score_threshold?: number;
  /** Rebind the optional reranker, or clear the binding entirely. */
  reranker_model_id?: string;
  clear_reranker_model?: boolean;
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
