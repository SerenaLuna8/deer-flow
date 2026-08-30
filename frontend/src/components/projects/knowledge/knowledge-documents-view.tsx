"use client";

import {
  DownloadIcon,
  ListTreeIcon,
  MoreHorizontalIcon,
  PencilIcon,
  RotateCcwIcon,
  TagsIcon,
  Trash2Icon,
  UploadIcon,
} from "lucide-react";
import { useState } from "react";

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
  useDeleteKnowledgeDocument,
  useDeleteKnowledgeDocuments,
  useKnowledgeDocuments,
  useKnowledgeMetadataFields,
  useRenameKnowledgeDocument,
  useRetryKnowledgeDocument,
  useSetKnowledgeDocumentMetadata,
  useSetKnowledgeDocumentsEnabled,
  useUploadKnowledgeDocument,
} from "@/core/knowledge/hooks";
import {
  DEFAULT_CHILD_CHUNK_SEPARATOR,
  DEFAULT_CHUNK_SEPARATOR,
  type KnowledgeBaseItem,
  type KnowledgeChunkingMode,
  type KnowledgeDocumentItem,
  type KnowledgeDocumentStatus,
  type KnowledgeMetadataFieldItem,
} from "@/core/knowledge/types";
import type { ProjectClientScope } from "@/core/private-work/types";

import { knowledgeErrorMessage } from "./knowledge-error";
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

export function KnowledgeDocumentsView({
  scope,
  base,
  canEdit,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  canEdit: boolean;
}) {
  const documents = useKnowledgeDocuments(scope, base.id);
  const [browsingId, setBrowsingId] = useState<string | null>(null);

  // Resolved from the live list so segment mutations (which invalidate the
  // documents query) keep the header counts fresh while browsing.
  const browsing =
    browsingId === null
      ? null
      : (documents.data?.items.find((item) => item.id === browsingId) ?? null);

  if (browsing) {
    return (
      <KnowledgeSegmentsBrowser
        scope={scope}
        base={base}
        document={browsing}
        canEdit={canEdit}
        onBack={() => setBrowsingId(null)}
      />
    );
  }

  return (
    <DocumentsTable
      scope={scope}
      base={base}
      canEdit={canEdit}
      documents={documents}
      onBrowse={(document) => setBrowsingId(document.id)}
    />
  );
}

