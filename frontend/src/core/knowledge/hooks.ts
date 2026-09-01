"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type Query,
  type QueryClient,
} from "@tanstack/react-query";
import { useEffect, useState } from "react";

import type { ProjectClientScope } from "@/core/private-work/types";

import {
  createKnowledgeBase,
  createKnowledgeMetadataField,
  createKnowledgeSegment,
  deleteKnowledgeBase,
  deleteKnowledgeDocument,
  deleteKnowledgeDocuments,
  deleteKnowledgeMetadataField,
  deleteKnowledgeSegment,
  fetchKnowledgeHealth,
  getKnowledgeSegmentDetail,
  isKnowledgeAuthorityBoundaryError,
  isKnowledgeConflictError,
  listKnowledgeBaseQueries,
  listKnowledgeBases,
  listKnowledgeDocumentSegments,
  listKnowledgeDocumentAttachments,
  listKnowledgeDocuments,
  listKnowledgeFileCapabilities,
  listKnowledgeMetadataFields,
  listKnowledgeModelOptions,
  previewKnowledgeChunks,
  previewKnowledgeDocumentReparse,
  rebuildKnowledgeBase,
  renameKnowledgeDocument,
  renameKnowledgeMetadataField,
  reparseKnowledgeDocument,
  retryKnowledgeDocument,
  searchKnowledge,
  setKnowledgeDocumentMetadata,
  setKnowledgeDocumentsEnabled,
  setKnowledgeDocumentsMetadata,
  updateKnowledgeBase,
  updateKnowledgeSegment,
  uploadKnowledgeDocument,
} from "./api";
import {
  knowledgeFileCapabilitiesQueryKey,
  knowledgeQueryKey,
} from "./query-keys";
import {
  KNOWLEDGE_DOCUMENT_ACTIVE_STATUSES,
  type CreateKnowledgeBaseInput,
  type CreateKnowledgeMetadataFieldInput,
  type KnowledgeBaseListResponse,
  type KnowledgeDocumentItem,
  type KnowledgeDocumentListResponse,
  type KnowledgeDocumentsMetadataInput,
  type KnowledgeReparseInput,
  type KnowledgeSearchInput,
  type PreviewKnowledgeChunksInput,
  type SetKnowledgeDocumentMetadataInput,
  type UpdateKnowledgeBaseInput,
  type UpdateKnowledgeSegmentInput,
  type UploadKnowledgeDocumentInput,
} from "./types";

const KNOWLEDGE_POLL_INTERVAL_MS = 2000;

/**
 * A `deleting` row with a recorded `delete_error` is parked until the user
 * explicitly re-deletes, so it must not keep the list polling forever.
 */
function basesNeedPolling(
  query: Query<KnowledgeBaseListResponse, Error, KnowledgeBaseListResponse>,
): number | false {
  const items = query.state.data?.items;
  if (!items) return false;
  return items.some((item) => item.status === "deleting" && !item.delete_error)
    ? KNOWLEDGE_POLL_INTERVAL_MS
    : false;
}

export function useKnowledgeBases(scope: ProjectClientScope) {
  return useQuery({
    queryKey: knowledgeQueryKey(scope, "bases", "list"),
    queryFn: ({ signal }) => listKnowledgeBases(scope.projectId, signal),
    refetchOnWindowFocus: false,
    refetchInterval: basesNeedPolling,
  });
}

/**
 * Feature probe behind the navigation entry: resolves `true` only when the
 * knowledge routes exist for this deployment (module enabled). A 404
 * `KNOWLEDGE_DISABLED` — or any failure — keeps the entry hidden.
 */
export function useKnowledgeFeature(
  scope: ProjectClientScope,
  enabled: boolean,
) {
  const query = useQuery({
    queryKey: knowledgeQueryKey(scope, "feature"),
    queryFn: ({ signal }) => fetchKnowledgeHealth(scope.projectId, signal),
    enabled,
    retry: false,
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  });
  return { enabled: query.data?.enabled === true };
}

export function useKnowledgeModelOptions(
  scope: ProjectClientScope,
  enabled: boolean,
) {
  return useQuery({
    queryKey: knowledgeQueryKey(scope, "model-options"),
    queryFn: ({ signal }) => listKnowledgeModelOptions(scope.projectId, signal),
    enabled,
    refetchOnWindowFocus: false,
  });
}

