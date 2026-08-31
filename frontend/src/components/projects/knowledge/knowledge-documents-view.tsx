"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  DownloadIcon,
  FileCog2Icon,
  ListTreeIcon,
  MoreHorizontalIcon,
  PencilIcon,
  RotateCcwIcon,
  SearchIcon,
  TagsIcon,
  Trash2Icon,
  UploadIcon,
} from "lucide-react";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useI18n } from "@/core/i18n/hooks";
import {
  isKnowledgeAuthorityBoundaryError,
  isKnowledgeConflictError,
  knowledgeDocumentDownloadURL,
} from "@/core/knowledge/api";
import {
  isChildChunkSizeValid,
  isChunkOverlapValid,
  isChunkSeparatorValid,
  isChunkSizeValid,
  KNOWLEDGE_CHILD_CHUNK_SIZE_MAX,
  KNOWLEDGE_CHILD_CHUNK_SIZE_MIN,
  KNOWLEDGE_CHUNK_OVERLAP_MAX,
  KNOWLEDGE_CHUNK_OVERLAP_MIN,
  KNOWLEDGE_CHUNK_SIZE_MAX,
  KNOWLEDGE_CHUNK_SIZE_MIN,
} from "@/core/knowledge/chunk-settings";
import {
  deriveKnowledgeDocumentList,
  type KnowledgeDocumentListView,
} from "@/core/knowledge/document-list";
import {
  useDeleteKnowledgeDocument,
  useDeleteKnowledgeDocuments,
  useKnowledgeDocuments,
  useKnowledgeMetadataFields,
  usePreviewKnowledgeDocumentReparse,
  useRenameKnowledgeDocument,
  useReparseKnowledgeDocument,
  useRetryKnowledgeDocument,
  useSetKnowledgeDocumentMetadata,
  useSetKnowledgeDocumentsEnabled,
  useSetKnowledgeDocumentsMetadata,
  useUploadKnowledgeDocument,
} from "@/core/knowledge/hooks";
import type {
  KnowledgeDocumentSort,
  KnowledgeNavigationState,
} from "@/core/knowledge/navigation";
import { knowledgeQueryKey } from "@/core/knowledge/query-keys";
import {
  DEFAULT_CHILD_CHUNK_SEPARATOR,
  DEFAULT_CHUNK_SEPARATOR,
  type KnowledgeBaseItem,
  type KnowledgeChunkingMode,
  type KnowledgeDocumentItem,
  type KnowledgeDocumentStatus,
  type KnowledgeMetadataFieldItem,
  type KnowledgeReparseInput,
  type KnowledgeReparsePreviewResponse,
  type KnowledgeTaskProgress,
} from "@/core/knowledge/types";
import type { ProjectClientScope } from "@/core/private-work/types";
import { cn } from "@/lib/utils";

import { knowledgeErrorMessage } from "./knowledge-error";
import { KnowledgeFileTypeIcon } from "./knowledge-file-type-icon";
import { KnowledgeSegmentsBrowser } from "./knowledge-segments-browser";

/** Mirrors the backend's frozen ALLOWED_DOCUMENT_EXTENSIONS. */
export const KNOWLEDGE_UPLOAD_ACCEPT =
  ".pdf,.docx,.txt,.md,.csv,.xlsx,.html,.htm,.pptx,.epub";

export function documentStatusVariant(
  status: KnowledgeDocumentStatus,
): "default" | "secondary" | "destructive" | "outline" {
  if (status === "ready") return "default";
  if (status === "failed") return "destructive";
  if (status === "deleting") return "outline";
  return "secondary";
}

export function documentStatusClassName(
  status: KnowledgeDocumentStatus,
): string {
  return cn(
    "rounded-md px-1.5 py-0.5 text-[11px] font-medium",
    status === "ready" &&
      "border-success/15 bg-success/10 text-emerald-700 dark:text-emerald-300",
    status === "failed" &&
      "border-destructive/15 bg-destructive/10 text-destructive dark:bg-destructive/10",
    (status === "uploading" ||
      status === "queued" ||
      status === "processing") &&
      "border-amber-500/15 bg-amber-500/10 text-amber-700 dark:text-amber-300",
    status === "deleting" && "border-border/70 bg-muted text-muted-foreground",
  );
}

