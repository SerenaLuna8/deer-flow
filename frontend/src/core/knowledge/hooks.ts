"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type Query,
} from "@tanstack/react-query";

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
  isKnowledgeConflictError,
  listKnowledgeBaseQueries,
  listKnowledgeBases,
  listKnowledgeDocumentSegments,
  listKnowledgeDocuments,
  listKnowledgeMetadataFields,
  listKnowledgeModelOptions,
  previewKnowledgeChunks,
  rebuildKnowledgeBase,
  renameKnowledgeDocument,
  renameKnowledgeMetadataField,
  retryKnowledgeDocument,
  searchKnowledge,
  setKnowledgeDocumentMetadata,
  setKnowledgeDocumentsEnabled,
  updateKnowledgeBase,
  updateKnowledgeSegment,
  uploadKnowledgeDocument,
} from "./api";
import { knowledgeQueryKey } from "./query-keys";
import {
  KNOWLEDGE_DOCUMENT_ACTIVE_STATUSES,
  type CreateKnowledgeBaseInput,
  type CreateKnowledgeMetadataFieldInput,
  type KnowledgeBaseListResponse,
  type KnowledgeDocumentListResponse,
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
      KNOWLEDGE_DOCUMENT_ACTIVE_STATUSES.includes(item.status) &&
      !(item.status === "deleting" && item.delete_error),
  )
    ? KNOWLEDGE_POLL_INTERVAL_MS
    : false;
}

export function useKnowledgeDocuments(
  scope: ProjectClientScope,
  baseId: string | null,
) {
  return useQuery({
    queryKey: knowledgeQueryKey(scope, "documents", "list", baseId),
    queryFn: ({ signal }) =>
      listKnowledgeDocuments(scope.projectId, baseId ?? "", signal),
    enabled: baseId !== null,
    refetchOnWindowFocus: false,
    refetchInterval: documentsNeedPolling,
  });
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
    onSuccess: () => invalidate.bases(),
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

/** Rebind the model configuration; every document re-embeds through the queue. */
export function useRebuildKnowledgeBase(scope: ProjectClientScope) {
  const invalidate = useInvalidateKnowledge(scope);
  return useMutation({
    mutationKey: knowledgeQueryKey(scope, "mutation", "rebuild-base"),
    mutationFn: ({
      baseId,
      modelConfigurationId,
    }: {
      baseId: string;
      modelConfigurationId: string;
    }) => rebuildKnowledgeBase(scope.projectId, baseId, modelConfigurationId),
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
    mutationFn: (input: PreviewKnowledgeChunksInput) =>
      previewKnowledgeChunks(scope.projectId, input),
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
    mutationFn: ({
      documentIds,
    }: {
      documentIds: string[];
      baseId: string;
    }) => deleteKnowledgeDocuments(scope.projectId, documentIds),
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
  });
}

const SEGMENT_PAGE_SIZE = 20;

export function useKnowledgeDocumentSegments(
  scope: ProjectClientScope,
  documentId: string | null,
  page: number,
) {
  return useQuery({
    queryKey: knowledgeQueryKey(scope, "segments", documentId, page),
    queryFn: ({ signal }) =>
      listKnowledgeDocumentSegments(
        scope.projectId,
        documentId ?? "",
        page,
        SEGMENT_PAGE_SIZE,
        signal,
      ),
    enabled: documentId !== null,
    refetchOnWindowFocus: false,
  });
}