export function useKnowledgeFileCapabilities(scope: ProjectClientScope) {
  return useQuery({
    queryKey: knowledgeFileCapabilitiesQueryKey(scope),
    queryFn: ({ signal }) =>
      listKnowledgeFileCapabilities(scope.projectId, signal),
    retry: false,
    refetchOnWindowFocus: false,
  });
}

function documentsNeedPolling(
  query: Query<
    KnowledgeDocumentListResponse,
    Error,
    KnowledgeDocumentListResponse
  >,
): number | false {
  const items = query.state.data?.items;
  if (!items) return false;
  return items.some(
    (item) =>
      (KNOWLEDGE_DOCUMENT_ACTIVE_STATUSES.includes(item.status) &&
        !(item.status === "deleting" && item.delete_error)) ||
      (item.task_progress !== null && item.task_progress.status !== "failed"),
  )
    ? KNOWLEDGE_POLL_INTERVAL_MS
    : false;
}

export function removeKnowledgeDocumentsCacheForAuthorityError(
  queryClient: QueryClient,
  scope: ProjectClientScope,
  baseId: string | null,
  error: unknown,
): boolean {
  if (baseId === null || !isKnowledgeAuthorityBoundaryError(error)) {
    return false;
  }
  queryClient.removeQueries({
    queryKey: knowledgeQueryKey(scope, "documents", "list", baseId),
    exact: true,
  });
  return true;
}

export function useKnowledgeDocuments(
  scope: ProjectClientScope,
  baseId: string | null,
) {
  const queryClient = useQueryClient();
  const { accountId, projectId } = scope;
  const authorityKey =
    baseId === null ? null : `${accountId}:${projectId}:${baseId}`;
  const [authorityBlock, setAuthorityBlock] = useState<{
    key: string;
    error: Error;
  } | null>(null);
  const authorityBlockedForCurrentKey = authorityBlock?.key === authorityKey;
  const query = useQuery({
    queryKey: knowledgeQueryKey(scope, "documents", "list", baseId),
    queryFn: ({ signal }) =>
      listKnowledgeDocuments(scope.projectId, baseId ?? "", signal),
    enabled: baseId !== null && !authorityBlockedForCurrentKey,
    retry: (failureCount, error) =>
      !isKnowledgeAuthorityBoundaryError(error) && failureCount < 3,
    refetchOnWindowFocus: false,
    refetchInterval: documentsNeedPolling,
  });

  // An authority boundary is terminal for this exact account/project/base
  // identity. Disable the active observer first; removing an enabled query can
  // immediately recreate and refetch it, leaking requests in a tight loop.
  useEffect(() => {
    const error = query.error;
    if (
      authorityKey !== null &&
      !authorityBlockedForCurrentKey &&
      error !== null &&
      isKnowledgeAuthorityBoundaryError(error)
    ) {
      setAuthorityBlock({ key: authorityKey, error });
    }
  }, [authorityBlockedForCurrentKey, authorityKey, query.error]);

  // This runs on the render after the observer became disabled. Keep the
  // boundary error in local state while clearing the now-inaccessible rows.
  useEffect(() => {
    if (!authorityBlockedForCurrentKey || authorityBlock === null) return;
    removeKnowledgeDocumentsCacheForAuthorityError(
      queryClient,
      { accountId, projectId },
      baseId,
      authorityBlock.error,
    );
  }, [
    accountId,
    authorityBlock,
    authorityBlockedForCurrentKey,
    baseId,
    projectId,
    queryClient,
  ]);

  const immediateAuthorityError =
    query.error !== null && isKnowledgeAuthorityBoundaryError(query.error)
      ? query.error
      : null;
  const visibleAuthorityError = authorityBlockedForCurrentKey
    ? authorityBlock?.error
    : immediateAuthorityError;
  if (visibleAuthorityError == null) return query;
  return {
    ...query,
    data: undefined,
    error: visibleAuthorityError,
  };
}