function DocumentsTable({
  scope,
  base,
  canEdit,
  documents,
  onBrowse,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  canEdit: boolean;
  documents: ReturnType<typeof useKnowledgeDocuments>;
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
  const [editingMetadata, setEditingMetadata] =
    useState<KnowledgeDocumentItem | null>(null);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);

  const items = documents.data?.items ?? [];
  // Deleting documents reject batch operations server-side, so they are not
  // selectable; stale ids from removed rows drop out here as well.
  const selectableIds = items
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
        <h2 className="truncate text-lg font-semibold">
          {labels.detail.documents}
        </h2>
        {canEdit ? (
          <Button type="button" onClick={() => setUploadOpen(true)}>
            <UploadIcon aria-hidden className="size-4" />
            {labels.documents.uploadButton}
          </Button>
        ) : null}
      </div>

      {retryDocument.error ? (
        <p role="alert" className="text-destructive text-sm">
          {knowledgeErrorMessage(retryDocument.error, labels.errors)}
        </p>
      ) : null}
      {toggleDocuments.error ? (
        <p role="alert" className="text-destructive text-sm">
          {knowledgeErrorMessage(toggleDocuments.error, labels.errors)}
        </p>
      ) : null}
      {recoverableRefreshError ? (
        <div
          role="alert"
          className="border-destructive/30 bg-destructive/5 flex flex-wrap items-center gap-3 rounded-xl border px-4 py-3 text-sm"
        >
          <span className="text-destructive min-w-0 flex-1">
            {knowledgeErrorMessage(documents.error, labels.errors)}
          </span>
          <Button
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

      {canEdit && selectedIds.length > 0 ? (
        <div
          data-testid="knowledge-batch-bar"
          className="border-border bg-muted/50 flex flex-wrap items-center gap-2 rounded-xl border px-4 py-2 text-sm"
        >
          <span className="font-medium">
            {labels.documents.selectedCount(selectedIds.length)}
          </span>
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            <Button
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
              type="button"
              variant="outline"
              size="sm"
              className="text-destructive"
              disabled={batchPending}
              onClick={() => setBatchDeleteOpen(true)}
            >
              {labels.documents.batchDelete}
            </Button>
            <Button
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
        <p role="alert" className="text-destructive text-sm">
          {knowledgeErrorMessage(documents.error, labels.errors)}
        </p>
      ) : items.length === 0 ? (
        <p className="text-muted-foreground rounded-xl border border-dashed px-4 py-10 text-center text-sm">
          {labels.documents.empty}
        </p>
      ) : (
        <div
          className="border-border overflow-x-auto rounded-xl border"
          data-testid="knowledge-documents-table"
        >
          <table className="w-full min-w-[680px] table-fixed text-left text-sm">
            <thead className="bg-muted/60">
              <tr>
                {canEdit ? (
                  <th className="w-10 px-3 py-3">
                    <input
                      type="checkbox"
                      className="accent-primary size-4 align-middle"
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
                <th className="w-36 px-3 py-3">
                  {labels.documents.columns.name}
                </th>
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
              {items.map((document) => (
                <tr key={document.id} className="group border-t align-top">
                  {canEdit ? (
                    <td className="px-3 py-3">
                      <input
                        type="checkbox"
                        className="accent-primary size-4 align-middle"
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
                  </td>
                  <td className="px-3 py-3">
                    <Badge variant={documentStatusVariant(document.status)}>
                      {labels.documentStatus[document.status]}
                    </Badge>
                    {document.status === "failed" && document.error_message ? (
                      <DocumentErrorMessage message={document.error_message} />
                    ) : null}
                    {document.delete_error ? (
                      <DocumentErrorMessage message={document.delete_error} />
                    ) : null}
                  </td>
                  <td className="px-3 py-3">
                    <Switch
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
                  <td className="bg-background group-hover:bg-muted/20 sticky right-0 z-10 px-2 py-2.5 shadow-[-8px_0_12px_-12px_rgba(0,0,0,0.45)]">
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
                              className="size-8"
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
                          <DropdownMenuContent align="end" className="w-48">
                            {document.status === "ready" ? (
                              <DropdownMenuItem
                                onSelect={() => onBrowse(document)}
                              >
                                <ListTreeIcon aria-hidden className="size-4" />
                                {labels.documents.viewSegments}
                              </DropdownMenuItem>
                            ) : null}
                            {document.status !== "uploading" &&
                            document.status !== "deleting" ? (
                              <DropdownMenuItem asChild>
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
                                onSelect={() => setRenaming(document)}
                              >
                                <PencilIcon aria-hidden className="size-4" />
                                {labels.documents.rename}
                              </DropdownMenuItem>
                            ) : null}
                            {canEdit && document.status !== "deleting" ? (
                              <DropdownMenuItem
                                onSelect={() => setEditingMetadata(document)}
                              >
                                <TagsIcon aria-hidden className="size-4" />
                                {labels.documents.metadataAction}
                              </DropdownMenuItem>
                            ) : null}
                            {canEdit ? (
                              <DropdownMenuItem
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

      {editingMetadata ? (
        <DocumentMetadataDialog
          scope={scope}
          base={base}
          document={editingMetadata}
          onClose={() => setEditingMetadata(null)}
        />
      ) : null}

      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) closeDeleteDialog();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{labels.documents.deleteTitle}</DialogTitle>
            <DialogDescription>
              {deleting
                ? labels.documents.deleteDescription(deleting.name)
                : ""}
            </DialogDescription>
          </DialogHeader>
          {deleteDocument.error ? (
            <p role="alert" className="text-destructive text-sm">
              {knowledgeErrorMessage(deleteDocument.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={closeDeleteDialog}>
              {labels.common.cancel}
            </Button>
            <Button
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
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{labels.documents.batchDeleteTitle}</DialogTitle>
            <DialogDescription>
              {labels.documents.batchDeleteDescription(selectedIds.length)}
            </DialogDescription>
          </DialogHeader>
          {batchDelete.error ? (
            <p role="alert" className="text-destructive text-sm">
              {knowledgeErrorMessage(batchDelete.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={closeBatchDeleteDialog}
            >
              {labels.common.cancel}
            </Button>
            <Button
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
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{labels.documents.renameTitle}</DialogTitle>
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
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{labels.documents.renameLabel}</span>
            <Input
              value={name}
              required
              maxLength={255}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          {renameDocument.error ? (
            <p role="alert" className="text-destructive text-sm">
              {knowledgeErrorMessage(renameDocument.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {labels.common.cancel}
            </Button>
            <Button
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
  document: KnowledgeDocumentItem;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const fields = useKnowledgeMetadataFields(scope, base.id);
  const setMetadata = useSetKnowledgeDocumentMetadata(scope);
  // Keyed by field name; entries appear lazily as the user edits so field
  // definitions can finish loading without wiping typed values.
  const [drafts, setDrafts] = useState<Record<string, string>>({});

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
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {labels.documents.metadataTitle(document.name)}
          </DialogTitle>
          <DialogDescription>
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
            <p role="alert" className="text-destructive text-sm">
              {knowledgeErrorMessage(fields.error, labels.errors)}
            </p>
          ) : fieldItems.length === 0 ? (
            <p className="text-muted-foreground text-sm">
              {labels.documents.metadataEmpty}
            </p>
          ) : (
            fieldItems.map((field) => (
              <label key={field.id} className="grid gap-1.5 text-sm">
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
            <p role="alert" className="text-destructive text-sm">
              {knowledgeErrorMessage(setMetadata.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {labels.common.cancel}
            </Button>
            <Button
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
      <DialogContent className="max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{labels.documents.uploadTitle}</DialogTitle>
          <DialogDescription>
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
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">{labels.documents.fileLabel}</span>
            <Input
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
            <label className="grid gap-1.5 text-sm">
              <span className="font-medium">
                {labels.documents.displayNameLabel}
              </span>
              <Input
                value={displayName}
                maxLength={200}
                placeholder={labels.documents.displayNamePlaceholder}
                onChange={(event) => setDisplayName(event.target.value)}
              />
            </label>
          ) : null}
          <fieldset className="grid gap-2 text-sm">
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
                    className="accent-primary size-4"
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
            <label className="grid gap-1.5 text-sm">
              <span className="font-medium">
                {labels.documents.chunkSizeLabel}
              </span>
              <Input
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
            <label className="grid gap-1.5 text-sm">
              <span className="font-medium">
                {labels.documents.chunkOverlapLabel}
              </span>
              <Input
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
          <label className="grid gap-1.5 text-sm">
            <span className="font-medium">
              {labels.documents.chunkSeparatorLabel}
            </span>
            <Input
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
              <label className="grid gap-1.5 text-sm">
                <span className="font-medium">
                  {labels.documents.childChunkSizeLabel}
                </span>
                <Input
                  type="number"
                  min={KNOWLEDGE_CHILD_CHUNK_SIZE_MIN}
                  max={KNOWLEDGE_CHILD_CHUNK_SIZE_MAX}
                  required
                  value={childChunkSize}
                  onChange={(event) => setChildChunkSize(event.target.value)}
                />
              </label>
              <label className="grid gap-1.5 text-sm">
                <span className="font-medium">
                  {labels.documents.childChunkSeparatorLabel}
                </span>
                <Input
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
          <fieldset className="grid gap-2 text-sm">
            <legend className="font-medium">
              {labels.documents.preprocessingLabel}
            </legend>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                className="accent-primary size-4"
                checked={removeExtraSpaces}
                onChange={(event) => setRemoveExtraSpaces(event.target.checked)}
              />
              {labels.documents.removeExtraSpacesLabel}
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                className="accent-primary size-4"
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
              type="button"
              variant="outline"
              disabled={uploadingIndex !== null}
              onClick={() => close(false)}
            >
              {labels.common.cancel}
            </Button>
            <Button
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
