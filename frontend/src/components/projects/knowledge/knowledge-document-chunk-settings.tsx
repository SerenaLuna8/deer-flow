"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeftIcon, RefreshCwIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import { isKnowledgeConflictError } from "@/core/knowledge/api";
import {
  isChildChunkSizeValid,
  isChunkOverlapValid,
  isChunkSeparatorValid,
  isChunkSizeValid,
} from "@/core/knowledge/chunk-settings";
import {
  useKnowledgeDocuments,
  useKnowledgeFileCapabilities,
  usePreviewKnowledgeDocumentReparse,
  useReparseKnowledgeDocument,
} from "@/core/knowledge/hooks";
import { knowledgeQueryKey } from "@/core/knowledge/query-keys";
import type {
  KnowledgeBaseItem,
  KnowledgeDocumentItem,
  KnowledgeReparseInput,
  KnowledgeReparsePreviewResponse,
} from "@/core/knowledge/types";
import type { ProjectClientScope } from "@/core/private-work/types";
import { cn } from "@/lib/utils";

import { KnowledgeBaseConfigurationSummary } from "./knowledge-base-configuration-summary";
import { KnowledgeChunkPreviewList } from "./knowledge-chunk-preview-list";
import {
  KnowledgeChunkSettingsFields,
  type KnowledgeChunkSettingsDraft,
} from "./knowledge-chunk-settings-fields";
import { knowledgeErrorMessage } from "./knowledge-error";
import { KnowledgeFileTypeIcon } from "./knowledge-file-type-icon";

/**
 * A document's chunk settings page: the upload wizard's "chunking & model"
 * step applied to one stored original file. Confirming is an explicit
 * reparse — every segment is replaced — so the page carries that warning,
 * previews the split server-side, and pins `expected_version` so a stale
 * confirmation conflicts instead of overwriting a newer generation.
 *
 * The page resolves its document from the live list: a row that disappears
 * (deleted elsewhere) shows "inaccessible" rather than a cached snapshot.
 */