function useInvalidateKnowledge(scope: ProjectClientScope) {
  const queryClient = useQueryClient();
  return {
    bases: () =>
      queryClient.invalidateQueries({
        queryKey: knowledgeQueryKey(scope, "bases"),
      }),
    documents: (baseId: string) =>
      queryClient.invalidateQueries({
        queryKey: knowledgeQueryKey(scope, "documents", "list", baseId),
      }),
    segments: (documentId: string) =>
      queryClient.invalidateQueries({
        queryKey: knowledgeQueryKey(scope, "segments", documentId),
      }),
    metadataFields: (baseId: string) =>
      queryClient.invalidateQueries({
        queryKey: knowledgeQueryKey(scope, "metadata-fields", baseId),
      }),
  };
}

export function useCreateKnowledgeBase(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    // Keys under the knowledge root so scope transitions dispose the mutation.
    mutationKey: knowledgeQueryKey(scope, "mutation", "create-base"),
    mutationFn: (input: CreateKnowledgeBaseInput) =>
      createKnowledgeBase(scope.projectId, input),
    onSuccess: () => invalidate.bases(),
  });
}

export function useUpdateKnowledgeBase(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "update-base"),
    mutationFn: ({
      baseId,
      input,
    }: {
      baseId: string;
      input: UpdateKnowledgeBaseInput;
    }) => updateKnowledgeBase(scope.projectId, baseId, input),
    onSuccess: async (result, variables) => {
      await Promise.all([
        invalidate.bases(),
        ...(result.summary_backfill === null
          ? []
          : [invalidate.documents(variables.baseId)]),
      ]);
    },
  });
}

export function useDeleteKnowledgeBase(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "delete-base"),
    mutationFn: (baseId: string) =>
      deleteKnowledgeBase(scope.projectId, baseId),
    onSuccess: () => invalidate.bases(),
  });
}

/** Rebind the embedding model; every document re-embeds through the queue. */
export function useRebuildKnowledgeBase(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "rebuild-base"),
    mutationFn: ({
      baseId,
      embeddingModelId,
    }: {
      baseId: string;
      embeddingModelId: string;
    }) => rebuildKnowledgeBase(scope.projectId, baseId, embeddingModelId),
    onSuccess: async (_item, variables) => {
      await Promise.all([
        invalidate.bases(),
        invalidate.documents(variables.baseId),
      ]);
    },
  });
}

export function useUploadKnowledgeDocument(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "upload-document"),
    mutationFn: ({
      baseId,
      input,
    }: {
      baseId: string;
      input: UploadKnowledgeDocumentInput;
    }) => uploadKnowledgeDocument(scope.projectId, baseId, input),
    onSuccess: async (_item, variables) => {
      await Promise.all([
        invalidate.documents(variables.baseId),
        invalidate.bases(),
      ]);
    },
  });
}

/**
 * Stateless chunk preview for the create wizard: no cache writes, no
 * invalidation — the response is only rendered next to the parameters.
 */
export function useKnowledgeChunkPreview(scope: ProjectClientScope) {
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "chunk-preview"),
    mutationFn: ({
      input,
      signal,
    }: {
      input: PreviewKnowledgeChunksInput;
      signal?: AbortSignal;
    }) => previewKnowledgeChunks(scope.projectId, input, signal),
  });
}

export function useDeleteKnowledgeDocument(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "delete-document"),
    mutationFn: ({ documentId }: { documentId: string; baseId: string }) =>
      deleteKnowledgeDocument(scope.projectId, documentId),
    onSuccess: async (_item, variables) => {
      await Promise.all([
        invalidate.documents(variables.baseId),
        invalidate.bases(),
      ]);
    },
  });
}

export function useRetryKnowledgeDocument(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "retry-document"),
    mutationFn: ({ documentId }: { documentId: string; baseId: string }) =>
      retryKnowledgeDocument(scope.projectId, documentId),
    onSuccess: (_item, variables) => invalidate.documents(variables.baseId),
  });
}

export function useRenameKnowledgeDocument(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "rename-document"),
    mutationFn: ({
      documentId,
      name,
    }: {
      documentId: string;
      baseId: string;
      name: string;
    }) => renameKnowledgeDocument(scope.projectId, documentId, name),
    onSuccess: (_item, variables) => invalidate.documents(variables.baseId),
  });
}

