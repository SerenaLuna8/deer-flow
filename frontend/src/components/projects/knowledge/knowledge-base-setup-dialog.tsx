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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import {
  useKnowledgeModelOptions,
  useUpdateKnowledgeBase,
} from "@/core/knowledge/hooks";
import type {
  KnowledgeBaseItem,
  KnowledgeRetrievalMode,
} from "@/core/knowledge/types";
import type { ProjectClientScope } from "@/core/private-work/types";

import { knowledgeErrorMessage } from "./knowledge-error";
import { KnowledgeRetrievalModeField } from "./knowledge-retrieval-mode-field";

/** First configuration of an empty base, before any upload is admitted. */
export function KnowledgeBaseSetupDialog({
  scope,
  base,
  open,
  onOpenChange,
  onConfigured,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfigured?: (base: KnowledgeBaseItem) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const options = useKnowledgeModelOptions(scope, open);
  const update = useUpdateKnowledgeBase(scope);
  const [embeddingModelId, setEmbeddingModelId] = useState("");
  const [rerankerModelId, setRerankerModelId] = useState("");
  const [retrievalMode, setRetrievalMode] = useState<KnowledgeRetrievalMode>(
    base.retrieval_mode,
  );

  const close = (nextOpen: boolean) => {
    if (update.isPending) return;
    if (!nextOpen) update.reset();
    onOpenChange(nextOpen);
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto rounded-xl text-[13px] sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="text-base">
            {labels.bases.setupTitle}
          </DialogTitle>
          <DialogDescription className="text-[13px]">
            {labels.bases.setupDescription}
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-5"
          onSubmit={(event) => {
            event.preventDefault();
            if (!embeddingModelId || update.isPending) return;
            update.mutate(
              {
                baseId: base.id,
                input: {
                  embedding_model_id: embeddingModelId,
                  retrieval_mode: retrievalMode,
                  ...(rerankerModelId
                    ? { reranker_model_id: rerankerModelId }
                    : {}),
                },
              },
              {
                onSuccess: (configured) => {
                  onOpenChange(false);
                  onConfigured?.(configured.item);
                },
              },
            );
          }}
        >
          <div className="space-y-2">
            <h3 className="font-medium">{labels.bases.modelLabel}</h3>
            {options.isLoading ? (
              <Skeleton className="h-9" />
            ) : options.error ? (
              <p role="alert" className="text-destructive text-xs">
                {labels.bases.modelsLoadFailed}
              </p>
            ) : options.data?.embedding_models.length === 0 ? (
              <p className="text-muted-foreground text-xs">
                {labels.bases.noModels}
              </p>
            ) : (
              <Select
                value={embeddingModelId}
                disabled={update.isPending}
                onValueChange={setEmbeddingModelId}
              >
                <SelectTrigger
                  aria-label={labels.bases.modelLabel}
                  className="bg-background border-input/80 w-full text-[13px] shadow-none"
                >
                  <SelectValue placeholder={labels.bases.modelPlaceholder} />
                </SelectTrigger>
                <SelectContent>
                  {options.data?.embedding_models.map((option) => (
                    <SelectItem key={option.id} value={option.id}>
                      {option.provider_name} · {option.model_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
          <KnowledgeRetrievalModeField
            variant="cards"
            showHint={false}
            value={retrievalMode}
            onChange={setRetrievalMode}
            disabled={update.isPending}
            selectedContent={
              <div className="space-y-2">
                <h3 className="font-medium">{labels.bases.rerankerLabel}</h3>
                <Select
                  value={rerankerModelId || "none"}
                  disabled={
                    update.isPending || options.isLoading || !!options.error
                  }
                  onValueChange={(value) =>
                    setRerankerModelId(value === "none" ? "" : value)
                  }
                >
                  <SelectTrigger
                    aria-label={labels.bases.rerankerLabel}
                    className="bg-background border-input/80 w-full text-[13px] shadow-none"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">
                      {labels.bases.rerankerNone}
                    </SelectItem>
                    {options.data?.reranker_models.map((option) => (
                      <SelectItem key={option.id} value={option.id}>
                        {option.provider_name} · {option.model_name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            }
          />
          {update.error ? (
            <p role="alert" className="text-destructive text-xs">
              {knowledgeErrorMessage(update.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={update.isPending}
              onClick={() => close(false)}
            >
              {labels.common.cancel}
            </Button>
            <Button
              type="submit"
              className="bg-blue-600 text-white hover:bg-blue-700"
              disabled={
                !embeddingModelId ||
                update.isPending ||
                options.isLoading ||
                !!options.error
              }
            >
              {update.isPending ? labels.common.saving : labels.bases.setupSave}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
