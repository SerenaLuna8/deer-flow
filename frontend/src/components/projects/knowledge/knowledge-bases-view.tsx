"use client";

import {
  BookOpenIcon,
  ChevronRightIcon,
  FolderPlusIcon,
  PlusIcon,
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
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/core/i18n/hooks";
import { KNOWLEDGE_BASE_NAME_MAX_CHARS } from "@/core/knowledge/chunk-settings";
import {
  useCreateKnowledgeBase,
  useDeleteKnowledgeBase,
  useKnowledgeBases,
} from "@/core/knowledge/hooks";
import type {
  KnowledgeBaseItem,
  KnowledgeBaseStatus,
} from "@/core/knowledge/types";
import type { ProjectClientScope } from "@/core/private-work/types";
import { cn } from "@/lib/utils";

import { knowledgeErrorMessage } from "./knowledge-error";

function baseStatusVariant(
  status: KnowledgeBaseStatus,
): "default" | "secondary" | "destructive" {
  if (status === "active") return "default";
  if (status === "disabled") return "secondary";
  return "destructive";
}

export function KnowledgeBasesView({
  scope,
  canEdit,
  createOpen,
  onCreateOpenChange,
  onStartWizard,
  onOpenBase,
}: {
  scope: ProjectClientScope;
  canEdit: boolean;
  /** The create action lives in the page toolbar; the dialog renders here. */
  createOpen: boolean;
  onCreateOpenChange: (open: boolean) => void;
  onStartWizard: () => void;
  onOpenBase: (base: KnowledgeBaseItem) => void;
}) {
  const { t, locale } = useI18n();
  const labels = t.knowledge;
  const bases = useKnowledgeBases(scope);
  const [deleting, setDeleting] = useState<KnowledgeBaseItem | null>(null);
  const deleteBase = useDeleteKnowledgeBase(scope);
  const closeDeleteDialog = () => {
    setDeleting(null);
    // A stale error from a previous failed delete must not greet the next one.
    deleteBase.reset();
  };

  return (
    <section aria-label={labels.bases.title} className="space-y-4">
      {bases.isLoading ? (
        <Skeleton className="h-40 rounded-xl" />
      ) : bases.error ? (
        <p role="alert" className="text-destructive text-[13px]">
          {knowledgeErrorMessage(bases.error, labels.errors)}
        </p>
      ) : (bases.data?.items.length ?? 0) === 0 ? (
        canEdit ? (
          <div className="flex flex-col items-center gap-5 rounded-xl border border-dashed px-6 py-14">
            <span className="bg-muted flex size-10 items-center justify-center rounded-lg">
              <BookOpenIcon
                aria-hidden
                className="text-muted-foreground size-5"
              />
            </span>
            <p className="text-[13px] font-medium">{labels.wizard.heroTitle}</p>
            <div className="w-full max-w-md space-y-2">
              <button
                type="button"
                className="border-border hover:border-selection/60 hover:bg-muted/40 focus-visible:ring-ring flex w-full items-start gap-3 rounded-xl border px-4 py-3 text-left transition-colors focus-visible:ring-2 focus-visible:outline-none"
                onClick={onStartWizard}
              >
                <span className="bg-muted mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md">
                  <PlusIcon aria-hidden className="size-4" />
                </span>
                <span className="min-w-0">
                  <span className="block text-[13px] font-medium">
                    {labels.wizard.uploadCreateTitle}
                  </span>
                  <span className="text-muted-foreground mt-0.5 block text-xs leading-5">
                    {labels.wizard.uploadCreateHint}
                  </span>
                </span>
              </button>
              <p className="text-muted-foreground text-center text-xs">
                {labels.wizard.orSeparator}
              </p>
              <button
                type="button"
                className="border-border hover:border-selection/60 hover:bg-muted/40 focus-visible:ring-ring flex w-full items-start gap-3 rounded-xl border px-4 py-3 text-left transition-colors focus-visible:ring-2 focus-visible:outline-none"
                onClick={() => onCreateOpenChange(true)}
              >
                <span className="bg-muted mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md">
                  <FolderPlusIcon aria-hidden className="size-4" />
                </span>
                <span className="min-w-0">
                  <span className="block text-[13px] font-medium">
                    {labels.wizard.emptyCreateTitle}
                  </span>
                  <span className="text-muted-foreground mt-0.5 block text-xs leading-5">
                    {labels.wizard.emptyCreateHint}
                  </span>
                </span>
              </button>
            </div>
          </div>
        ) : (
          <p className="text-muted-foreground rounded-xl border border-dashed px-4 py-10 text-center text-[13px]">
            {labels.bases.empty}
          </p>
        )
      ) : (
        <ol
          className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3"
          data-testid="knowledge-base-list"
        >
          {bases.data?.items.map((base) => (
            <li
              key={base.id}
              className="border-border/80 bg-card hover:border-selection/30 flex min-w-0 flex-col rounded-xl border p-4 shadow-xs transition-[border-color,box-shadow] hover:shadow-sm"
            >
              <button
                type="button"
                className="focus-visible:ring-ring min-w-0 flex-1 rounded-md text-left focus-visible:ring-2 focus-visible:outline-none"
                onClick={() => onOpenBase(base)}
              >
                <span className="flex min-w-0 items-center gap-2">
                  <span className="bg-selection-subtle/70 text-selection flex size-8 shrink-0 items-center justify-center rounded-lg">
                    <BookOpenIcon aria-hidden className="size-4" />
                  </span>
                  <span className="text-foreground min-w-0 truncate text-[13px] font-medium">
                    {base.name}
                  </span>
                  <Badge
                    variant={baseStatusVariant(base.status)}
                    className={cn(
                      "rounded-md px-1.5 py-0.5 text-[11px] font-medium",
                      base.status === "active" &&
                        base.embedding_model_id !== null &&
                        "border-success/15 bg-success/10 text-emerald-700 dark:text-emerald-300",
                      (base.status === "disabled" ||
                        base.embedding_model_id === null) &&
                        "border-border/60 bg-muted text-muted-foreground",
                    )}
                  >
                    {base.status === "active" &&
                    base.embedding_model_id === null
                      ? labels.bases.unconfigured
                      : labels.status[base.status]}
                  </Badge>
                </span>
                <span className="text-muted-foreground mt-2 line-clamp-2 block text-[13px] leading-5">
                  {base.description || labels.bases.noDescription}
                </span>
                {base.delete_error ? (
                  <span className="text-destructive mt-1 block text-xs">
                    {labels.bases.deleteError(base.delete_error)}
                  </span>
                ) : null}
              </button>
              <div className="border-border/60 mt-3 flex items-center justify-between gap-2 border-t pt-3">
                <span className="text-muted-foreground min-w-0 truncate text-xs">
                  {labels.bases.documentCount(base.document_count)}
                  {" · "}
                  {labels.bases.updatedAt(
                    new Date(base.updated_at).toLocaleDateString(locale),
                  )}
                </span>
                <div className="flex shrink-0 items-center gap-1">
                  {canEdit ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="text-muted-foreground hover:bg-destructive/5 hover:text-destructive h-7 rounded-md px-2 text-xs shadow-none"
                      onClick={() => setDeleting(base)}
                    >
                      {labels.common.delete}
                    </Button>
                  ) : null}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="text-muted-foreground hover:bg-selection-subtle hover:text-selection h-7 rounded-md px-2 text-[13px] shadow-none"
                    aria-label={labels.bases.openDocuments}
                    onClick={() => onOpenBase(base)}
                  >
                    <ChevronRightIcon aria-hidden className="size-4" />
                  </Button>
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}

      {canEdit ? (
        <CreateBaseDialog
          scope={scope}
          open={createOpen}
          onOpenChange={onCreateOpenChange}
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
              {labels.bases.deleteTitle}
            </DialogTitle>
            <DialogDescription className="text-[13px] leading-5">
              {deleting ? labels.bases.deleteDescription(deleting.name) : ""}
            </DialogDescription>
          </DialogHeader>
          {deleteBase.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(deleteBase.error, labels.errors)}
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
              disabled={deleteBase.isPending}
              onClick={() => {
                if (!deleting) return;
                deleteBase.mutate(deleting.id, {
                  onSuccess: () => closeDeleteDialog(),
                });
              }}
            >
              {deleteBase.isPending
                ? labels.common.deleting
                : labels.bases.deleteConfirm}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function CreateBaseDialog({
  scope,
  open,
  onOpenChange,
}: {
  scope: ProjectClientScope;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const createBase = useCreateKnowledgeBase(scope);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  const close = (nextOpen: boolean) => {
    onOpenChange(nextOpen);
    if (!nextOpen) {
      setName("");
      setDescription("");
      createBase.reset();
    }
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="border-border/80 rounded-xl text-[13px]">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">
            {labels.bases.createTitle}
          </DialogTitle>
          <DialogDescription className="text-[13px] leading-5">
            {labels.bases.createDescription}
          </DialogDescription>
        </DialogHeader>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!name.trim()) return;
            createBase.mutate(
              {
                name: name.trim(),
                description: description.trim(),
              },
              { onSuccess: () => close(false) },
            );
          }}
        >
          <label className="grid gap-1.5 text-[13px]">
            <span className="font-medium">{labels.bases.nameLabel}</span>
            <Input
              className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
              value={name}
              required
              maxLength={KNOWLEDGE_BASE_NAME_MAX_CHARS}
              placeholder={labels.bases.namePlaceholder}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label className="grid gap-1.5 text-[13px]">
            <span className="font-medium">{labels.bases.descriptionLabel}</span>
            <Textarea
              className="border-input/80 bg-background rounded-lg text-[13px] leading-5 shadow-none md:text-[13px]"
              value={description}
              rows={3}
              placeholder={labels.bases.descriptionPlaceholder}
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
          {createBase.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(createBase.error, labels.errors)}
            </p>
          ) : null}
          <DialogFooter>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              onClick={() => close(false)}
            >
              {labels.common.cancel}
            </Button>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="submit"
              disabled={createBase.isPending || !name.trim()}
            >
              {createBase.isPending
                ? labels.common.creating
                : labels.common.create}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