export function useSetKnowledgeDocumentsEnabled(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "documents-status"),
    mutationFn: ({
      documentIds,
      enabled,
    }: {
      documentIds: string[];
      baseId: string;
      enabled: boolean;
    }) => setKnowledgeDocumentsEnabled(scope.projectId, documentIds, enabled),
    onSuccess: (_items, variables) => invalidate.documents(variables.baseId),
  });
}

export function useDeleteKnowledgeDocuments(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "documents-delete"),
    mutationFn: ({ documentIds }: { documentIds: string[]; baseId: string }) =>
      deleteKnowledgeDocuments(scope.projectId, documentIds),
    onSuccess: async (_items, variables) => {
      await Promise.all([
        invalidate.documents(variables.baseId),
        invalidate.bases(),
      ]);
    },
  });
}

export function useCreateKnowledgeSegment(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "create-segment"),
    mutationFn: ({
      documentId,
      content,
    }: {
      documentId: string;
      baseId: string;
      content: string;
    }) => createKnowledgeSegment(scope.projectId, documentId, content),
    onSuccess: async (_item, variables) => {
      await Promise.all([
        invalidate.segments(variables.documentId),
        invalidate.documents(variables.baseId),
      ]);
    },
    // KNOWLEDGE_CONFLICT means a re-ingest or delete won the race: the cached
    // segment list is stale, so refresh it for the retry the message asks for.
    onError: async (error, variables) => {
      if (!isKnowledgeConflictError(error)) return;
      await Promise.all([
        invalidate.segments(variables.documentId),
        invalidate.documents(variables.baseId),
      ]);
    },
  });
}

export function useUpdateKnowledgeSegment(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "update-segment"),
    mutationFn: ({
      segmentId,
      input,
    }: {
      segmentId: string;
      documentId: string;
      baseId: string;
      input: UpdateKnowledgeSegmentInput;
    }) => updateKnowledgeSegment(scope.projectId, segmentId, input),
    onSuccess: async (_item, variables) => {
      // A content edit changes the aggregated document word count too.
      await Promise.all([
        invalidate.segments(variables.documentId),
        invalidate.documents(variables.baseId),
      ]);
    },
    // KNOWLEDGE_CONFLICT means a re-ingest or delete won the race: the cached
    // segment list is stale, so refresh it for the retry the message asks for.
    onError: async (error, variables) => {
      if (!isKnowledgeConflictError(error)) return;
      await Promise.all([
        invalidate.segments(variables.documentId),
        invalidate.documents(variables.baseId),
      ]);
    },
  });
}

export function useDeleteKnowledgeSegment(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "delete-segment"),
    mutationFn: ({
      segmentId,
    }: {
      segmentId: string;
      documentId: string;
      baseId: string;
    }) => deleteKnowledgeSegment(scope.projectId, segmentId),
    onSuccess: async (_item, variables) => {
      await Promise.all([
        invalidate.segments(variables.documentId),
        invalidate.documents(variables.baseId),
      ]);
    },
  });
}

export function useKnowledgeSearch(scope: ProjectClientScope) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "search"),
    mutationFn: (input: KnowledgeSearchInput) =>
      searchKnowledge(scope.projectId, input),
    // Every search appends a query-log row; refresh any recent-query lists.
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: knowledgeQueryKey(scope, "queries"),
      }),
  });
}

const QUERY_LOG_PAGE_SIZE = 10;

/** Recent retrieval queries (agent + test panel) that targeted this base. */
export function useKnowledgeBaseQueries(
  scope: ProjectClientScope,
  baseId: string | null,
  page: number,
) {
  return useQuery({
    queryKey: knowledgeQueryKey(scope, "queries", baseId, page),
    queryFn: ({ signal }) =>
      listKnowledgeBaseQueries(
        scope.projectId,
        baseId ?? "",
        page,
        QUERY_LOG_PAGE_SIZE,
        signal,
      ),
    enabled: baseId !== null,
    refetchOnWindowFocus: false,
  });
}

