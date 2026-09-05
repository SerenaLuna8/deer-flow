"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useI18n } from "@/core/i18n/hooks";
import {
  isChildChunkSizeValid,
  isChunkOverlapValid,
  isChunkSeparatorValid,
  isChunkSizeValid,
} from "@/core/knowledge/chunk-settings";
import {
  useKnowledgeFileCapabilities,
  useReparseKnowledgeBase,
} from "@/core/knowledge/hooks";
import {
  DEFAULT_CHILD_CHUNK_SEPARATOR,
  DEFAULT_CHUNK_SEPARATOR,
  type KnowledgeBaseItem,
  type KnowledgeBaseReparseInput,
} from "@/core/knowledge/types";
import type { ProjectClientScope } from "@/core/private-work/types";

import {
  KnowledgeChunkSettingsFields,
  type KnowledgeChunkSettingsDraft,
} from "./knowledge-chunk-settings-fields";
import { knowledgeErrorMessage } from "./knowledge-error";

/**
 * Base-wide re-parse: the only way to change a populated base's chunking
 * mode. One parameter set replaces every document's own settings, so the
 * dialog opens on the *other* mode with wizard defaults and states exactly
 * what is replaced. The server admits every settled document or nothing; a
 * refusal (documents still processing) surfaces as its message.
 */
export function KnowledgeBaseReparseDialog({
  scope,
  base,
  onClose,
  onAccepted,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  onClose: () => void;
  onAccepted: (acceptedDocumentCount: number) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const capabilities = useKnowledgeFileCapabilities(scope);
  const reparse = useReparseKnowledgeBase(scope);
  const [draft, setDraft] = useState<KnowledgeChunkSettingsDraft>(() => ({
    chunkSize: "1000",
    chunkOverlap: "100",
    chunkSeparator: DEFAULT_CHUNK_SEPARATOR,
    chunkingMode:
      base.chunking_mode === "parent_child" ? "general" : "parent_child",
    childChunkSize: "500",
    childChunkSeparator: DEFAULT_CHILD_CHUNK_SEPARATOR,
    removeExtraSpaces: false,
    removeUrlsEmails: false,
  }));

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

  const submit = () => {
    if (!paramsValid) return;
    const input: KnowledgeBaseReparseInput = {
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
    };
    reparse.mutate(
      { baseId: base.id, input },
      {
        onSuccess: (result) => {
          onAccepted(result.accepted_document_count);
          onClose();
        },
      },
    );
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !reparse.isPending) onClose();
      }}
    >
      <DialogContent
        className="border-border/80 max-h-[85vh] overflow-y-auto rounded-xl text-[13px] sm:max-w-xl"
        data-testid="knowledge-base-reparse-dialog"
      >
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">
            {labels.bases.reparseBaseTitle}
          </DialogTitle>
          <DialogDescription className="text-[13px] leading-5">
            {labels.bases.reparseBaseDescription(
              base.name,
              base.document_count,
            )}
          </DialogDescription>
        </DialogHeader>
        <p className="text-muted-foreground text-xs leading-5">
          {labels.bases.reparseBaseBlockedHint}
        </p>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <KnowledgeChunkSettingsFields
            value={draft}
            onChange={setDraft}
            disabled={reparse.isPending}
            limits={chunkLimits}
            radioName="base-chunking-mode"
          />
          {reparse.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(reparse.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              disabled={reparse.isPending}
              onClick={onClose}
            >
              {labels.common.cancel}
            </Button>
            <Button
              className="h-9 rounded-lg bg-blue-600 text-[13px] text-white shadow-none hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600"
              type="submit"
              disabled={reparse.isPending || !paramsValid}
            >
              {reparse.isPending
                ? labels.bases.reparseBasePending
                : labels.bases.reparseBaseSubmit}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