function formatSizeBytes(sizeBytes: number): string {
  if (sizeBytes >= 1024 * 1024) {
    return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  if (sizeBytes >= 1024) {
    return `${(sizeBytes / 1024).toFixed(1)} KB`;
  }
  return `${sizeBytes} B`;
}

function DocumentErrorMessage({ message }: { message: string }) {
  return (
    <div role="status" aria-live="polite" aria-atomic="true">
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="text-destructive mt-1 line-clamp-2 w-full max-w-56 cursor-help text-left text-xs leading-4 break-words"
          >
            {message}
          </button>
        </TooltipTrigger>
        <TooltipContent
          side="bottom"
          align="start"
          className="max-w-sm text-left break-words whitespace-normal"
        >
          {message}
        </TooltipContent>
      </Tooltip>
    </div>
  );
}

/**
 * The open indexing attempt as the server reports it: stage, verified batch
 * counts, and the attempt number. Stages without a verifiable total render
 * indeterminate (no counter, never a simulated percentage), and a failed
 * task keeps its failing stage on screen.
 */
function TaskProgressLine({ progress }: { progress: KnowledgeTaskProgress }) {
  const { t } = useI18n();
  const labels = t.knowledge.documents.progress;
  const stageLabel = labels.stages[progress.stage];
  const parts: string[] = [
    `${labels.kinds[progress.kind]} · ${
      progress.status === "failed"
        ? labels.failedDuring(stageLabel)
        : stageLabel
    }`,
  ];
  if (progress.total_units !== null) {
    parts.push(labels.units(progress.completed_units, progress.total_units));
  }
  if (
    progress.attempt_count > 1 ||
    progress.status === "retry_wait" ||
    progress.status === "failed"
  ) {
    parts.push(labels.attempt(progress.attempt_count, progress.max_attempts));
  }
  return (
    <div
      className="text-muted-foreground mt-1 text-xs"
      data-testid="knowledge-task-progress"
    >
      <span>{parts.join(" · ")}</span>
      {progress.status === "retry_wait" ? (
        <span className="block">
          {progress.next_attempt_at
            ? labels.retryWaitAt(
                new Date(progress.next_attempt_at).toLocaleTimeString(),
              )
            : labels.retryWaitSoon}
        </span>
      ) : null}
    </div>
  );
}

export function KnowledgeDocumentsView({
  scope,
  base,
  canEdit,
  navState,
  onNavigate,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  canEdit: boolean;
  navState: KnowledgeNavigationState;
  onNavigate: (
    next: KnowledgeNavigationState,
    mode: "push" | "replace",
  ) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const documents = useKnowledgeDocuments(scope, base.id);

  // Resolved from the live list so segment mutations (which invalidate the
  // documents query) keep the header counts fresh while browsing. The URL
  // names the document; a deleted or foreign id shows "inaccessible" and
  // never falls back to a cached object.
  const browsing =
    navState.doc === null
      ? null
      : (documents.data?.items.find((item) => item.id === navState.doc) ??
        null);

  // Returning to the list keeps status/sort/page so the previous location
  // is restored; the transient keyword lives below and survives on its own.
  const closeBrowser = () =>
    onNavigate({ ...navState, doc: null, segment: null }, "push");

  // Without list data the browser cannot resolve its document. A pending
  // first load shows a skeleton; a blocking failure (including authority
  // loss, which conceals the cached rows) falls through to the table so its
  // error rendering owns the message instead of a misleading "not found".
  if (navState.doc !== null && documents.data !== undefined) {
    if (browsing === null) {
      return (
        <div className="rounded-xl border border-dashed px-4 py-12 text-center">
          <p className="text-muted-foreground text-[13px]">
            {labels.documents.notFound}
          </p>
          <Button
            type="button"
            variant="outline"
            className="mt-4 h-9 rounded-lg text-[13px] shadow-none"
            onClick={closeBrowser}
          >
            {labels.documents.backToList}
          </Button>
        </div>
      );
    }
    return (
      <KnowledgeSegmentsBrowser
        scope={scope}
        base={base}
        document={browsing}
        canEdit={canEdit}
        onBack={closeBrowser}
        locateSegmentId={navState.segment}
        onDismissLocate={() =>
          onNavigate({ ...navState, segment: null }, "replace")
        }
      />
    );
  }
  if (navState.doc !== null && documents.error === null) {
    return <Skeleton className="h-40 rounded-xl" />;
  }

  return (
    <DocumentsTable
      scope={scope}
      base={base}
      canEdit={canEdit}
      documents={documents}
      navState={navState}
      onNavigate={onNavigate}
      onBrowse={(document) =>
        onNavigate({ ...navState, doc: document.id }, "push")
      }
    />
  );
}

function DocumentsTable({
  scope,
  base,
  canEdit,
  documents,
  navState,
  onNavigate,
  onBrowse,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  canEdit: boolean;
  documents: ReturnType<typeof useKnowledgeDocuments>;
  navState: KnowledgeNavigationState;
  onNavigate: (
    next: KnowledgeNavigationState,
    mode: "push" | "replace",
  ) => void;
  onBrowse: (document: KnowledgeDocumentItem) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const retryDocument = useRetryKnowledgeDocument(scope);
  const deleteDocument = useDeleteKnowledgeDocument(scope);
  const toggleDocuments = useSetKnowledgeDocumentsEnabled(scope);
  const batchDelete = useDeleteKnowledgeDocuments(scope);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [deleting, setDeleting] = useState<KnowledgeDocumentItem | null>(null);
  const [renaming, setRenaming] = useState<KnowledgeDocumentItem | null>(null);
  // Metadata and reparse dialogs track ids, not row objects: conflicts
  // refresh the authoritative row and the dialogs must re-read the live
  // version instead of a snapshot taken when they opened.
  const [editingMetadataId, setEditingMetadataId] = useState<string | null>(
    null,
  );
  const [reparsingId, setReparsingId] = useState<string | null>(null);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);
  const [batchMetadataOpen, setBatchMetadataOpen] = useState(false);
  // Content-typed state: never written to the URL or browser storage. It
  // survives opening a document and coming back (the component stays
  // mounted), and resets with the component on a base switch or reload.
  const [keyword, setKeyword] = useState("");

  const items = documents.data?.items ?? [];
  // Keyword/status filtering, ordering, and paging all run over the complete
  // authoritative list — the API layer fails loudly when it cannot page to
  // completion, so this is never just the first backend page.
  const derived: KnowledgeDocumentListView = deriveKnowledgeDocumentList(
    items,
    {
      keyword,
      status: navState.status,
      sort: navState.sort,
      page: navState.page,
    },
  );

  // Deleting the last row of the final page (or any list shrink) walks the
  // URL back to the nearest legal page.
  useEffect(() => {
    if (documents.data !== undefined && derived.page !== navState.page) {
      onNavigate({ ...navState, page: derived.page }, "replace");
    }
  }, [derived.page, documents.data, navState, onNavigate]);

  // Selection is scoped to the visible page: page turns and filter changes
  // clear it so hidden rows can never ride along into a batch operation.
  useEffect(() => {
    setSelected(new Set());
  }, [derived.page, navState.status, navState.sort, keyword]);

  // Lifecycle summary over the complete list, not the visible page. A failed
  // document is a terminal state of its own — the summary never folds it
  // into "done".
  const summaryCounts = { processing: 0, retryWait: 0, failed: 0, ready: 0 };
  for (const item of items) {
    if (item.status === "ready") summaryCounts.ready += 1;
    else if (item.status === "failed") summaryCounts.failed += 1;
    else if (item.status === "deleting") continue;
    else if (item.task_progress?.status === "retry_wait")
      summaryCounts.retryWait += 1;
    else summaryCounts.processing += 1;
  }
  const summaryActive =
    summaryCounts.processing + summaryCounts.retryWait + summaryCounts.failed >
    0;

  // Deleting documents reject batch operations server-side, so they are not
  // selectable; stale ids from removed rows drop out here as well.
  const selectableIds = derived.rows
    .filter((item) => item.status !== "deleting")
    .map((item) => item.id);
  const selectedIds = selectableIds.filter((id) => selected.has(id));
  const allSelected =
    selectableIds.length > 0 && selectedIds.length === selectableIds.length;
  const batchPending = toggleDocuments.isPending || batchDelete.isPending;
  const authorityBoundaryError = isKnowledgeAuthorityBoundaryError(
    documents.error,
  );
  const recoverableRefreshError =
    documents.error !== null &&
    documents.data !== undefined &&
    !authorityBoundaryError;
  const blockingDocumentsError =
    documents.error !== null &&
    (documents.data === undefined || authorityBoundaryError);

  const toggleRow = (documentId: string, checked: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(documentId);
      else next.delete(documentId);
      return next;
    });
  };

  // Filter changes reset the page: the old page number described another
  // result set. Both are replace navigations, not history entries.
  const setStatusFilter = (status: KnowledgeDocumentStatus | null) =>
    onNavigate({ ...navState, status, page: 1 }, "replace");
  const setSort = (sort: KnowledgeDocumentSort) =>
    onNavigate({ ...navState, sort, page: 1 }, "replace");
  const setPage = (page: number) =>
    onNavigate({ ...navState, page }, "replace");
  const setKeywordFilter = (next: string) => {
    setKeyword(next);
    if (navState.page !== 1) onNavigate({ ...navState, page: 1 }, "replace");
  };

  const closeDeleteDialog = () => {
    setDeleting(null);
    // A stale error from a previous failed delete must not greet the next one.
    deleteDocument.reset();
  };
  const closeBatchDeleteDialog = () => {
    setBatchDeleteOpen(false);
    batchDelete.reset();
  };

  return (
    <section
      aria-label={labels.documents.title(base.name)}
      className="space-y-4"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <h2 className="truncate text-base font-semibold tracking-tight">
          {labels.detail.documents}
        </h2>
        {canEdit ? (
          <Button
            type="button"
            className="h-9 rounded-lg text-[13px] shadow-none"
            onClick={() => setUploadOpen(true)}
          >
            <UploadIcon aria-hidden className="size-4" />
            {labels.documents.uploadButton}
          </Button>
        ) : null}
      </div>

      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative min-w-0 flex-1 sm:max-w-72">
          <SearchIcon
            aria-hidden
            className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2"
          />
          <Input
            type="search"
            className="border-input/80 bg-background h-9 rounded-lg pl-8 text-[13px] shadow-none md:text-[13px]"
            value={keyword}
            placeholder={labels.documents.searchPlaceholder}
            aria-label={labels.documents.searchAria}
            onChange={(event) => setKeywordFilter(event.target.value)}
          />
        </div>
        <div className="flex items-center gap-2">
          <Select
            value={navState.status ?? "all"}
            onValueChange={(value) =>
              setStatusFilter(
                value === "all" ? null : (value as KnowledgeDocumentStatus),
              )
            }
          >
            <SelectTrigger
              className="border-input/80 bg-background h-9 w-36 rounded-lg text-[13px] shadow-none"
              aria-label={labels.documents.statusFilterLabel}
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent className="rounded-lg">
              <SelectItem className="text-[13px]" value="all">
                {labels.documents.statusFilterAll}
              </SelectItem>
              {(
                [
                  "ready",
                  "processing",
                  "queued",
                  "uploading",
                  "failed",
                  "deleting",
                ] as const
              ).map((status) => (
                <SelectItem className="text-[13px]" key={status} value={status}>
                  {labels.documentStatus[status]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select
            value={navState.sort}
            onValueChange={(value) => setSort(value as KnowledgeDocumentSort)}
          >
            <SelectTrigger
              className="border-input/80 bg-background h-9 w-36 rounded-lg text-[13px] shadow-none"
              aria-label={labels.documents.sortLabel}
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent className="rounded-lg">
              {(
                [
                  "created_desc",
                  "created_asc",
                  "name_asc",
                  "name_desc",
                ] as const
              ).map((sort) => (
                <SelectItem className="text-[13px]" key={sort} value={sort}>
                  {labels.documents.sortOptions[sort]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {retryDocument.error ? (
        <p role="alert" className="text-destructive text-[13px]">
          {knowledgeErrorMessage(retryDocument.error, labels.errors)}
        </p>
      ) : null}
      {toggleDocuments.error ? (
        <p role="alert" className="text-destructive text-[13px]">
          {knowledgeErrorMessage(toggleDocuments.error, labels.errors)}
        </p>
      ) : null}
      {recoverableRefreshError ? (
        <div
          role="alert"
          className="border-destructive/30 bg-destructive/5 flex flex-wrap items-center gap-3 rounded-xl border px-4 py-3 text-[13px]"
        >
          <span className="text-destructive min-w-0 flex-1">
            {knowledgeErrorMessage(documents.error, labels.errors)}
          </span>
          <Button
            className="h-8 rounded-lg text-[13px] shadow-none"
            type="button"
            variant="outline"
            size="sm"
            disabled={documents.isFetching}
            onClick={() => void documents.refetch()}
          >
            {t.common.retry}
          </Button>
        </div>
      ) : null}

      {summaryActive ? (
        <p
          data-testid="knowledge-processing-summary"
          className="border-border/60 bg-muted/40 flex flex-wrap gap-x-3 gap-y-1 rounded-lg border px-3 py-2 text-xs"
        >
          {summaryCounts.processing > 0 ? (
            <span className="text-muted-foreground">
              {labels.documents.processingSummary.processing(
                summaryCounts.processing,
              )}
            </span>
          ) : null}
          {summaryCounts.retryWait > 0 ? (
            <span className="text-muted-foreground">
              {labels.documents.processingSummary.retryWait(
                summaryCounts.retryWait,
              )}
            </span>
          ) : null}
          {summaryCounts.failed > 0 ? (
            <span className="text-destructive font-medium">
              {labels.documents.processingSummary.failed(summaryCounts.failed)}
            </span>
          ) : null}
          <span className="text-muted-foreground">
            {labels.documents.processingSummary.ready(summaryCounts.ready)}
          </span>
        </p>
      ) : null}

      {canEdit && selectedIds.length > 0 ? (
        <div
          data-testid="knowledge-batch-bar"
          className="border-selection/20 bg-selection-subtle/50 flex flex-wrap items-center gap-2 rounded-lg border px-4 py-2 text-[13px]"
        >
          <span className="font-medium">
            {labels.documents.selectedCount(selectedIds.length)}
          </span>
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            <Button
              className="h-8 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              size="sm"
              disabled={batchPending}
              onClick={() =>
                toggleDocuments.mutate({
                  documentIds: selectedIds,
                  baseId: base.id,
                  enabled: true,
                })
              }
            >
              {labels.documents.batchEnable}
            </Button>
            <Button
              className="h-8 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              size="sm"
              disabled={batchPending}
              onClick={() =>
                toggleDocuments.mutate({
                  documentIds: selectedIds,
                  baseId: base.id,
                  enabled: false,
                })
              }
            >
              {labels.documents.batchDisable}
            </Button>
            <Button
              className="h-8 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              size="sm"
              disabled={batchPending}
              onClick={() => setBatchMetadataOpen(true)}
            >
              {labels.documents.batchMetadata}
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="text-destructive h-8 rounded-lg text-[13px] shadow-none"
              disabled={batchPending}
              onClick={() => setBatchDeleteOpen(true)}
            >
              {labels.documents.batchDelete}
            </Button>
            <Button
              className="h-8 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setSelected(new Set())}
            >
              {labels.documents.clearSelection}
            </Button>
          </div>
        </div>
      ) : null}

      {documents.isLoading ? (
        <Skeleton className="h-40 rounded-xl" />
      ) : blockingDocumentsError ? (
        <p role="alert" className="text-destructive text-[13px]">
          {knowledgeErrorMessage(documents.error, labels.errors)}
        </p>
      ) : items.length === 0 ? (
        <p className="text-muted-foreground rounded-xl border border-dashed px-4 py-10 text-center text-[13px]">
          {labels.documents.empty}
        </p>
      ) : derived.filteredTotal === 0 ? (
        <p className="text-muted-foreground rounded-xl border border-dashed px-4 py-10 text-center text-[13px]">
          {labels.documents.filteredEmpty}
        </p>
      ) : (
        <div
          className="border-border/80 bg-card overflow-x-auto rounded-xl border shadow-xs"
          data-testid="knowledge-documents-table"
        >
          <table className="w-full min-w-[680px] table-fixed text-left text-[13px]">
            <thead className="bg-muted/70 text-muted-foreground text-xs [&_th]:font-medium">
              <tr>
                {canEdit ? (
                  <th className="w-10 px-3 py-3">
                    <input
                      type="checkbox"
                      className="accent-selection size-4 align-middle"
                      aria-label={labels.documents.selectAllAria}
                      checked={allSelected}
                      onChange={(event) =>
                        setSelected(
                          event.target.checked
                            ? new Set(selectableIds)
                            : new Set(),
                        )
                      }
                    />
                  </th>
                ) : null}
                <th className="px-3 py-3">{labels.documents.columns.name}</th>
                <th className="w-36 px-3 py-3">
                  {labels.documents.columns.status}
                </th>
                <th className="w-20 px-3 py-3">
                  {labels.documents.columns.enabled}
                </th>
                <th className="w-16 px-2 py-3">
                  {labels.documents.columns.size}
                </th>
                <th className="w-20 px-3 py-3">
                  {labels.documents.columns.segments}
                </th>
                <th className="w-24 px-3 py-3">
                  {labels.documents.columns.words}
                </th>
                <th className="bg-muted/95 sticky right-0 z-10 w-12 px-2 py-3 text-right shadow-[-8px_0_12px_-12px_rgba(0,0,0,0.45)]">
                  <span className="sr-only">
                    {labels.documents.columns.actions}
                  </span>
                </th>
              </tr>
            </thead>
            <tbody data-testid="knowledge-document-rows">
              {derived.rows.map((document) => (
                <tr
                  key={document.id}
                  className={cn(
                    "group border-border/60 hover:bg-muted/40 border-t align-top transition-colors",
                    selected.has(document.id) && "bg-selection-subtle/40",
                  )}
                >
                  {canEdit ? (
                    <td className="px-3 py-3">
                      <input
                        type="checkbox"
                        className="accent-selection size-4 align-middle"
                        aria-label={labels.documents.selectRowAria(
                          document.name,
                        )}
                        checked={selected.has(document.id)}
                        disabled={document.status === "deleting"}
                        onChange={(event) =>
                          toggleRow(document.id, event.target.checked)
                        }
                      />
                    </td>
                  ) : null}
                  <td className="px-3 py-3">
                    <div className="flex min-w-0 items-center gap-3">
                      <KnowledgeFileTypeIcon
                        fileName={document.original_name}
                      />
                      <div className="min-w-0 flex-1">
                        <span
                          title={document.name}
                          className="text-foreground block truncate font-medium"
                        >
                          {document.name}
                        </span>
                        {document.original_name !== document.name ? (
                          <span
                            title={document.original_name}
                            className="text-muted-foreground block truncate text-xs"
                          >
                            {document.original_name}
                          </span>
                        ) : null}
                      </div>
                    </div>
                  </td>
                  <td className="px-3 py-3">
                    <Badge
                      variant={documentStatusVariant(document.status)}
                      className={documentStatusClassName(document.status)}
                    >
                      {labels.documentStatus[document.status]}
                    </Badge>
                    {document.task_progress ? (
                      <TaskProgressLine progress={document.task_progress} />
                    ) : null}
                    {document.status === "failed" && document.error_message ? (
                      <DocumentErrorMessage message={document.error_message} />
                    ) : null}
                    {document.delete_error ? (
                      <DocumentErrorMessage message={document.delete_error} />
                    ) : null}
                  </td>
                  <td className="px-3 py-3">
                    <Switch
                      className="data-[state=checked]:bg-success/80"
                      checked={document.enabled}
                      disabled={
                        !canEdit ||
                        document.status === "deleting" ||
                        toggleDocuments.isPending
                      }
                      aria-label={
                        document.enabled
                          ? labels.documents.disableAria(document.name)
                          : labels.documents.enableAria(document.name)
                      }
                      onCheckedChange={(checked) =>
                        toggleDocuments.mutate({
                          documentIds: [document.id],
                          baseId: base.id,
                          enabled: checked,
                        })
                      }
                    />
                  </td>
                  <td className="text-muted-foreground px-2 py-3">
                    {formatSizeBytes(document.size_bytes)}
                  </td>
                  <td className="text-muted-foreground px-3 py-3 tabular-nums">
                    {document.segment_count}
                  </td>
                  <td className="text-muted-foreground px-3 py-3 tabular-nums">
                    {document.word_count.toLocaleString()}
                  </td>
                  <td className="bg-background group-hover:bg-muted sticky right-0 z-10 px-2 py-2.5 shadow-[-8px_0_12px_-12px_rgba(0,0,0,0.45)]">
                    {document.status === "ready" ||
                    (document.status !== "uploading" &&
                      document.status !== "deleting") ||
                    canEdit ? (
                      <div className="flex justify-end">
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon"
                              className="text-muted-foreground hover:text-foreground size-8 rounded-md text-[13px] shadow-none"
                              aria-label={labels.documents.actionsAria(
                                document.name,
                              )}
                            >
                              <MoreHorizontalIcon
                                aria-hidden
                                className="size-4"
                              />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent
                            align="end"
                            className="w-48 rounded-lg"
                          >
                            {document.status === "ready" ? (
                              <DropdownMenuItem
                                className="text-[13px]"
                                onSelect={() => onBrowse(document)}
                              >
                                <ListTreeIcon aria-hidden className="size-4" />
                                {labels.documents.viewSegments}
                              </DropdownMenuItem>
                            ) : null}
                            {document.status !== "uploading" &&
                            document.status !== "deleting" ? (
                              <DropdownMenuItem className="text-[13px]" asChild>
                                <a
                                  href={knowledgeDocumentDownloadURL(
                                    scope.projectId,
                                    document.id,
                                  )}
                                  download={document.original_name}
                                >
                                  <DownloadIcon
                                    aria-hidden
                                    className="size-4"
                                  />
                                  {labels.documents.download}
                                </a>
                              </DropdownMenuItem>
                            ) : null}
                            {canEdit && document.status === "failed" ? (
                              <DropdownMenuItem
                                className="text-[13px]"
                                disabled={retryDocument.isPending}
                                onSelect={() =>
                                  retryDocument.mutate({
                                    documentId: document.id,
                                    baseId: base.id,
                                  })
                                }
                              >
                                <RotateCcwIcon aria-hidden className="size-4" />
                                {labels.documents.retry}
                              </DropdownMenuItem>
                            ) : null}
                            {canEdit && document.status !== "deleting" ? (
                              <DropdownMenuItem
                                className="text-[13px]"
                                onSelect={() => setRenaming(document)}
                              >
                                <PencilIcon aria-hidden className="size-4" />
                                {labels.documents.rename}
                              </DropdownMenuItem>
                            ) : null}
                            {canEdit && document.status !== "deleting" ? (
                              <DropdownMenuItem
                                className="text-[13px]"
                                onSelect={() =>
                                  setEditingMetadataId(document.id)
                                }
                              >
                                <TagsIcon aria-hidden className="size-4" />
                                {labels.documents.metadataAction}
                              </DropdownMenuItem>
                            ) : null}
                            {canEdit &&
                            (document.status === "ready" ||
                              document.status === "failed") ? (
                              <DropdownMenuItem
                                className="text-[13px]"
                                onSelect={() => setReparsingId(document.id)}
                              >
                                <FileCog2Icon aria-hidden className="size-4" />
                                {labels.documents.reparse}
                              </DropdownMenuItem>
                            ) : null}
                            {canEdit ? (
                              <DropdownMenuItem
                                className="text-[13px]"
                                variant="destructive"
                                onSelect={() => setDeleting(document)}
                              >
                                <Trash2Icon aria-hidden className="size-4" />
                                {labels.documents.delete}
                              </DropdownMenuItem>
                            ) : null}
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </div>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {derived.filteredTotal > 0 ? (
        <div className="text-muted-foreground flex items-center justify-between gap-2 text-xs">
          <span data-testid="knowledge-documents-page-info">
            {labels.documents.pageInfo(
              derived.page,
              derived.pageCount,
              derived.filteredTotal,
            )}
          </span>
          <div className="flex items-center gap-1.5">
            <Button
              className="h-8 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              size="sm"
              disabled={derived.page <= 1}
              onClick={() => setPage(derived.page - 1)}
            >
              <ChevronLeftIcon aria-hidden className="size-4" />
              {labels.documents.previousPage}
            </Button>
            <Button
              className="h-8 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              size="sm"
              disabled={derived.page >= derived.pageCount}
              onClick={() => setPage(derived.page + 1)}
            >
              {labels.documents.nextPage}
              <ChevronRightIcon aria-hidden className="size-4" />
            </Button>
          </div>
        </div>
      ) : null}

      {canEdit ? (
        <UploadDocumentDialog
          scope={scope}
          baseId={base.id}
          open={uploadOpen}
          onOpenChange={setUploadOpen}
        />
      ) : null}

      {renaming ? (
        <RenameDocumentDialog
          scope={scope}
          base={base}
          document={renaming}
          onClose={() => setRenaming(null)}
        />
      ) : null}

      {editingMetadataId !== null ? (
        <DocumentMetadataDialog
          scope={scope}
          base={base}
          document={items.find((item) => item.id === editingMetadataId) ?? null}
          onClose={() => setEditingMetadataId(null)}
        />
      ) : null}

      {batchMetadataOpen && selectedIds.length > 0 ? (
        <BatchMetadataDialog
          scope={scope}
          base={base}
          documents={derived.rows.filter((row) => selected.has(row.id))}
          onClose={() => setBatchMetadataOpen(false)}
        />
      ) : null}

      {reparsingId !== null ? (
        <ReparseDocumentDialog
          scope={scope}
          base={base}
          document={items.find((item) => item.id === reparsingId) ?? null}
          onClose={() => setReparsingId(null)}
        />
      ) : null}

      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) closeDeleteDialog();
        }}
      >
        <DialogContent className="border-border/80 rounded-xl text-[13px]">
          <DialogHeader>
            <DialogTitle className="text-base font-semibold">
              {labels.documents.deleteTitle}
            </DialogTitle>
            <DialogDescription className="text-[13px] leading-5">
              {deleting
                ? labels.documents.deleteDescription(deleting.name)
                : ""}
            </DialogDescription>
          </DialogHeader>
          {deleteDocument.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(deleteDocument.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              onClick={closeDeleteDialog}
            >
              {labels.common.cancel}
            </Button>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="destructive"
              disabled={deleteDocument.isPending}
              onClick={() => {
                if (!deleting) return;
                deleteDocument.mutate(
                  { documentId: deleting.id, baseId: base.id },
                  { onSuccess: () => closeDeleteDialog() },
                );
              }}
            >
              {deleteDocument.isPending
                ? labels.common.deleting
                : labels.common.delete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={batchDeleteOpen}
        onOpenChange={(open) => {
          if (!open) closeBatchDeleteDialog();
        }}
      >
        <DialogContent className="border-border/80 rounded-xl text-[13px]">
          <DialogHeader>
            <DialogTitle className="text-base font-semibold">
              {labels.documents.batchDeleteTitle}
            </DialogTitle>
            <DialogDescription className="text-[13px] leading-5">
              {labels.documents.batchDeleteDescription(selectedIds.length)}
            </DialogDescription>
          </DialogHeader>
          {batchDelete.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(batchDelete.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              onClick={closeBatchDeleteDialog}
            >
              {labels.common.cancel}
            </Button>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="destructive"
              disabled={batchDelete.isPending || selectedIds.length === 0}
              onClick={() =>
                batchDelete.mutate(
                  { documentIds: selectedIds, baseId: base.id },
                  {
                    onSuccess: () => {
                      setSelected(new Set());
                      closeBatchDeleteDialog();
                    },
                  },
                )
              }
            >
              {batchDelete.isPending
                ? labels.common.deleting
                : labels.common.delete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function RenameDocumentDialog({
  scope,
  base,
  document,
  onClose,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  document: KnowledgeDocumentItem;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const renameDocument = useRenameKnowledgeDocument(scope);
  const [name, setName] = useState(document.name);

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="border-border/80 rounded-xl text-[13px]">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">
            {labels.documents.renameTitle}
          </DialogTitle>
        </DialogHeader>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!name.trim()) return;
            renameDocument.mutate(
              {
                documentId: document.id,
                baseId: base.id,
                name: name.trim(),
              },
              { onSuccess: () => onClose() },
            );
          }}
        >
          <label className="grid gap-1.5 text-[13px]">
            <span className="font-medium">{labels.documents.renameLabel}</span>
            <Input
              className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
              value={name}
              required
              maxLength={255}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          {renameDocument.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(renameDocument.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              onClick={onClose}
            >
              {labels.common.cancel}
            </Button>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="submit"
              disabled={renameDocument.isPending || !name.trim()}
            >
              {renameDocument.isPending
                ? labels.common.saving
                : labels.common.save}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function epochToLocalInput(epoch: number): string {
  const date = new Date(epoch * 1000);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function localInputToEpoch(value: string): number | null {
  // datetime-local values parse as local time per the ES spec.
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? Math.round(ms / 1000) : null;
}

function metadataInputText(
  field: KnowledgeMetadataFieldItem,
  value: string | number | undefined,
): string {
  if (value === undefined) return "";
  if (field.field_type === "time" && typeof value === "number") {
    return epochToLocalInput(value);
  }
  return String(value);
}

/** Converts one input back to its API value; null clears, undefined is invalid. */
function metadataInputValue(
  field: KnowledgeMetadataFieldItem,
  text: string,
): string | number | null | undefined {
  if (text === "") return null;
  if (field.field_type === "string") return text;
  if (field.field_type === "number") {
    const parsed = Number.parseFloat(text);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return localInputToEpoch(text) ?? undefined;
}

function DocumentMetadataDialog({
  scope,
  base,
  document,
  onClose,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  document: KnowledgeDocumentItem | null;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const fields = useKnowledgeMetadataFields(scope, base.id);
  const setMetadata = useSetKnowledgeDocumentMetadata(scope);
  // Keyed by field name; entries appear lazily as the user edits so field
  // definitions can finish loading without wiping typed values.
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  // The row disappeared (deleted elsewhere): nothing to confirm against.
  useEffect(() => {
    if (document === null) onClose();
  }, [document, onClose]);

  if (document === null) return null;

  const fieldItems = fields.data ?? [];
  const textFor = (field: KnowledgeMetadataFieldItem): string =>
    drafts[field.name] ??
    metadataInputText(field, document.doc_metadata[field.name]);

  const invalidNames = fieldItems
    .filter((field) => metadataInputValue(field, textFor(field)) === undefined)
    .map((field) => field.name);

  const submit = () => {
    if (invalidNames.length > 0) return;
    const values: Record<string, string | number | null> = {};
    for (const field of fieldItems) {
      const next = metadataInputValue(field, textFor(field));
      if (next === undefined) return;
      const current = document.doc_metadata[field.name];
      if (next === null) {
        // Only clear keys that actually exist.
        if (current !== undefined) values[field.name] = null;
      } else if (next !== current) {
        values[field.name] = next;
      }
    }
    if (Object.keys(values).length === 0) {
      onClose();
      return;
    }
    setMetadata.mutate(
      { documentId: document.id, baseId: base.id, input: { values } },
      { onSuccess: () => onClose() },
    );
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="border-border/80 rounded-xl text-[13px]">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">
            {labels.documents.metadataTitle(document.name)}
          </DialogTitle>
          <DialogDescription className="text-[13px] leading-5">
            {labels.documents.metadataClearHint}
          </DialogDescription>
        </DialogHeader>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          {fields.isLoading ? (
            <Skeleton className="h-20 rounded-lg" />
          ) : fields.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(fields.error, labels.errors)}
            </p>
          ) : fieldItems.length === 0 ? (
            <p className="text-muted-foreground text-[13px]">
              {labels.documents.metadataEmpty}
            </p>
          ) : (
            fieldItems.map((field) => (
              <label key={field.id} className="grid gap-1.5 text-[13px]">
                <span className="font-medium">
                  {field.name}
                  <span className="text-muted-foreground ml-2 text-xs font-normal">
                    {
                      labels.metadata[
                        field.field_type === "string"
                          ? "typeString"
                          : field.field_type === "number"
                            ? "typeNumber"
                            : "typeTime"
                      ]
                    }
                  </span>
                </span>
                <Input
                  className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
                  type={
                    field.field_type === "number"
                      ? "number"
                      : field.field_type === "time"
                        ? "datetime-local"
                        : "text"
                  }
                  maxLength={field.field_type === "string" ? 500 : undefined}
                  step={field.field_type === "number" ? "any" : undefined}
                  value={textFor(field)}
                  aria-invalid={invalidNames.includes(field.name) || undefined}
                  onChange={(event) =>
                    setDrafts((current) => ({
                      ...current,
                      [field.name]: event.target.value,
                    }))
                  }
                />
              </label>
            ))
          )}
          {setMetadata.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(setMetadata.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              onClick={onClose}
            >
              {labels.common.cancel}
            </Button>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="submit"
              disabled={
                setMetadata.isPending ||
                fieldItems.length === 0 ||
                invalidNames.length > 0
              }
            >
              {setMetadata.isPending
                ? labels.common.saving
                : labels.common.save}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

type BatchMetadataMode = "keep" | "set" | "clear";

/**
 * One common patch across the current selection. Every field starts in
 * "keep"; only fields the user explicitly switches to set/clear enter the
 * submitted values, so an untouched field can never be wiped by accident.
 */
function BatchMetadataDialog({
  scope,
  base,
  documents,
  onClose,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  documents: KnowledgeDocumentItem[];
  onClose: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const fields = useKnowledgeMetadataFields(scope, base.id);
  const setMetadata = useSetKnowledgeDocumentsMetadata(scope);
  const [modes, setModes] = useState<Record<string, BatchMetadataMode>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const fieldItems = fields.data ?? [];
  const modeFor = (name: string): BatchMetadataMode => modes[name] ?? "keep";

  // Current values across the selection: one shared value pre-fills the set
  // input, mixed values are reported as a count instead of a fake blank.
  const currentSummary = (
    field: KnowledgeMetadataFieldItem,
  ): { shared: string | number | undefined; distinct: number } => {
    const seen = new Map<string, string | number | undefined>();
    for (const document of documents) {
      const value = document.doc_metadata[field.name];
      seen.set(JSON.stringify(value ?? null), value);
    }
    return {
      shared: seen.size === 1 ? [...seen.values()][0] : undefined,
      distinct: seen.size,
    };
  };

  const textFor = (field: KnowledgeMetadataFieldItem): string =>
    drafts[field.name] ??
    metadataInputText(field, currentSummary(field).shared);

  const invalidNames = fieldItems
    .filter((field) => {
      if (modeFor(field.name) !== "set") return false;
      const value = metadataInputValue(field, textFor(field));
      // In a batch "set", an empty value is not a clear — that intent has
      // its own mode — so it is simply invalid.
      return value === undefined || value === null;
    })
    .map((field) => field.name);

  const editedCount = fieldItems.filter(
    (field) => modeFor(field.name) !== "keep",
  ).length;

  const submit = () => {
    if (invalidNames.length > 0) return;
    const values: Record<string, string | number | null> = {};
    for (const field of fieldItems) {
      const mode = modeFor(field.name);
      if (mode === "clear") {
        values[field.name] = null;
      } else if (mode === "set") {
        const next = metadataInputValue(field, textFor(field));
        if (next === undefined || next === null) return;
        values[field.name] = next;
      }
    }
    if (Object.keys(values).length === 0) {
      onClose();
      return;
    }
    setMetadata.mutate(
      {
        baseId: base.id,
        input: {
          document_ids: documents.map((document) => document.id),
          values,
        },
      },
      { onSuccess: () => onClose() },
    );
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="border-border/80 max-h-[85vh] overflow-y-auto rounded-xl text-[13px]">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">
            {labels.documents.batchMetadataTitle(documents.length)}
          </DialogTitle>
          <DialogDescription className="text-[13px] leading-5">
            {labels.documents.batchMetadataDescription}
          </DialogDescription>
        </DialogHeader>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          {fields.isLoading ? (
            <Skeleton className="h-20 rounded-lg" />
          ) : fields.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(fields.error, labels.errors)}
            </p>
          ) : fieldItems.length === 0 ? (
            <p className="text-muted-foreground text-[13px]">
              {labels.documents.metadataEmpty}
            </p>
          ) : (
            fieldItems.map((field) => {
              const summary = currentSummary(field);
              const mode = modeFor(field.name);
              return (
                <div
                  key={field.id}
                  className="grid gap-1.5 text-[13px]"
                  data-testid="knowledge-batch-field"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">
                      {field.name}
                      <span className="text-muted-foreground ml-2 text-xs font-normal">
                        {
                          labels.metadata[
                            field.field_type === "string"
                              ? "typeString"
                              : field.field_type === "number"
                                ? "typeNumber"
                                : "typeTime"
                          ]
                        }
                      </span>
                    </span>
                    {summary.distinct > 1 ? (
                      <span className="text-muted-foreground text-xs">
                        {labels.documents.batchMetadataMixedValues(
                          summary.distinct,
                        )}
                      </span>
                    ) : null}
                  </div>
                  <div className="flex items-start gap-2">
                    <Select
                      value={mode}
                      onValueChange={(value) =>
                        setModes((current) => ({
                          ...current,
                          [field.name]: value as BatchMetadataMode,
                        }))
                      }
                    >
                      <SelectTrigger
                        className="border-input/80 bg-background w-28 shrink-0 rounded-lg text-[13px] shadow-none"
                        aria-label={`${field.name} mode`}
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent className="rounded-lg">
                        <SelectItem className="text-[13px]" value="keep">
                          {labels.documents.batchMetadataModeKeep}
                        </SelectItem>
                        <SelectItem className="text-[13px]" value="set">
                          {labels.documents.batchMetadataModeSet}
                        </SelectItem>
                        <SelectItem className="text-[13px]" value="clear">
                          {labels.documents.batchMetadataModeClear}
                        </SelectItem>
                      </SelectContent>
                    </Select>
                    {mode === "set" ? (
                      <Input
                        className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
                        type={
                          field.field_type === "number"
                            ? "number"
                            : field.field_type === "time"
                              ? "datetime-local"
                              : "text"
                        }
                        maxLength={
                          field.field_type === "string" ? 500 : undefined
                        }
                        step={field.field_type === "number" ? "any" : undefined}
                        value={textFor(field)}
                        aria-label={`${field.name} value`}
                        aria-invalid={
                          invalidNames.includes(field.name) || undefined
                        }
                        onChange={(event) =>
                          setDrafts((current) => ({
                            ...current,
                            [field.name]: event.target.value,
                          }))
                        }
                      />
                    ) : null}
                  </div>
                  {mode !== "keep" ? (
                    <p className="text-muted-foreground text-xs">
                      {labels.documents.batchMetadataOverwrite(
                        documents.length,
                      )}
                    </p>
                  ) : null}
                </div>
              );
            })
          )}
          {setMetadata.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(setMetadata.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              onClick={onClose}
            >
              {labels.common.cancel}
            </Button>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="submit"
              disabled={
                setMetadata.isPending ||
                fieldItems.length === 0 ||
                editedCount === 0 ||
                invalidNames.length > 0
              }
            >
              {setMetadata.isPending
                ? labels.common.saving
                : labels.common.save}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Explicit re-parse of the stored original file. The dialog reads the live
 * document row: a version conflict refreshes the authoritative data and the
 * user re-confirms against the new version, keeping their unsaved parameters.
 */
function ReparseDocumentDialog({
  scope,
  base,
  document,
  onClose,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  document: KnowledgeDocumentItem | null;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const queryClient = useQueryClient();
  const preview = usePreviewKnowledgeDocumentReparse(scope);
  const reparse = useReparseKnowledgeDocument(scope);
  const [chunkSize, setChunkSize] = useState(
    String(document?.chunk_size ?? 1000),
  );
  const [chunkOverlap, setChunkOverlap] = useState(
    String(document?.chunk_overlap ?? 100),
  );
  const [chunkSeparator, setChunkSeparator] = useState(
    document?.chunk_separator ?? DEFAULT_CHUNK_SEPARATOR,
  );
  const [chunkingMode, setChunkingMode] = useState<KnowledgeChunkingMode>(
    document?.chunking_mode ?? "general",
  );
  const [childChunkSize, setChildChunkSize] = useState(
    String(document?.child_chunk_size ?? 500),
  );
  const [childChunkSeparator, setChildChunkSeparator] = useState(
    document?.child_chunk_separator ?? DEFAULT_CHILD_CHUNK_SEPARATOR,
  );
  const [removeExtraSpaces, setRemoveExtraSpaces] = useState(
    document?.remove_extra_spaces ?? false,
  );
  const [removeUrlsEmails, setRemoveUrlsEmails] = useState(
    document?.remove_urls_emails ?? false,
  );
  // Sticky conflict notice: it must survive the authority refresh, which
  // resets the mutation error it would otherwise derive from.
  const [staleConflict, setStaleConflict] = useState(false);

  // The row disappeared (deleted elsewhere): nothing to confirm against.
  useEffect(() => {
    if (document === null) onClose();
  }, [document, onClose]);

  // A parameter edit or a version change invalidates the preview on screen —
  // it described a different reparse than the one now up for confirmation.
  const resetPreview = preview.reset;
  useEffect(() => {
    resetPreview();
  }, [
    chunkSize,
    chunkOverlap,
    chunkSeparator,
    chunkingMode,
    childChunkSize,
    childChunkSeparator,
    removeExtraSpaces,
    removeUrlsEmails,
    document?.version,
    resetPreview,
  ]);

  if (document === null) return null;

  const parsedChunkSize = Number.parseInt(chunkSize, 10);
  const parsedChunkOverlap = Number.parseInt(chunkOverlap, 10);
  const parsedChildChunkSize = Number.parseInt(childChunkSize, 10);
  const childParamsValid =
    chunkingMode === "general" ||
    (isChildChunkSizeValid(parsedChildChunkSize, parsedChunkSize) &&
      isChunkSeparatorValid(childChunkSeparator));
  const paramsValid =
    isChunkSizeValid(parsedChunkSize) &&
    isChunkOverlapValid(parsedChunkOverlap, parsedChunkSize) &&
    isChunkSeparatorValid(chunkSeparator) &&
    childParamsValid;

  const buildInput = (): KnowledgeReparseInput => ({
    expected_version: document.version,
    chunk_size: parsedChunkSize,
    chunk_overlap: parsedChunkOverlap,
    chunk_separator: chunkSeparator,
    remove_extra_spaces: removeExtraSpaces,
    remove_urls_emails: removeUrlsEmails,
    chunking_mode: chunkingMode,
    ...(chunkingMode === "parent_child"
      ? {
          child_chunk_size: parsedChildChunkSize,
          child_chunk_separator: childChunkSeparator,
        }
      : {}),
  });

  const conflict =
    isKnowledgeConflictError(reparse.error) ||
    isKnowledgeConflictError(preview.error) ||
    staleConflict;

  // A version conflict means the attempt was based on a stale row: refresh
  // the authority so the dialog re-reads the current version, and let the
  // user re-confirm with their edits intact. The sticky flag keeps the
  // notice visible after the refresh clears the mutation error behind it.
  const refreshStaleAuthority = (error: unknown) => {
    if (isKnowledgeConflictError(error)) {
      setStaleConflict(true);
      void queryClient.invalidateQueries({
        queryKey: knowledgeQueryKey(scope, "documents", "list", base.id),
      });
    }
  };

  const submit = () => {
    if (!paramsValid) return;
    setStaleConflict(false);
    reparse.mutate(
      { documentId: document.id, baseId: base.id, input: buildInput() },
      {
        onSuccess: () => onClose(),
        onError: refreshStaleAuthority,
      },
    );
  };

  const requestPreview = () => {
    setStaleConflict(false);
    preview.mutate(
      { documentId: document.id, input: buildInput() },
      { onError: refreshStaleAuthority },
    );
  };

  const previewData: KnowledgeReparsePreviewResponse | null =
    preview.data ?? null;

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="border-border/80 max-h-[85vh] overflow-y-auto rounded-xl text-[13px] sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">
            {labels.documents.reparseTitle(document.name)}
          </DialogTitle>
          <DialogDescription className="text-[13px] leading-5">
            {labels.documents.reparseWarning}
          </DialogDescription>
        </DialogHeader>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="grid gap-1.5 text-[13px]">
              <span className="font-medium">
                {labels.documents.chunkSizeLabel}
              </span>
              <Input
                className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
                type="number"
                min={KNOWLEDGE_CHUNK_SIZE_MIN}
                max={KNOWLEDGE_CHUNK_SIZE_MAX}
                value={chunkSize}
                aria-invalid={!isChunkSizeValid(parsedChunkSize) || undefined}
                onChange={(event) => setChunkSize(event.target.value)}
              />
            </label>
            <label className="grid gap-1.5 text-[13px]">
              <span className="font-medium">
                {labels.documents.chunkOverlapLabel}
              </span>
              <Input
                className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
                type="number"
                min={KNOWLEDGE_CHUNK_OVERLAP_MIN}
                max={KNOWLEDGE_CHUNK_OVERLAP_MAX}
                value={chunkOverlap}
                aria-invalid={
                  !isChunkOverlapValid(parsedChunkOverlap, parsedChunkSize) ||
                  undefined
                }
                onChange={(event) => setChunkOverlap(event.target.value)}
              />
            </label>
          </div>
          <label className="grid gap-1.5 text-[13px]">
            <span className="font-medium">
              {labels.documents.chunkSeparatorLabel}
            </span>
            <Input
              className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
              value={chunkSeparator}
              aria-invalid={!isChunkSeparatorValid(chunkSeparator) || undefined}
              onChange={(event) => setChunkSeparator(event.target.value)}
            />
          </label>
          <fieldset className="grid gap-2 text-[13px]">
            <legend className="font-medium">
              {labels.documents.chunkingModeLabel}
            </legend>
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="reparse-chunking-mode"
                className="accent-selection size-4"
                checked={chunkingMode === "general"}
                onChange={() => setChunkingMode("general")}
              />
              {labels.documents.chunkingModeGeneral}
            </label>
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="reparse-chunking-mode"
                className="accent-selection size-4"
                checked={chunkingMode === "parent_child"}
                onChange={() => setChunkingMode("parent_child")}
              />
              {labels.documents.chunkingModeParentChild}
            </label>
          </fieldset>
          {chunkingMode === "parent_child" ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="grid gap-1.5 text-[13px]">
                <span className="font-medium">
                  {labels.documents.childChunkSizeLabel}
                </span>
                <Input
                  className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
                  type="number"
                  min={KNOWLEDGE_CHILD_CHUNK_SIZE_MIN}
                  max={KNOWLEDGE_CHILD_CHUNK_SIZE_MAX}
                  value={childChunkSize}
                  aria-invalid={
                    !isChildChunkSizeValid(
                      parsedChildChunkSize,
                      parsedChunkSize,
                    ) || undefined
                  }
                  onChange={(event) => setChildChunkSize(event.target.value)}
                />
              </label>
              <label className="grid gap-1.5 text-[13px]">
                <span className="font-medium">
                  {labels.documents.childChunkSeparatorLabel}
                </span>
                <Input
                  className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
                  value={childChunkSeparator}
                  aria-invalid={
                    !isChunkSeparatorValid(childChunkSeparator) || undefined
                  }
                  onChange={(event) =>
                    setChildChunkSeparator(event.target.value)
                  }
                />
              </label>
            </div>
          ) : null}
          <div className="grid gap-2 text-[13px]">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                className="accent-selection size-4"
                checked={removeExtraSpaces}
                onChange={(event) => setRemoveExtraSpaces(event.target.checked)}
              />
              {labels.documents.removeExtraSpacesLabel}
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                className="accent-selection size-4"
                checked={removeUrlsEmails}
                onChange={(event) => setRemoveUrlsEmails(event.target.checked)}
              />
              {labels.documents.removeUrlsEmailsLabel}
            </label>
          </div>

          <div className="space-y-2">
            <Button
              className="h-8 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              size="sm"
              disabled={preview.isPending || !paramsValid}
              onClick={requestPreview}
            >
              {preview.isPending
                ? labels.wizard.previewLoading
                : labels.documents.reparsePreviewButton}
            </Button>
            {preview.error && !conflict ? (
              <p role="alert" className="text-destructive text-[13px]">
                {knowledgeErrorMessage(preview.error, labels.errors)}
              </p>
            ) : null}
            {previewData ? (
              <div
                className="border-border max-h-56 space-y-2 overflow-y-auto rounded-lg border p-3"
                data-testid="knowledge-reparse-preview"
              >
                <p className="text-muted-foreground text-xs">
                  {labels.documents.reparsePreviewShowing(
                    previewData.items.length,
                    previewData.total,
                  )}
                </p>
                {previewData.items.map((item) => (
                  <div key={item.position} className="space-y-1">
                    <p className="text-muted-foreground text-xs font-medium">
                      {labels.wizard.previewChunkLabel(item.position)} ·{" "}
                      {labels.wizard.previewCharacters(item.word_count)}
                      {item.child_contents.length > 0
                        ? ` · ${labels.wizard.previewChildCount(item.child_contents.length)}`
                        : ""}
                    </p>
                    <p className="line-clamp-3 text-xs break-words whitespace-pre-wrap">
                      {item.content}
                    </p>
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          {conflict ? (
            <p
              role="alert"
              className="text-destructive text-[13px]"
              data-testid="knowledge-reparse-conflict"
            >
              {labels.documents.reparseConflict}
            </p>
          ) : null}
          {reparse.error && !conflict ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(reparse.error, labels.errors)}
            </p>
          ) : null}

          <DialogFooter>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              onClick={onClose}
            >
              {labels.common.cancel}
            </Button>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="submit"
              variant="destructive"
              disabled={reparse.isPending || !paramsValid}
            >
              {reparse.isPending
                ? labels.documents.reparsePending
                : labels.documents.reparseSubmit}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

type UploadFileOutcome = {
  fileName: string;
  ok: boolean;
  message: string;
};

function UploadDocumentDialog({
  scope,
  baseId,
  open,
  onOpenChange,
}: {
  scope: ProjectClientScope;
  baseId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const upload = useUploadKnowledgeDocument(scope);
  const [files, setFiles] = useState<File[]>([]);
  const [displayName, setDisplayName] = useState("");
  const [chunkSize, setChunkSize] = useState("1000");
  const [chunkOverlap, setChunkOverlap] = useState("100");
  const [chunkSeparator, setChunkSeparator] = useState(DEFAULT_CHUNK_SEPARATOR);
  const [chunkingMode, setChunkingMode] =
    useState<KnowledgeChunkingMode>("general");
  const [childChunkSize, setChildChunkSize] = useState("500");
  const [childChunkSeparator, setChildChunkSeparator] = useState(
    DEFAULT_CHILD_CHUNK_SEPARATOR,
  );
  const [removeExtraSpaces, setRemoveExtraSpaces] = useState(false);
  const [removeUrlsEmails, setRemoveUrlsEmails] = useState(false);
  const [outcomes, setOutcomes] = useState<UploadFileOutcome[]>([]);
  const [uploadingIndex, setUploadingIndex] = useState<number | null>(null);
  const [fileInputKey, setFileInputKey] = useState(0);

  const close = (nextOpen: boolean) => {
    if (uploadingIndex !== null) return;
    onOpenChange(nextOpen);
    if (!nextOpen) {
      setFiles([]);
      setDisplayName("");
      setChunkSize("1000");
      setChunkOverlap("100");
      setChunkSeparator(DEFAULT_CHUNK_SEPARATOR);
      setChunkingMode("general");
      setChildChunkSize("500");
      setChildChunkSeparator(DEFAULT_CHILD_CHUNK_SEPARATOR);
      setRemoveExtraSpaces(false);
      setRemoveUrlsEmails(false);
      setOutcomes([]);
      setFileInputKey((key) => key + 1);
      upload.reset();
    }
  };

  const parsedChunkSize = Number.parseInt(chunkSize, 10);
  const parsedChunkOverlap = Number.parseInt(chunkOverlap, 10);
  const parsedChildChunkSize = Number.parseInt(childChunkSize, 10);
  const childParamsValid =
    chunkingMode === "general" ||
    (isChildChunkSizeValid(parsedChildChunkSize, parsedChunkSize) &&
      isChunkSeparatorValid(childChunkSeparator));
  // Bounds mirror the backend exactly so an out-of-range value cannot reach
  // a doomed upload request.
  const chunkParamsValid =
    isChunkSizeValid(parsedChunkSize) &&
    isChunkOverlapValid(parsedChunkOverlap, parsedChunkSize) &&
    isChunkSeparatorValid(chunkSeparator) &&
    childParamsValid;

  const startUpload = async () => {
    if (files.length === 0 || !chunkParamsValid) return;
    setOutcomes([]);
    const collected: UploadFileOutcome[] = [];
    const failedFiles: File[] = [];
    for (const [index, file] of files.entries()) {
      setUploadingIndex(index);
      try {
        // Files upload one by one so each gets its own verdict; the custom
        // display name only makes sense for a single file.
        await upload.mutateAsync({
          baseId,
          input: {
            file,
            name: files.length === 1 ? displayName : "",
            chunk_size: parsedChunkSize,
            chunk_overlap: parsedChunkOverlap,
            chunk_separator: chunkSeparator,
            remove_extra_spaces: removeExtraSpaces,
            remove_urls_emails: removeUrlsEmails,
            chunking_mode: chunkingMode,
            ...(chunkingMode === "parent_child"
              ? {
                  child_chunk_size: parsedChildChunkSize,
                  child_chunk_separator: childChunkSeparator,
                }
              : {}),
          },
        });
        collected.push({
          fileName: file.name,
          ok: true,
          message: labels.documents.uploadResultSuccess(file.name),
        });
      } catch (error) {
        failedFiles.push(file);
        collected.push({
          fileName: file.name,
          ok: false,
          message: labels.documents.uploadResultFailed(
            file.name,
            knowledgeErrorMessage(error, labels.errors),
          ),
        });
      }
      setOutcomes([...collected]);
    }
    setUploadingIndex(null);
    if (failedFiles.length === 0) {
      onOpenChange(false);
      setFiles([]);
      setDisplayName("");
      setOutcomes([]);
      setFileInputKey((key) => key + 1);
      upload.reset();
    } else {
      // Retrying must not re-upload the files that already succeeded.
      setFiles(failedFiles);
      setFileInputKey((key) => key + 1);
    }
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      {/* Parent-child mode adds enough fields to outgrow small viewports. */}
      <DialogContent className="border-border/80 max-h-[85vh] overflow-y-auto rounded-xl text-[13px]">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">
            {labels.documents.uploadTitle}
          </DialogTitle>
          <DialogDescription className="text-[13px] leading-5">
            {labels.documents.uploadDescription}
          </DialogDescription>
        </DialogHeader>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            void startUpload();
          }}
        >
          <label className="grid gap-1.5 text-[13px]">
            <span className="font-medium">{labels.documents.fileLabel}</span>
            <Input
              className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
              key={fileInputKey}
              type="file"
              // After a partial failure the input is cleared while the failed
              // files stay queued in state, so it must not block resubmission.
              required={files.length === 0}
              multiple
              accept={KNOWLEDGE_UPLOAD_ACCEPT}
              onChange={(event) =>
                setFiles(Array.from(event.target.files ?? []))
              }
            />
          </label>
          {files.length <= 1 ? (
            <label className="grid gap-1.5 text-[13px]">
              <span className="font-medium">
                {labels.documents.displayNameLabel}
              </span>
              <Input
                className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
                value={displayName}
                maxLength={200}
                placeholder={labels.documents.displayNamePlaceholder}
                onChange={(event) => setDisplayName(event.target.value)}
              />
            </label>
          ) : null}
          <fieldset className="grid gap-2 text-[13px]">
            <legend className="font-medium">
              {labels.documents.chunkingModeLabel}
            </legend>
            <div className="flex flex-wrap gap-4">
              {(
                [
                  ["general", labels.documents.chunkingModeGeneral],
                  ["parent_child", labels.documents.chunkingModeParentChild],
                ] as const
              ).map(([mode, label]) => (
                <label key={mode} className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="upload-chunking-mode"
                    value={mode}
                    className="accent-selection size-4"
                    checked={chunkingMode === mode}
                    onChange={() => setChunkingMode(mode)}
                  />
                  {label}
                </label>
              ))}
            </div>
            {chunkingMode === "parent_child" ? (
              <p className="text-muted-foreground text-xs">
                {labels.documents.chunkingModeParentChildHint}
              </p>
            ) : null}
          </fieldset>
          <div className="grid grid-cols-2 gap-3">
            <label className="grid gap-1.5 text-[13px]">
              <span className="font-medium">
                {labels.documents.chunkSizeLabel}
              </span>
              <Input
                className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
                type="number"
                min={KNOWLEDGE_CHUNK_SIZE_MIN}
                max={KNOWLEDGE_CHUNK_SIZE_MAX}
                required
                value={chunkSize}
                onChange={(event) => setChunkSize(event.target.value)}
              />
              <span className="text-muted-foreground text-xs">
                {labels.documents.chunkSizeHint}
              </span>
            </label>
            <label className="grid gap-1.5 text-[13px]">
              <span className="font-medium">
                {labels.documents.chunkOverlapLabel}
              </span>
              <Input
                className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
                type="number"
                min={KNOWLEDGE_CHUNK_OVERLAP_MIN}
                max={KNOWLEDGE_CHUNK_OVERLAP_MAX}
                required
                value={chunkOverlap}
                onChange={(event) => setChunkOverlap(event.target.value)}
              />
              <span className="text-muted-foreground text-xs">
                {labels.documents.chunkOverlapHint}
              </span>
            </label>
          </div>
          <label className="grid gap-1.5 text-[13px]">
            <span className="font-medium">
              {labels.documents.chunkSeparatorLabel}
            </span>
            <Input
              className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
              required
              maxLength={64}
              value={chunkSeparator}
              onChange={(event) => setChunkSeparator(event.target.value)}
            />
            <span className="text-muted-foreground text-xs">
              {labels.documents.chunkSeparatorHint}
            </span>
          </label>
          {chunkingMode === "parent_child" ? (
            <div className="grid grid-cols-2 gap-3">
              <label className="grid gap-1.5 text-[13px]">
                <span className="font-medium">
                  {labels.documents.childChunkSizeLabel}
                </span>
                <Input
                  className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
                  type="number"
                  min={KNOWLEDGE_CHILD_CHUNK_SIZE_MIN}
                  max={KNOWLEDGE_CHILD_CHUNK_SIZE_MAX}
                  required
                  value={childChunkSize}
                  onChange={(event) => setChildChunkSize(event.target.value)}
                />
              </label>
              <label className="grid gap-1.5 text-[13px]">
                <span className="font-medium">
                  {labels.documents.childChunkSeparatorLabel}
                </span>
                <Input
                  className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
                  required
                  maxLength={64}
                  value={childChunkSeparator}
                  onChange={(event) =>
                    setChildChunkSeparator(event.target.value)
                  }
                />
                <span className="text-muted-foreground text-xs">
                  {labels.documents.childChunkSeparatorHint}
                </span>
              </label>
            </div>
          ) : null}
          <fieldset className="grid gap-2 text-[13px]">
            <legend className="font-medium">
              {labels.documents.preprocessingLabel}
            </legend>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                className="accent-selection size-4"
                checked={removeExtraSpaces}
                onChange={(event) => setRemoveExtraSpaces(event.target.checked)}
              />
              {labels.documents.removeExtraSpacesLabel}
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                className="accent-selection size-4"
                checked={removeUrlsEmails}
                onChange={(event) => setRemoveUrlsEmails(event.target.checked)}
              />
              {labels.documents.removeUrlsEmailsLabel}
            </label>
          </fieldset>
          <p className="text-muted-foreground text-xs">
            {labels.documents.chunkImmutableNote}
          </p>
          {outcomes.length > 0 ? (
            <ul className="grid gap-1 text-xs" data-testid="upload-outcomes">
              {outcomes.map((outcome) => (
                <li
                  key={outcome.fileName}
                  className={
                    outcome.ok ? "text-muted-foreground" : "text-destructive"
                  }
                >
                  {outcome.message}
                </li>
              ))}
            </ul>
          ) : null}
          <DialogFooter>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              disabled={uploadingIndex !== null}
              onClick={() => close(false)}
            >
              {labels.common.cancel}
            </Button>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="submit"
              disabled={
                uploadingIndex !== null ||
                files.length === 0 ||
                !chunkParamsValid
              }
            >
              {uploadingIndex !== null
                ? files.length > 1
                  ? labels.documents.uploadingProgress(
                      uploadingIndex + 1,
                      files.length,
                    )
                  : labels.documents.uploading
                : labels.documents.upload}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