export function useKnowledgeMetadataFields(
  scope: ProjectClientScope,
  baseId: string | null,
) {
  return useQuery({
    queryKey: knowledgeQueryKey(scope, "metadata-fields", baseId),
    queryFn: ({ signal }) =>
      listKnowledgeMetadataFields(scope.projectId, baseId ?? "", signal),
    enabled: baseId !== null,
    refetchOnWindowFocus: false,
  });
}

export function useCreateKnowledgeMetadataField(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "create-metadata-field"),
    mutationFn: ({
      baseId,
      input,
    }: {
      baseId: string;
      input: CreateKnowledgeMetadataFieldInput;
    }) => createKnowledgeMetadataField(scope.projectId, baseId, input),
    onSuccess: (_item, variables) =>
      invalidate.metadataFields(variables.baseId),
  });
}

export function useRenameKnowledgeMetadataField(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "rename-metadata-field"),
    mutationFn: ({
      fieldId,
      name,
    }: {
      fieldId: string;
      baseId: string;
      name: string;
    }) => renameKnowledgeMetadataField(scope.projectId, fieldId, name),
    onSuccess: async (_item, variables) => {
      // Renames rewrite document metadata keys on the backend.
      await Promise.all([
        invalidate.metadataFields(variables.baseId),
        invalidate.documents(variables.baseId),
      ]);
    },
  });
}

export function useDeleteKnowledgeMetadataField(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "delete-metadata-field"),
    mutationFn: ({ fieldId }: { fieldId: string; baseId: string }) =>
      deleteKnowledgeMetadataField(scope.projectId, fieldId),
    onSuccess: async (_item, variables) => {
      await Promise.all([
        invalidate.metadataFields(variables.baseId),
        invalidate.documents(variables.baseId),
      ]);
    },
  });
}

/**
 * A rejected metadata write that hit a conflict or a vanished selection was
 * confirmed against stale rows: refresh the authoritative documents so
 * re-confirmation sees current state, while the dialog keeps its unsaved
 * form values.
 */
function metadataWriteHitStaleRows(error: unknown): boolean {
  return (
    isKnowledgeConflictError(error) || isKnowledgeAuthorityBoundaryError(error)
  );
}

export function useSetKnowledgeDocumentMetadata(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "document-metadata"),
    mutationFn: ({
      documentId,
      input,
    }: {
      documentId: string;
      baseId: string;
      input: SetKnowledgeDocumentMetadataInput;
    }) => setKnowledgeDocumentMetadata(scope.projectId, documentId, input),
    onSuccess: (_item, variables) => invalidate.documents(variables.baseId),
    onError: (error, variables) => {
      if (metadataWriteHitStaleRows(error)) {
        void invalidate.documents(variables.baseId);
      }
    },
  });
}

/** One all-or-nothing metadata patch across the selected documents. */
export function useSetKnowledgeDocumentsMetadata(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "documents-metadata"),
    mutationFn: ({
      baseId,
      input,
    }: {
      baseId: string;
      input: KnowledgeDocumentsMetadataInput;
    }) => setKnowledgeDocumentsMetadata(scope.projectId, baseId, input),
    onSuccess: (_items, variables) => invalidate.documents(variables.baseId),
    onError: (error, variables) => {
      if (metadataWriteHitStaleRows(error)) {
        void invalidate.documents(variables.baseId);
      }
    },
  });
}

/**
 * Server-side re-parse preview of the stored original file. A mutation, not a
 * query: it must run only on the user's explicit request and never revive
 * from a cache — the preview is tied to the exact submitted parameters.
 */
export function usePreviewKnowledgeDocumentReparse(scope: ProjectClientScope) {
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "reparse-preview"),
    mutationFn: ({
      documentId,
      input,
    }: {
      documentId: string;
      input: KnowledgeReparseInput;
    }) => previewKnowledgeDocumentReparse(scope.projectId, documentId, input),
  });
}

export function useReparseKnowledgeDocument(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "reparse-document"),
    mutationFn: ({
      documentId,
      input,
    }: {
      documentId: string;
      baseId: string;
      input: KnowledgeReparseInput;
    }) => reparseKnowledgeDocument(scope.projectId, documentId, input),
    onSuccess: (_item, variables) => invalidate.documents(variables.baseId),
  });
}

