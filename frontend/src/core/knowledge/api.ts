import type { z } from "zod";

import { AuthRequiredError, fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  knowledgeBaseListResponseSchema,
  knowledgeBaseMutationResponseSchema,
  knowledgeBaseRebuildResponseSchema,
  knowledgeBaseUpdateResponseSchema,
  knowledgeChunkPreviewResponseSchema,
  knowledgeDocumentBatchResponseSchema,
  knowledgeDocumentListResponseSchema,
  knowledgeDocumentMutationResponseSchema,
  knowledgeHealthResponseSchema,
  knowledgeMetadataFieldDeleteResponseSchema,
  knowledgeMetadataFieldListResponseSchema,
  knowledgeMetadataFieldMutationResponseSchema,
  knowledgeModelOptionsResponseSchema,
  knowledgeQueryListResponseSchema,
  knowledgeReparsePreviewResponseSchema,
  knowledgeSearchResponseSchema,
  knowledgeSegmentDetailResponseSchema,
  knowledgeSegmentListResponseSchema,
  knowledgeSegmentMutationResponseSchema,
  type CreateKnowledgeBaseInput,
  type CreateKnowledgeMetadataFieldInput,
  type KnowledgeBaseItem,
  type KnowledgeBaseListResponse,
  type KnowledgeBaseRebuildResponse,
  type KnowledgeChunkingMode,
  type KnowledgeChunkPreviewResponse,
  type KnowledgeDocumentItem,
  type KnowledgeDocumentListResponse,
  type KnowledgeDocumentsMetadataInput,
  type KnowledgeHealthResponse,
  type KnowledgeMetadataFieldItem,
  type KnowledgeModelOptions,
  type KnowledgeQueryListResponse,
  type KnowledgeReparseInput,
  type KnowledgeReparsePreviewResponse,
  type KnowledgeSearchInput,
  type KnowledgeSearchResponse,
  type KnowledgeSegmentDetailResponse,
  type KnowledgeSegmentItem,
  type KnowledgeSegmentListResponse,
  type PreviewKnowledgeChunksInput,
  type SetKnowledgeDocumentMetadataInput,
  type UpdateKnowledgeBaseInput,
  type KnowledgeBaseUpdateResponse,
  type UpdateKnowledgeSegmentInput,
  type UploadKnowledgeDocumentInput,
} from "./types";

export class KnowledgeApiError extends Error {
  readonly status: number;
  readonly code:
    | "AUTH_REQUIRED"
    | "NETWORK_ERROR"
    | "REQUEST_FAILED"
    | "INVALID_RESPONSE"
    | "INCOMPLETE_LIST";
  /** Backend `KNOWLEDGE_*` error code from the error envelope, if present. */
  readonly knowledgeCode: string | null;
  /** Backend-provided user-facing message, if present. */
  readonly serverMessage: string | null;

  constructor(
    status: number,
    code: KnowledgeApiError["code"],
    message: string,
    options: {
      knowledgeCode?: string | null;
      serverMessage?: string | null;
    } = {},
  ) {
    super(message);
    this.name = "KnowledgeApiError";
    this.status = status;
    this.code = code;
    this.knowledgeCode = options.knowledgeCode ?? null;
    this.serverMessage = options.serverMessage ?? null;
  }
}

/**
 * True when the backend reported a segment version race
 * (`KNOWLEDGE_CONFLICT`): the served list is stale and the mutation caller
 * must refresh authoritative state for the retry the message asks for.
 */
export function isKnowledgeConflictError(error: unknown): boolean {
  return (
    error instanceof KnowledgeApiError &&
    error.knowledgeCode === "KNOWLEDGE_CONFLICT"
  );
}

/**
 * Cached project data is no longer readable after authentication, capability,
 * or object-scope authority disappears. Recoverable network/5xx failures do
 * not cross this boundary and may continue showing the last safe response.
 */