export function KnowledgeDocumentChunkSettings({
  scope,
  base,
  documentId,
  onExit,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  documentId: string;
  onExit: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const documents = useKnowledgeDocuments(scope, base.id);
  const document =
    documents.data?.items.find((item) => item.id === documentId) ?? null;

  if (documents.data === undefined) {
    if (documents.error === null) {
      return <Skeleton className="h-40 rounded-xl" />;
    }
    return (
      <div className="space-y-4">
        <p role="alert" className="text-destructive text-[13px]">
          {knowledgeErrorMessage(documents.error, labels.errors)}
        </p>
        <Button
          type="button"
          variant="outline"
          className="h-9 rounded-lg text-[13px] shadow-none"
          onClick={onExit}
        >
          {labels.documents.backToList}
        </Button>
      </div>
    );
  }

  if (document === null) {
    return (
      <div className="rounded-xl border border-dashed px-4 py-12 text-center">
        <p className="text-muted-foreground text-[13px]">
          {labels.documents.notFound}
        </p>
        <Button
          type="button"
          variant="outline"
          className="mt-4 h-9 rounded-lg text-[13px] shadow-none"
          onClick={onExit}
        >
          {labels.documents.backToList}
        </Button>
      </div>
    );
  }

  return (
    <DocumentChunkSettingsForm
      scope={scope}
      base={base}
      document={document}
      onExit={onExit}
    />
  );
}

/**
 * Identity of one server-side preview: the exact parameter snapshot it was
 * computed for (including the pinned version). A newer request or an edit
 * that changes the snapshot makes an older response stale; a replaced
 * in-flight request is cancelled and never published.
 */
type ReparsePreviewState =
  | { status: "idle" }
  | { status: "loading"; input: KnowledgeReparseInput; sequence: number }
  | { status: "cancelled"; input: KnowledgeReparseInput }
  | {
      status: "success";
      input: KnowledgeReparseInput;
      data: KnowledgeReparsePreviewResponse;
    }
  | { status: "error"; input: KnowledgeReparseInput; error: unknown };

function reparseInputEqual(
  left: KnowledgeReparseInput,
  right: KnowledgeReparseInput,
): boolean {
  return (
    left.expected_version === right.expected_version &&
    left.chunk_size === right.chunk_size &&
    left.chunk_overlap === right.chunk_overlap &&
    left.chunk_separator === right.chunk_separator &&
    left.remove_extra_spaces === right.remove_extra_spaces &&
    left.remove_urls_emails === right.remove_urls_emails &&
    left.chunking_mode === right.chunking_mode &&
    left.child_chunk_size === right.child_chunk_size &&
    left.child_chunk_separator === right.child_chunk_separator
  );
}

function DocumentChunkSettingsForm({
  scope,
  base,
  document,
  onExit,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  document: KnowledgeDocumentItem;
  onExit: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const wizard = labels.wizard;
  const queryClient = useQueryClient();
  const capabilities = useKnowledgeFileCapabilities(scope);
  const preview = usePreviewKnowledgeDocumentReparse(scope);
  const reparse = useReparseKnowledgeDocument(scope);
  // The base fixes the mode for every document; a document still on another
  // mode (admitted before a base-wide switch) is pre-filled with the base's,
  // so confirming brings it back in line.
  const lockedChunkingMode = base.chunking_mode ?? document.chunking_mode;
  // The form starts from the document's frozen parameters. It is keyed by
  // document id, not version: a conflict refreshes the authoritative row
  // while the user's unsaved edits stay in place for re-confirmation.
  const [draft, setDraft] = useState<KnowledgeChunkSettingsDraft>(() => ({
    chunkSize: String(document.chunk_size),
    chunkOverlap: String(document.chunk_overlap),
    chunkSeparator: document.chunk_separator,
    chunkingMode: lockedChunkingMode,
    childChunkSize: String(document.child_chunk_size),
    childChunkSeparator: document.child_chunk_separator,
    removeExtraSpaces: document.remove_extra_spaces,
    removeUrlsEmails: document.remove_urls_emails,
  }));
  useEffect(() => {
    setDraft((current) =>
      current.chunkingMode === lockedChunkingMode
        ? current
        : { ...current, chunkingMode: lockedChunkingMode },
    );
  }, [lockedChunkingMode]);
  const [previewState, setPreviewState] = useState<ReparsePreviewState>({
    status: "idle",
  });
  // Sticky conflict notice: survives the authority refresh that follows it.
  const [staleConflict, setStaleConflict] = useState(false);
  const previewSequenceRef = useRef(0);
  const previewAbortRef = useRef<AbortController | null>(null);
  const autoPreviewedRef = useRef(false);
  const scopeKey = `${scope.accountId}:${scope.projectId}:${document.id}`;

  const chunkLimits = capabilities.data?.chunk_limits;
  const parsedChunkSize = Number.parseInt(draft.chunkSize, 10);
  const parsedChunkOverlap = Number.parseInt(draft.chunkOverlap, 10);
  const parsedChildChunkSize = Number.parseInt(draft.childChunkSize, 10);
  const paramsValid =
    isChunkSizeValid(parsedChunkSize, chunkLimits) &&
    isChunkOverlapValid(parsedChunkOverlap, parsedChunkSize, chunkLimits) &&
    isChunkSeparatorValid(draft.chunkSeparator) &&
    (draft.chunkingMode === "general" ||
      (isChildChunkSizeValid(
        parsedChildChunkSize,
        parsedChunkSize,
        chunkLimits,
      ) &&
        isChunkSeparatorValid(draft.childChunkSeparator)));

  const currentInput: KnowledgeReparseInput | null = paramsValid
    ? {
        expected_version: document.version,
        chunk_size: parsedChunkSize,
        chunk_overlap: parsedChunkOverlap,
        chunk_separator: draft.chunkSeparator,
        remove_extra_spaces: draft.removeExtraSpaces,
        remove_urls_emails: draft.removeUrlsEmails,
        chunking_mode: draft.chunkingMode,
        ...(draft.chunkingMode === "parent_child"
          ? {
              child_chunk_size: parsedChildChunkSize,
              child_chunk_separator: draft.childChunkSeparator,
            }
          : {}),
      }
    : null;

  const previewInput =
    previewState.status === "idle" ? null : previewState.input;
  // A preview describes exactly one snapshot; any edit (or a version bump
  // after a conflict) retires it until the user explicitly refreshes.
  const previewIsStale =
    previewInput !== null &&
    (currentInput === null || !reparseInputEqual(previewInput, currentInput));
  const previewLoading = previewState.status === "loading" && !previewIsStale;
  const visiblePreviewData =
    previewState.status === "success" ? previewState.data : null;
  const visiblePreviewError =
    previewState.status === "error" &&
    !previewIsStale &&
    !isKnowledgeConflictError(previewState.error)
      ? previewState.error
      : null;

  // A version conflict means the attempt was based on a stale row: refresh
  // the authority so the page re-reads the current version, and let the
  // user re-confirm with their edits intact.
  const refreshStaleAuthority = (error: unknown) => {
    if (isKnowledgeConflictError(error)) {
      setStaleConflict(true);
      void queryClient.invalidateQueries({
        queryKey: knowledgeQueryKey(scope, "documents", "list", base.id),
      });
    }
  };

  const requestPreview = () => {
    if (currentInput === null) return;
    previewAbortRef.current?.abort();
    const controller = new AbortController();
    previewAbortRef.current = controller;
    const sequence = previewSequenceRef.current + 1;
    previewSequenceRef.current = sequence;
    const input = currentInput;
    setStaleConflict(false);
    setPreviewState({ status: "loading", input, sequence });
    preview
      .mutateAsync({
        documentId: document.id,
        input,
        signal: controller.signal,
      })
      .then(
        (data) => {
          if (previewSequenceRef.current !== sequence) return;
          setPreviewState({ status: "success", input, data });
        },
        (error: unknown) => {
          if (controller.signal.aborted) {
            // Cancelled (edited away or torn down): settle only our own
            // pending state; a newer request may already own the panel.
            setPreviewState((state) =>
              state.status === "loading" && state.sequence === sequence
                ? { status: "cancelled", input }
                : state,
            );
            return;
          }
          if (previewSequenceRef.current !== sequence) return;
          setPreviewState({ status: "error", input, error });
          refreshStaleAuthority(error);
        },
      );
  };

  // A stale in-flight request can never be published, so stop waiting on it.
  useEffect(() => {
    if (previewIsStale) {
      previewAbortRef.current?.abort();
      previewAbortRef.current = null;
    }
  }, [previewIsStale]);

  // Teardown abandons any in-flight request. Re-arming the automatic preview
  // keeps strict mode's simulated remount from leaving a blank panel.
  useEffect(
    () => () => {
      previewAbortRef.current?.abort();
      previewAbortRef.current = null;
      previewSequenceRef.current += 1;
      autoPreviewedRef.current = false;
    },
    [],
  );

  // Entering the page previews the current parameters once; later edits
  // only mark that response stale until the user refreshes. The ref keeps
  // the effect re-entrant under strict mode.
  useEffect(() => {
    if (autoPreviewedRef.current || currentInput === null) return;
    autoPreviewedRef.current = true;
    requestPreview();
  });

  const submit = () => {
    if (currentInput === null) return;
    setStaleConflict(false);
    reparse.mutate(
      { documentId: document.id, baseId: base.id, input: currentInput },
      { onSuccess: () => onExit(), onError: refreshStaleAuthority },
    );
  };

  const busy = reparse.isPending;

  return (
    <section
      data-testid="knowledge-document-chunk-settings"
      aria-label={labels.documents.chunkSettingsTitle(document.name)}
      className="flex min-h-0 flex-col text-[13px] lg:h-[calc(100dvh-3rem)]"
    >
      <div className="border-border/60 grid shrink-0 gap-3 border-b pb-3 md:grid-cols-[1fr_auto_1fr] md:items-center">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8 w-fit rounded-lg px-1 text-[13px]"
          disabled={busy}
          onClick={onExit}
          aria-label={labels.common.back}
        >
          <ArrowLeftIcon aria-hidden className="size-4" />
          {base.name}
        </Button>
        <h1
          className="min-w-0 truncate text-center text-[13px] font-medium"
          title={document.name}
        >
          {labels.documents.chunkSettingsTitle(document.name)}
        </h1>
        <span aria-hidden className="hidden md:block" />
      </div>

      <div className="grid min-h-0 w-full flex-1 gap-5 py-4 lg:grid-cols-2 lg:overflow-hidden">
        <form
          className="flex min-h-0 flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <div className="min-h-0 flex-1 space-y-5 pb-1 lg:overflow-y-auto lg:pr-4">
            <div className="space-y-3">
              <div className="space-y-1.5">
                <h2 className="text-[13px] font-semibold">
                  {wizard.chunkSectionTitle}
                </h2>
                <p className="text-muted-foreground text-xs leading-5">
                  {labels.documents.chunkSettingsCurrentProfile}
                </p>
                <p className="text-muted-foreground text-xs leading-5">
                  {labels.wizard.knowledgeTokenUnit}
                </p>
                <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
                  {labels.documents.reparseWarning}
                </p>
                {document.chunk_size_unit === "character" ? (
                  <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
                    {labels.documents.reparseLegacyUnitWarning}
                  </p>
                ) : null}
              </div>
              <KnowledgeChunkSettingsFields
                value={draft}
                onChange={setDraft}
                disabled={busy}
                limits={chunkLimits}
                radioName="document-chunking-mode"
                lockedMode={lockedChunkingMode}
              />
            </div>

            <KnowledgeBaseConfigurationSummary scope={scope} base={base} />

            {staleConflict ? (
              <p
                role="alert"
                className="text-destructive text-[13px]"
                data-testid="knowledge-reparse-conflict"
              >
                {labels.documents.reparseConflict}
              </p>
            ) : null}
            {reparse.error && !staleConflict ? (
              <p role="alert" className="text-destructive text-[13px]">
                {knowledgeErrorMessage(reparse.error, labels.errors)}
              </p>
            ) : null}
          </div>
          <div className="border-border/70 bg-background flex shrink-0 items-center justify-between gap-3 border-t pt-3">
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              disabled={busy}
              onClick={onExit}
            >
              {labels.common.cancel}
            </Button>
            <Button
              className="h-9 rounded-lg bg-blue-600 text-[13px] text-white shadow-none hover:bg-blue-700"
              type="submit"
              disabled={busy || currentInput === null}
            >
              {busy
                ? labels.documents.reparsePending
                : labels.documents.reparseSubmit}
            </Button>
          </div>
        </form>

        <aside
          aria-label={wizard.previewTitle}
          className="border-border/70 bg-background flex min-h-80 min-w-0 flex-col overflow-hidden rounded-xl border lg:min-h-0"
          data-testid="knowledge-reparse-preview"
          aria-busy={previewLoading}
        >
          <div className="border-border/60 shrink-0 space-y-2 border-b px-4 py-3">
            <div className="flex items-center justify-between gap-3">
              <h2 className="text-xs font-semibold text-blue-600 dark:text-blue-400">
                {wizard.previewTitle}
              </h2>
              <Button
                className="h-8 shrink-0 rounded-lg text-xs text-blue-600 shadow-none hover:text-blue-700"
                type="button"
                variant="outline"
                size="sm"
                disabled={busy || currentInput === null || previewLoading}
                onClick={requestPreview}
              >
                <RefreshCwIcon
                  aria-hidden
                  className={cn("size-3.5", previewLoading && "animate-spin")}
                />
                {previewLoading ? wizard.previewLoading : wizard.previewRefresh}
              </Button>
            </div>
            <div className="flex min-w-0 items-center gap-2">
              <KnowledgeFileTypeIcon fileName={document.original_name} />
              <p
                className="min-w-0 flex-1 truncate text-[13px] font-medium"
                title={document.original_name}
              >
                {wizard.previewHint(document.original_name)}
              </p>
            </div>
            {visiblePreviewData ? (
              <p className="text-muted-foreground text-xs tabular-nums">
                {wizard.previewShowing(
                  visiblePreviewData.items.length,
                  visiblePreviewData.total,
                )}
              </p>
            ) : null}
          </div>
          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
            {previewLoading ? (
              <p
                role="status"
                className="text-muted-foreground text-xs leading-5"
              >
                {wizard.previewLoading}
              </p>
            ) : currentInput === null ? (
              <p role="status" className="text-destructive text-xs">
                {wizard.previewInvalid}
              </p>
            ) : previewIsStale ? (
              <p
                role="status"
                className="text-muted-foreground text-xs leading-5"
              >
                {wizard.previewStale}
              </p>
            ) : null}
            {visiblePreviewError ? (
              <p role="alert" className="text-destructive text-[13px]">
                {knowledgeErrorMessage(visiblePreviewError, labels.errors)}
              </p>
            ) : null}
            {visiblePreviewData &&
            visiblePreviewData.omitted_preview_attachment_count > 0 ? (
              <p className="text-muted-foreground text-xs">
                {labels.documents.reparsePreviewAttachmentsOmitted(
                  visiblePreviewData.omitted_preview_attachment_count,
                )}
              </p>
            ) : null}
            {visiblePreviewData ? (
              <KnowledgeChunkPreviewList
                data={visiblePreviewData}
                scopeKey={scopeKey}
                stale={previewIsStale}
              />
            ) : null}
          </div>
        </aside>
      </div>
    </section>
  );
}