/**
 * Locates one segment via its detail endpoint — never by walking the base's
 * pages. The backend validates the base/document/segment lineage, so a
 * cross-base id combination or a deleted resource answers 404 instead of
 * resurrecting a stale object; that failure is terminal (no retries).
 */
export function useKnowledgeSegmentLocate(
  scope: ProjectClientScope,
  baseId: string,
  document: Pick<KnowledgeDocumentItem, "id" | "version" | "status">,
  segmentId: string | null,
) {
  return useQuery({
    queryKey: knowledgeQueryKey(
      scope,
      // Share the document's segment subtree so edits and deletes refresh
      // both the list and an already-mounted location card.
      "segments",
      document.id,
      "locate",
      baseId,
      segmentId,
      // Admission changes the version; publication changes the status. Both
      // need a new detail read as a reparse/re-embed finishes under an open card.
      document.version,
      document.status,
    ),
    queryFn: ({ signal }) =>
      getKnowledgeSegmentDetail(
        scope.projectId,
        baseId,
        document.id,
        segmentId ?? "",
        undefined,
        signal,
      ),
    enabled: segmentId !== null,
    retry: false,
    refetchOnWindowFocus: false,
    gcTime: 0,
  });
}

export type KnowledgeSearchHitDetailInput = {
  baseId: string;
  documentId: string;
  segmentId: string;
  /** Version/digest the hit's score was computed for; null skips the pin. */
  expectedDocumentVersion: number | null;
  expectedContentDigest: string | null;
  childPage: number;
};

/**
 * Detail read pinned to the exact content a search hit scored. A version or
 * digest mismatch is a terminal KNOWLEDGE_CONFLICT: the panel asks for a new
 * search instead of explaining old scores with new text. Nothing is cached
 * past the dialog (gcTime 0), so a closed detail can never resurface.
 */
export function useKnowledgeSearchHitDetail(
  scope: ProjectClientScope,
  input: KnowledgeSearchHitDetailInput | null,
) {
  return useQuery({
    queryKey: knowledgeQueryKey(
      scope,
      "search-hit-detail",
      input?.baseId,
      input?.documentId,
      input?.segmentId,
      input?.expectedDocumentVersion,
      input?.expectedContentDigest,
      input?.childPage,
    ),
    queryFn: ({ signal }) => {
      if (input === null) throw new Error("disabled");
      return getKnowledgeSegmentDetail(
        scope.projectId,
        input.baseId,
        input.documentId,
        input.segmentId,
        {
          ...(input.expectedDocumentVersion !== null
            ? { expectedDocumentVersion: input.expectedDocumentVersion }
            : {}),
          ...(input.expectedContentDigest !== null
            ? { expectedContentDigest: input.expectedContentDigest }
            : {}),
          childPage: input.childPage,
        },
        signal,
      );
    },
    enabled: input !== null,
    retry: false,
    refetchOnWindowFocus: false,
    gcTime: 0,
  });
}

const SEGMENT_PAGE_SIZE = 20;

export function useKnowledgeDocumentAttachments(
  scope: ProjectClientScope,
  document: Pick<KnowledgeDocumentItem, "id" | "status" | "version"> | null,
  enabled: boolean,
) {
  return useQuery({
    queryKey: knowledgeQueryKey(
      scope,
      "document-attachments",
      document?.id ?? null,
      document?.version ?? null,
    ),
    queryFn: ({ signal }) =>
      listKnowledgeDocumentAttachments(
        scope.projectId,
        document?.id ?? "",
        signal,
      ),
    enabled: enabled && document?.status === "ready",
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function useKnowledgeDocumentSegments(
  scope: ProjectClientScope,
  document: Pick<KnowledgeDocumentItem, "id" | "status" | "version"> | null,
  page: number,
) {
  return useQuery({
    queryKey: knowledgeQueryKey(
      scope,
      "segments",
      document?.id ?? null,
      document?.version ?? null,
      document?.status ?? null,
      page,
    ),
    queryFn: ({ signal }) =>
      listKnowledgeDocumentSegments(
        scope.projectId,
        document?.id ?? "",
        page,
        SEGMENT_PAGE_SIZE,
        signal,
      ),
    enabled: document !== null,
    refetchOnWindowFocus: false,
  });
}