export function isKnowledgeAuthorityBoundaryError(error: unknown): boolean {
  return (
    error instanceof KnowledgeApiError &&
    (error.status === 401 ||
      error.status === 403 ||
      error.status === 404 ||
      error.knowledgeCode === "KNOWLEDGE_NOT_FOUND")
  );
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

function knowledgeBaseURL(projectId: string): string {
  return `${getBackendBaseURL()}/api/projects/${encodeURIComponent(projectId)}/knowledge`;
}

async function requestKnowledge(
  input: string,
  init?: RequestInit,
): Promise<Response> {
  try {
    return await fetchWithAuth(input, init);
  } catch (error) {
    if (isAbortError(error) || error instanceof KnowledgeApiError) {
      throw error;
    }
    if (error instanceof AuthRequiredError) {
      throw new KnowledgeApiError(
        401,
        "AUTH_REQUIRED",
        "Authentication required",
      );
    }
    throw new KnowledgeApiError(
      0,
      "NETWORK_ERROR",
      "Knowledge is temporarily unavailable.",
    );
  }
}

function readErrorEnvelope(body: unknown): {
  knowledgeCode: string | null;
  serverMessage: string | null;
} {
  if (typeof body !== "object" || body === null) {
    return { knowledgeCode: null, serverMessage: null };
  }
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail !== "object" || detail === null) {
    return { knowledgeCode: null, serverMessage: null };
  }
  const code = (detail as { code?: unknown }).code;
  const message = (detail as { message?: unknown }).message;
  return {
    knowledgeCode: typeof code === "string" ? code : null,
    serverMessage: typeof message === "string" ? message : null,
  };
}

async function readKnowledgeResponse<T>(
  response: Response,
  schema: z.ZodType<T, z.ZodTypeDef, unknown>,
): Promise<T> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new KnowledgeApiError(
      response.status,
      "INVALID_RESPONSE",
      "Knowledge response was invalid.",
    );
  }
  if (!response.ok) {
    const envelope = readErrorEnvelope(body);
    throw new KnowledgeApiError(
      response.status,
      "REQUEST_FAILED",
      envelope.serverMessage ?? "Knowledge request failed.",
      envelope,
    );
  }
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new KnowledgeApiError(
      response.status,
      "INVALID_RESPONSE",
      "Knowledge response was invalid.",
    );
  }
  return parsed.data;
}

function jsonRequestInit(
  method: "POST" | "PATCH" | "DELETE",
  body?: unknown,
  signal?: AbortSignal,
): RequestInit {
  return {
    method,
    ...(body === undefined
      ? {}
      : {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
    ...(signal ? { signal } : {}),
  };
}

const LIST_PAGE_SIZE = 100;
/** Hard stop well beyond the backend quotas (20 bases, 500 documents). */
const LIST_MAX_PAGES = 20;

/**
 * Lists page to completion: the backend allows more rows than one page
 * (up to 500 documents per base), and a silently truncated list would make
 * the overflow rows invisible and unmanageable.
 *
 * Offset pages must agree on their total and contain distinct identities.
 * A concurrent insertion/deletion can otherwise make the counts look complete
 * while skipping rows. A changed total, duplicate, premature empty page or
 * the local page cap without reaching the exact total raises an
 * explicit `INCOMPLETE_LIST` error instead of publishing a partial list —
 * callers filter and paginate over this result as if it were everything,
 * and a silent partial would quietly hide the missing rows.
 */
async function listAllPages<
  Item extends { id: string },
  R extends { items: Item[]; total: number },
>(
  pageURL: (page: number) => string,
  schema: z.ZodType<R, z.ZodTypeDef, unknown>,
  signal?: AbortSignal,
): Promise<R> {
  const items: Item[] = [];
  const seenIds = new Set<string>();
  let expectedTotal: number | undefined;
  let page = 1;
  for (;;) {
    const response = await requestKnowledge(
      pageURL(page),
      signal ? { signal } : undefined,
    );
    const parsed = await readKnowledgeResponse(response, schema);
    expectedTotal ??= parsed.total;
    if (
      parsed.total !== expectedTotal ||
      items.length + parsed.items.length > expectedTotal
    ) {
      throw new KnowledgeApiError(
        response.status,
        "INCOMPLETE_LIST",
        "Knowledge list changed while loading. Retry to load it again.",
      );
    }
    for (const item of parsed.items) {
      if (seenIds.has(item.id)) {
        throw new KnowledgeApiError(
          response.status,
          "INCOMPLETE_LIST",
          "Knowledge list changed while loading. Retry to load it again.",
        );
      }
      seenIds.add(item.id);
    }
    items.push(...parsed.items);
    if (items.length === expectedTotal) {
      return { ...parsed, items, page: 1 };
    }
    if (parsed.items.length === 0 || page >= LIST_MAX_PAGES) {
      throw new KnowledgeApiError(
        response.status,
        "INCOMPLETE_LIST",
        "Knowledge list is incomplete.",
      );
    }
    page += 1;
  }
}

export async function listKnowledgeBases(
  projectId: string,
  signal?: AbortSignal,
): Promise<KnowledgeBaseListResponse> {
  return listAllPages(
    (page) =>
      `${knowledgeBaseURL(projectId)}/bases?page=${page}&page_size=${LIST_PAGE_SIZE}`,
    knowledgeBaseListResponseSchema,
    signal,
  );
}

export async function createKnowledgeBase(
  projectId: string,
  input: CreateKnowledgeBaseInput,
  signal?: AbortSignal,
): Promise<KnowledgeBaseItem> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/bases`,
    jsonRequestInit("POST", input, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeBaseMutationResponseSchema,
  );
  return parsed.item;
}

export async function updateKnowledgeBase(
  projectId: string,
  baseId: string,
  input: UpdateKnowledgeBaseInput,
  signal?: AbortSignal,
): Promise<KnowledgeBaseUpdateResponse> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/bases/${encodeURIComponent(baseId)}`,
    jsonRequestInit("PATCH", input, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeBaseUpdateResponseSchema,
  );
  return parsed;
}

export async function deleteKnowledgeBase(
  projectId: string,
  baseId: string,
  signal?: AbortSignal,
): Promise<KnowledgeBaseItem> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/bases/${encodeURIComponent(baseId)}`,
    jsonRequestInit("DELETE", undefined, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeBaseMutationResponseSchema,
  );
  return parsed.item;
}

export async function listKnowledgeDocuments(
  projectId: string,
  baseId: string,
  signal?: AbortSignal,
): Promise<KnowledgeDocumentListResponse> {
  return listAllPages(
    (page) =>
      `${knowledgeBaseURL(projectId)}/bases/${encodeURIComponent(baseId)}/documents?page=${page}&page_size=${LIST_PAGE_SIZE}`,
    knowledgeDocumentListResponseSchema,
    signal,
  );
}

function chunkSettingsFormData(input: {
  file: File;
  chunk_size?: number;
  chunk_overlap?: number;
  chunk_separator?: string;
  remove_extra_spaces?: boolean;
  remove_urls_emails?: boolean;
  chunking_mode?: KnowledgeChunkingMode;
  child_chunk_size?: number;
  child_chunk_separator?: string;
}): FormData {
  const formData = new FormData();
  formData.append("file", input.file);
  if (input.chunk_size !== undefined) {
    formData.append("chunk_size", String(input.chunk_size));
  }
  if (input.chunk_overlap !== undefined) {
    formData.append("chunk_overlap", String(input.chunk_overlap));
  }
  if (input.chunk_separator !== undefined && input.chunk_separator.length > 0) {
    formData.append("chunk_separator", input.chunk_separator);
  }
  if (input.remove_extra_spaces !== undefined) {
    formData.append("remove_extra_spaces", String(input.remove_extra_spaces));
  }
  if (input.remove_urls_emails !== undefined) {
    formData.append("remove_urls_emails", String(input.remove_urls_emails));
  }
  if (input.chunking_mode !== undefined) {
    formData.append("chunking_mode", input.chunking_mode);
  }
  if (input.child_chunk_size !== undefined) {
    formData.append("child_chunk_size", String(input.child_chunk_size));
  }
  if (
    input.child_chunk_separator !== undefined &&
    input.child_chunk_separator.length > 0
  ) {
    formData.append("child_chunk_separator", input.child_chunk_separator);
  }
  return formData;
}

export async function uploadKnowledgeDocument(
  projectId: string,
  baseId: string,
  input: UploadKnowledgeDocumentInput,
  signal?: AbortSignal,
): Promise<KnowledgeDocumentItem> {
  const formData = chunkSettingsFormData(input);
  if (input.name !== undefined && input.name.trim().length > 0) {
    formData.append("name", input.name.trim());
  }
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/bases/${encodeURIComponent(baseId)}/documents`,
    { method: "POST", body: formData, ...(signal ? { signal } : {}) },
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeDocumentMutationResponseSchema,
  );
  return parsed.item;
}

/** Synchronous extract → clean → split preview; nothing is stored or queued. */
export async function previewKnowledgeChunks(
  projectId: string,
  input: PreviewKnowledgeChunksInput,
  signal?: AbortSignal,
): Promise<KnowledgeChunkPreviewResponse> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/chunk-preview`,
    {
      method: "POST",
      body: chunkSettingsFormData(input),
      ...(signal ? { signal } : {}),
    },
  );
  return readKnowledgeResponse(response, knowledgeChunkPreviewResponseSchema);
}

export async function deleteKnowledgeDocument(
  projectId: string,
  documentId: string,
  signal?: AbortSignal,
): Promise<KnowledgeDocumentItem> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/documents/${encodeURIComponent(documentId)}`,
    jsonRequestInit("DELETE", undefined, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeDocumentMutationResponseSchema,
  );
  return parsed.item;
}

export async function retryKnowledgeDocument(
  projectId: string,
  documentId: string,
  signal?: AbortSignal,
): Promise<KnowledgeDocumentItem> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/documents/${encodeURIComponent(documentId)}/retry`,
    jsonRequestInit("POST", undefined, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeDocumentMutationResponseSchema,
  );
  return parsed.item;
}

export async function renameKnowledgeDocument(
  projectId: string,
  documentId: string,
  name: string,
  signal?: AbortSignal,
): Promise<KnowledgeDocumentItem> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/documents/${encodeURIComponent(documentId)}`,
    jsonRequestInit("PATCH", { name }, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeDocumentMutationResponseSchema,
  );
  return parsed.item;
}

export async function setKnowledgeDocumentsEnabled(
  projectId: string,
  documentIds: string[],
  enabled: boolean,
  signal?: AbortSignal,
): Promise<KnowledgeDocumentItem[]> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/documents/batch-status`,
    jsonRequestInit("POST", { document_ids: documentIds, enabled }, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeDocumentBatchResponseSchema,
  );
  return parsed.items;
}

export async function deleteKnowledgeDocuments(
  projectId: string,
  documentIds: string[],
  signal?: AbortSignal,
): Promise<KnowledgeDocumentItem[]> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/documents/batch-delete`,
    jsonRequestInit("POST", { document_ids: documentIds }, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeDocumentBatchResponseSchema,
  );
  return parsed.items;
}

export async function createKnowledgeSegment(
  projectId: string,
  documentId: string,
  content: string,
  signal?: AbortSignal,
): Promise<KnowledgeSegmentItem> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/documents/${encodeURIComponent(documentId)}/segments`,
    jsonRequestInit("POST", { content }, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeSegmentMutationResponseSchema,
  );
  return parsed.item;
}

export async function updateKnowledgeSegment(
  projectId: string,
  segmentId: string,
  input: UpdateKnowledgeSegmentInput,
  signal?: AbortSignal,
): Promise<KnowledgeSegmentItem> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/segments/${encodeURIComponent(segmentId)}`,
    jsonRequestInit("PATCH", input, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeSegmentMutationResponseSchema,
  );
  return parsed.item;
}

/** Deletes one segment; resolves to the updated parent document. */
export async function deleteKnowledgeSegment(
  projectId: string,
  segmentId: string,
  signal?: AbortSignal,
): Promise<KnowledgeDocumentItem> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/segments/${encodeURIComponent(segmentId)}`,
    jsonRequestInit("DELETE", undefined, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeDocumentMutationResponseSchema,
  );
  return parsed.item;
}

export function knowledgeDocumentDownloadURL(
  projectId: string,
  documentId: string,
): string {
  return `${knowledgeBaseURL(projectId)}/documents/${encodeURIComponent(documentId)}/download`;
}

export async function listKnowledgeDocumentSegments(
  projectId: string,
  documentId: string,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<KnowledgeSegmentListResponse> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/documents/${encodeURIComponent(documentId)}/segments?page=${page}&page_size=${pageSize}`,
    signal ? { signal } : undefined,
  );
  return readKnowledgeResponse(response, knowledgeSegmentListResponseSchema);
}

/**
 * Feature probe: the health route answers 404 `KNOWLEDGE_DISABLED` when the
 * host runs without a knowledge module, which is how navigation decides
 * whether the Knowledge entry exists at all.
 */
export async function fetchKnowledgeHealth(
  projectId: string,
  signal?: AbortSignal,
): Promise<KnowledgeHealthResponse> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/health`,
    signal ? { signal } : undefined,
  );
  return readKnowledgeResponse(response, knowledgeHealthResponseSchema);
}

export async function listKnowledgeModelOptions(
  projectId: string,
  signal?: AbortSignal,
): Promise<KnowledgeModelOptions> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/model-options`,
    signal ? { signal } : undefined,
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeModelOptionsResponseSchema,
  );
  return {
    embedding_models: parsed.embedding_models,
    reranker_models: parsed.reranker_models,
    summary_model: parsed.summary_model,
  };
}

/** Recent retrieval queries that targeted this base, newest first. */
export async function listKnowledgeBaseQueries(
  projectId: string,
  baseId: string,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<KnowledgeQueryListResponse> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/bases/${encodeURIComponent(baseId)}/queries?page=${page}&page_size=${pageSize}`,
    signal ? { signal } : undefined,
  );
  return readKnowledgeResponse(response, knowledgeQueryListResponseSchema);
}

export async function searchKnowledge(
  projectId: string,
  input: KnowledgeSearchInput,
  signal?: AbortSignal,
): Promise<KnowledgeSearchResponse> {
  const body: Record<string, unknown> = { query: input.query };
  if (input.knowledge_base_ids !== undefined) {
    body.knowledge_base_ids = input.knowledge_base_ids;
  }
  if (input.top_k !== undefined) body.top_k = input.top_k;
  if (input.score_threshold !== undefined) {
    body.score_threshold = input.score_threshold;
  }
  if (
    input.metadata_filters !== undefined &&
    input.metadata_filters.length > 0
  ) {
    body.metadata_filters = input.metadata_filters;
  }
  if (input.retrieval_mode !== undefined) {
    body.retrieval_mode = input.retrieval_mode;
  }
  if (input.debug === true) body.debug = true;
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/search`,
    jsonRequestInit("POST", body, signal),
  );
  return readKnowledgeResponse(response, knowledgeSearchResponseSchema);
}

export async function getKnowledgeSegmentDetail(
  projectId: string,
  baseId: string,
  documentId: string,
  segmentId: string,
  options?: {
    expectedDocumentVersion?: number;
    expectedContentDigest?: string;
    childPage?: number;
  },
  signal?: AbortSignal,
): Promise<KnowledgeSegmentDetailResponse> {
  const params = new URLSearchParams();
  if (options?.expectedDocumentVersion !== undefined) {
    params.set(
      "expected_document_version",
      String(options.expectedDocumentVersion),
    );
  }
  if (options?.expectedContentDigest !== undefined) {
    params.set("expected_content_digest", options.expectedContentDigest);
  }
  if (options?.childPage !== undefined) {
    params.set("child_page", String(options.childPage));
  }
  const query = params.size > 0 ? `?${params.toString()}` : "";
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/bases/${encodeURIComponent(baseId)}/documents/${encodeURIComponent(documentId)}/segments/${encodeURIComponent(segmentId)}${query}`,
    signal ? { signal } : undefined,
  );
  return readKnowledgeResponse(response, knowledgeSegmentDetailResponseSchema);
}

/** Rebinds the base's embedding model and re-embeds the current content. */
export async function rebuildKnowledgeBase(
  projectId: string,
  baseId: string,
  embeddingModelId: string,
  signal?: AbortSignal,
): Promise<KnowledgeBaseRebuildResponse> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/bases/${encodeURIComponent(baseId)}/rebuild`,
    jsonRequestInit("POST", { embedding_model_id: embeddingModelId }, signal),
  );
  return readKnowledgeResponse(response, knowledgeBaseRebuildResponseSchema);
}

export async function listKnowledgeMetadataFields(
  projectId: string,
  baseId: string,
  signal?: AbortSignal,
): Promise<KnowledgeMetadataFieldItem[]> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/bases/${encodeURIComponent(baseId)}/metadata-fields`,
    signal ? { signal } : undefined,
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeMetadataFieldListResponseSchema,
  );
  return parsed.items;
}

export async function createKnowledgeMetadataField(
  projectId: string,
  baseId: string,
  input: CreateKnowledgeMetadataFieldInput,
  signal?: AbortSignal,
): Promise<KnowledgeMetadataFieldItem> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/bases/${encodeURIComponent(baseId)}/metadata-fields`,
    jsonRequestInit("POST", input, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeMetadataFieldMutationResponseSchema,
  );
  return parsed.item;
}

/** Renames the field; document metadata keys follow on the backend. */
export async function renameKnowledgeMetadataField(
  projectId: string,
  fieldId: string,
  name: string,
  signal?: AbortSignal,
): Promise<KnowledgeMetadataFieldItem> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/metadata-fields/${encodeURIComponent(fieldId)}`,
    jsonRequestInit("PATCH", { name }, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeMetadataFieldMutationResponseSchema,
  );
  return parsed.item;
}

/** Deletes the field and strips its key from the base's documents. */
export async function deleteKnowledgeMetadataField(
  projectId: string,
  fieldId: string,
  signal?: AbortSignal,
): Promise<void> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/metadata-fields/${encodeURIComponent(fieldId)}`,
    jsonRequestInit("DELETE", undefined, signal),
  );
  await readKnowledgeResponse(
    response,
    knowledgeMetadataFieldDeleteResponseSchema,
  );
}

/** Partial metadata update on one document: null removes a key. */
export async function setKnowledgeDocumentMetadata(
  projectId: string,
  documentId: string,
  input: SetKnowledgeDocumentMetadataInput,
  signal?: AbortSignal,
): Promise<KnowledgeDocumentItem> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/documents/${encodeURIComponent(documentId)}/metadata`,
    jsonRequestInit("PATCH", input, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeDocumentMutationResponseSchema,
  );
  return parsed.item;
}

/**
 * One common metadata patch across documents of one base, all-or-nothing:
 * untouched keys stay, null clears a key, and any rejected document rolls
 * back the whole batch server-side.
 */
export async function setKnowledgeDocumentsMetadata(
  projectId: string,
  baseId: string,
  input: KnowledgeDocumentsMetadataInput,
  signal?: AbortSignal,
): Promise<KnowledgeDocumentItem[]> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/bases/${encodeURIComponent(baseId)}/documents/metadata`,
    jsonRequestInit("PATCH", input, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeDocumentBatchResponseSchema,
  );
  return parsed.items;
}

/** Server-side re-parse preview of the stored original file. */
export async function previewKnowledgeDocumentReparse(
  projectId: string,
  documentId: string,
  input: KnowledgeReparseInput,
  signal?: AbortSignal,
): Promise<KnowledgeReparsePreviewResponse> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/documents/${encodeURIComponent(documentId)}/reparse-preview`,
    jsonRequestInit("POST", input, signal),
  );
  return readKnowledgeResponse(response, knowledgeReparsePreviewResponseSchema);
}

/** Confirmed re-parse: freezes the submitted parameters into the task. */
export async function reparseKnowledgeDocument(
  projectId: string,
  documentId: string,
  input: KnowledgeReparseInput,
  signal?: AbortSignal,
): Promise<KnowledgeDocumentItem> {
  const response = await requestKnowledge(
    `${knowledgeBaseURL(projectId)}/documents/${encodeURIComponent(documentId)}/reparse`,
    jsonRequestInit("POST", input, signal),
  );
  const parsed = await readKnowledgeResponse(
    response,
    knowledgeDocumentMutationResponseSchema,
  );
  return parsed.item;
}
