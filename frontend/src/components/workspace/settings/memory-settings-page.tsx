"use client";

import { useDeferredValue, useEffect, useId, useRef, useState } from "react";
import { toast } from "sonner";

import { ProjectPageHeader } from "@/components/projects/project-page-header";
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
import { Textarea } from "@/components/ui/textarea";
import {
  buildMemorySectionGroups,
  countPopulatedSummaries,
  isMemorySummaryEmpty,
  type MemoryFact,
  type MemoryViewFilter,
  truncateFactPreview,
} from "@/components/workspace/settings/memory/memory-view-model";
import {
  MemoryContextSidecar,
  MemoryEmptyState,
  MemoryFactList,
  MemoryHeaderActions,
  MemoryLoadError,
  MemoryLoadingState,
  MemoryStatusBar,
  MemorySummaryDisclosure,
  MemoryToolbar,
  type MemorySourceThreadHref,
} from "@/components/workspace/settings/memory/memory-workbench";
import { useI18n } from "@/core/i18n/hooks";
import type {
  MemoryFactInput,
  MemoryFactPatchInput,
  UserMemory,
} from "@/core/private-work/memory";
import { formatTimeAgo } from "@/core/utils/datetime";

type PendingImport = {
  fileName: string;
  memory: UserMemory;
};

type MemoryMutation<TInput = void> = {
  isPending: boolean;
  mutateAsync: (input: TInput) => Promise<unknown>;
};

export type MemorySettingsPermissions = {
  canAdd: boolean;
  canClear: boolean;
  canDelete: boolean;
  canExport: boolean;
  canImport: boolean;
  canModify: boolean;
  canReload: boolean;
};

export type MemorySettingsController = {
  memory: UserMemory | null;
  isLoading: boolean;
  error: Error | null;
  clearMemory?: MemoryMutation;
  createMemoryFact?: MemoryMutation<MemoryFactInput>;
  deleteMemoryFact?: MemoryMutation<string>;
  importMemory?: MemoryMutation<UserMemory>;
  updateMemoryFact?: MemoryMutation<{
    factId: string;
    input: MemoryFactPatchInput;
  }>;
  reloadMemory?: MemoryMutation;
  exportMemory: () => Promise<UserMemory>;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isMemorySection(value: unknown): value is {
  summary: string;
  updatedAt: string;
} {
  return (
    isRecord(value) &&
    typeof value.summary === "string" &&
    typeof value.updatedAt === "string"
  );
}

function isMemoryFact(value: unknown): value is UserMemory["facts"][number] {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.content === "string" &&
    typeof value.category === "string" &&
    typeof value.confidence === "number" &&
    Number.isFinite(value.confidence) &&
    typeof value.createdAt === "string" &&
    typeof value.source === "string"
  );
}

function isImportedMemory(value: unknown): value is UserMemory {
  if (!isRecord(value)) {
    return false;
  }

  if (
    typeof value.version !== "string" ||
    typeof value.lastUpdated !== "string" ||
    !isRecord(value.user) ||
    !isRecord(value.history) ||
    !Array.isArray(value.facts)
  ) {
    return false;
  }

  return (
    isMemorySection(value.user.workContext) &&
    isMemorySection(value.user.personalContext) &&
    isMemorySection(value.user.topOfMind) &&
    isMemorySection(value.history.recentMonths) &&
    isMemorySection(value.history.earlierContext) &&
    isMemorySection(value.history.longTermBackground) &&
    value.facts.every(isMemoryFact)
  );
}

type FactFormState = {
  content: string;
  category: string;
  confidence: string;
};

const DEFAULT_FACT_FORM_STATE: FactFormState = {
  content: "",
  category: "context",
  confidence: "0.8",
};

export function MemorySettingsView({
  controller,
  permissions,
  sourceThreadHref,
}: {
  controller: MemorySettingsController;
  permissions: MemorySettingsPermissions;
  sourceThreadHref: MemorySourceThreadHref;
}) {
  const { t } = useI18n();
  const {
    memory,
    isLoading,
    error,
    clearMemory,
    createMemoryFact,
    deleteMemoryFact,
    importMemory: importMemoryMutation,
    updateMemoryFact,
    reloadMemory,
  } = controller;
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [clearDialogOpen, setClearDialogOpen] = useState(false);
  const [factToDelete, setFactToDelete] = useState<MemoryFact | null>(null);
  const [factToEdit, setFactToEdit] = useState<MemoryFact | null>(null);
  const [factEditorOpen, setFactEditorOpen] = useState(false);
  const [factForm, setFactForm] = useState<FactFormState>(
    DEFAULT_FACT_FORM_STATE,
  );
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<MemoryViewFilter>("all");
  const [summariesExpanded, setSummariesExpanded] = useState(false);
  const summaryTriggerRef = useRef<HTMLButtonElement | null>(null);
  const shouldFocusSummaryTriggerRef = useRef(false);
  const [pendingImport, setPendingImport] = useState<PendingImport | null>(
    null,
  );
  const [isExporting, setIsExporting] = useState(false);
  const deferredQuery = useDeferredValue(query);
  const normalizedQuery = deferredQuery.trim().toLowerCase();
  const factContentInputId = useId();
  const factCategoryInputId = useId();
  const factConfidenceInputId = useId();
  const factConfidenceHintId = useId();

  const clearAllLabel = t.settings.memory.clearAll ?? "Clear all memory";
  const clearAllConfirmTitle =
    t.settings.memory.clearAllConfirmTitle ?? "Clear all memory?";
  const clearAllConfirmDescription =
    t.settings.memory.clearAllConfirmDescription ??
    "This will remove all saved summaries and facts. This action cannot be undone.";
  const clearAllSuccess =
    t.settings.memory.clearAllSuccess ?? "All memory cleared";
  const factDeleteConfirmTitle =
    t.settings.memory.factDeleteConfirmTitle ?? "Delete this fact?";
  const factDeleteConfirmDescription =
    t.settings.memory.factDeleteConfirmDescription ??
    "This fact will be removed from memory immediately. This action cannot be undone.";
  const factDeleteSuccess =
    t.settings.memory.factDeleteSuccess ?? "Fact deleted";
  const addFactTitle = t.settings.memory.addFactTitle;
  const editFactTitle = t.settings.memory.editFactTitle;
  const addFactSuccess = t.settings.memory.addFactSuccess;
  const editFactSuccess = t.settings.memory.editFactSuccess;
  const factContentLabel = t.settings.memory.factContentLabel;
  const factCategoryLabel = t.settings.memory.factCategoryLabel;
  const factConfidenceLabel = t.settings.memory.factConfidenceLabel;
  const factContentPlaceholder = t.settings.memory.factContentPlaceholder;
  const factCategoryPlaceholder = t.settings.memory.factCategoryPlaceholder;
  const factConfidenceHint = t.settings.memory.factConfidenceHint;
  const factSave = t.settings.memory.factSave;
  const factValidationContent = t.settings.memory.factValidationContent;
  const factValidationConfidence = t.settings.memory.factValidationConfidence;
  const factPreviewLabel =
    t.settings.memory.factPreviewLabel ?? "Fact to delete";
  const noMatches = t.settings.memory.noMatches ?? "No matching memory found";
  const exportSuccess =
    t.settings.memory.exportSuccess ?? t.common.exportSuccess;
  const importSuccess = t.settings.memory.importSuccess ?? "Memory imported";

  const sectionGroups = memory ? buildMemorySectionGroups(memory, t) : [];
  const summaryCount = countPopulatedSummaries(sectionGroups);
  const trimmedRecentFocus = memory?.user.topOfMind.summary.trim();
  const recentFocus =
    trimmedRecentFocus && trimmedRecentFocus.length > 0
      ? trimmedRecentFocus
      : t.settings.memory.markdown.empty;
  const filteredSectionGroups = sectionGroups
    .map((group) => ({
      ...group,
      sections: group.sections.filter((section) =>
        normalizedQuery
          ? `${section.title} ${section.summary}`
              .toLowerCase()
              .includes(normalizedQuery)
          : true,
      ),
    }))
    .filter((group) => group.sections.length > 0);

  const filteredFacts = memory
    ? memory.facts.filter((fact) =>
        normalizedQuery
          ? `${fact.content} ${fact.category}`
              .toLowerCase()
              .includes(normalizedQuery)
          : true,
      )
    : [];

  const hasSummarySearchMatch =
    normalizedQuery.length > 0 && filteredSectionGroups.length > 0;
  const summariesForcedOpen = filter === "summaries" || hasSummarySearchMatch;
  const summariesOpen = summariesForcedOpen || summariesExpanded;
  const showFacts = filter !== "summaries";
  const showSummaries = filter !== "facts";
  const memoryFullyEmpty = Boolean(
    memory && isMemorySummaryEmpty(memory) && memory.facts.length === 0,
  );
  const hasMatchingVisibleContent =
    (showSummaries && filteredSectionGroups.length > 0) ||
    (showFacts && filteredFacts.length > 0);
  const hasNoMatches = normalizedQuery.length > 0 && !hasMatchingVisibleContent;
  const shouldRenderFactsBlock =
    showFacts &&
    !hasNoMatches &&
    (normalizedQuery.length === 0 || filteredFacts.length > 0);
  const shouldRenderSummariesBlock =
    showSummaries &&
    !hasNoMatches &&
    (normalizedQuery.length === 0 || filteredSectionGroups.length > 0);
  const filteredSummaryCount = countPopulatedSummaries(filteredSectionGroups);
  const showContextSidecar = filter === "all" && shouldRenderSummariesBlock;

  useEffect(() => {
    const trigger = summaryTriggerRef.current;
    if (
      !shouldFocusSummaryTriggerRef.current ||
      !shouldRenderSummariesBlock ||
      !summariesOpen ||
      !trigger
    ) {
      return;
    }

    shouldFocusSummaryTriggerRef.current = false;
    trigger.scrollIntoView({ behavior: "smooth", block: "center" });
    trigger.focus({ preventScroll: true });
  }, [shouldRenderSummariesBlock, summariesOpen]);

  function handleViewSummaries() {
    shouldFocusSummaryTriggerRef.current = true;
    setQuery("");
    setFilter("summaries");
    setSummariesExpanded(true);
  }

  async function handleExportMemory() {
    try {
      setIsExporting(true);
      const exportedMemory = await controller.exportMemory();
      const fileName = `deerflow-memory-${(exportedMemory.lastUpdated || new Date().toISOString()).replace(/[:.]/g, "-")}.json`;
      const blob = new Blob([JSON.stringify(exportedMemory, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = fileName;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      toast.success(exportSuccess);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setIsExporting(false);
    }
  }

  async function handleImportFileSelection(event: {
    target: HTMLInputElement;
  }) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) {
      return;
    }

    try {
      const parsed: unknown = JSON.parse(await file.text());
      if (!isImportedMemory(parsed)) {
        toast.error(t.settings.memory.importInvalidFile);
        return;
      }
      setPendingImport({
        fileName: file.name,
        memory: parsed,
      });
    } catch {
      toast.error(t.settings.memory.importInvalidFile);
    }
  }

  async function handleConfirmImport() {
    if (!pendingImport) {
      return;
    }

    try {
      if (!importMemoryMutation) return;
      await importMemoryMutation.mutateAsync(pendingImport.memory);
      toast.success(importSuccess);
      setPendingImport(null);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleClearMemory() {
    try {
      if (!clearMemory) return;
      await clearMemory.mutateAsync();
      toast.success(clearAllSuccess);
      setClearDialogOpen(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleDeleteFact() {
    if (!factToDelete) return;

    try {
      if (!deleteMemoryFact) return;
      await deleteMemoryFact.mutateAsync(factToDelete.id);
      toast.success(factDeleteSuccess);
      setFactToDelete(null);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }

  function openCreateFactDialog() {
    setFactToEdit(null);
    setFactForm(DEFAULT_FACT_FORM_STATE);
    setFactEditorOpen(true);
  }

  function openEditFactDialog(fact: MemoryFact) {
    setFactToEdit(fact);
    setFactForm({
      content: fact.content,
      category: fact.category,
      confidence: String(fact.confidence),
    });
    setFactEditorOpen(true);
  }

  async function handleSaveFact() {
    const trimmedContent = factForm.content.trim();
    if (!trimmedContent) {
      toast.error(factValidationContent);
      return;
    }

    const confidence = Number(factForm.confidence);
    if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
      toast.error(factValidationConfidence);
      return;
    }

    const input: MemoryFactInput = {
      content: trimmedContent,
      category: factForm.category.trim() || "context",
      confidence,
    };

    try {
      if (factToEdit) {
        const patchInput: MemoryFactPatchInput = {
          content: input.content,
          category: input.category,
          confidence: input.confidence,
        };
        if (!updateMemoryFact) return;
        await updateMemoryFact.mutateAsync({
          factId: factToEdit.id,
          input: patchInput,
        });
        toast.success(editFactSuccess);
      } else {
        if (!createMemoryFact) return;
        await createMemoryFact.mutateAsync(input);
        toast.success(addFactSuccess);
      }
      setFactEditorOpen(false);
      setFactToEdit(null);
      setFactForm(DEFAULT_FACT_FORM_STATE);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }

  const isFactFormPending =
    Boolean(createMemoryFact?.isPending) ||
    Boolean(updateMemoryFact?.isPending);

  async function handleReloadMemory() {
    if (!reloadMemory) return;
    try {
      await reloadMemory.mutateAsync();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <>
      <section
        data-testid="memory-workbench"
        className="space-y-6 lg:space-y-11"
      >
        <ProjectPageHeader
          title={t.settings.memory.title}
          description={t.settings.memory.description}
          actions={
            !isLoading && !error && memory ? (
              <>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".json,application/json"
                  className="hidden"
                  onChange={(event) => void handleImportFileSelection(event)}
                />
                <MemoryHeaderActions
                  t={t}
                  isImporting={importMemoryMutation?.isPending ?? false}
                  isExporting={isExporting}
                  isClearing={clearMemory?.isPending ?? false}
                  isReloading={reloadMemory?.isPending ?? false}
                  canAddFact={permissions.canAdd}
                  canImport={permissions.canImport}
                  canExport={permissions.canExport}
                  canClear={permissions.canClear}
                  canReload={permissions.canReload}
                  onAddFact={openCreateFactDialog}
                  onImport={() => fileInputRef.current?.click()}
                  onExport={() => void handleExportMemory()}
                  onClear={() => setClearDialogOpen(true)}
                  onReload={() => void handleReloadMemory()}
                />
              </>
            ) : undefined
          }
        />

        {isLoading ? (
          <MemoryLoadingState label={t.common.loading} />
        ) : error ? (
          <MemoryLoadError t={t} error={error} />
        ) : !memory ? (
          <div className="text-muted-foreground text-sm">
            {t.settings.memory.empty}
          </div>
        ) : (
          <>
            <MemoryStatusBar
              t={t}
              factCount={memory.facts.length}
              summaryCount={summaryCount}
              lastUpdated={formatTimeAgo(memory.lastUpdated)}
            />

            {memoryFullyEmpty ? (
              <div className="space-y-4">
                <MemoryToolbar
                  t={t}
                  query={query}
                  filter={filter}
                  factCount={memory.facts.length}
                  summaryCount={summaryCount}
                  onQueryChange={setQuery}
                  onFilterChange={setFilter}
                />
                <MemoryEmptyState
                  t={t}
                  onAddFact={
                    permissions.canAdd ? openCreateFactDialog : undefined
                  }
                />
              </div>
            ) : hasNoMatches ? (
              <section className="bg-card overflow-hidden rounded-xl border">
                <MemoryToolbar
                  t={t}
                  query={query}
                  filter={filter}
                  factCount={memory.facts.length}
                  summaryCount={summaryCount}
                  embedded
                  onQueryChange={setQuery}
                  onFilterChange={setFilter}
                />
                <div className="text-muted-foreground p-5 text-sm">
                  {noMatches}
                </div>
              </section>
            ) : filter === "summaries" ? (
              <section className="bg-card overflow-hidden rounded-xl border">
                <MemoryToolbar
                  t={t}
                  query={query}
                  filter={filter}
                  factCount={memory.facts.length}
                  summaryCount={summaryCount}
                  embedded
                  onQueryChange={setQuery}
                  onFilterChange={setFilter}
                />
                <MemorySummaryDisclosure
                  t={t}
                  groups={filteredSectionGroups}
                  summaryCount={filteredSummaryCount}
                  open={summariesOpen}
                  onOpenChange={(open) => {
                    if (!summariesForcedOpen) {
                      setSummariesExpanded(open);
                    }
                  }}
                  triggerRef={summaryTriggerRef}
                  embedded
                />
              </section>
            ) : (
              <div
                className={
                  showContextSidecar
                    ? "grid min-w-0 gap-4 lg:grid-cols-[minmax(0,2.15fr)_minmax(20rem,1fr)]"
                    : "grid min-w-0 gap-4"
                }
              >
                <section className="bg-card min-w-0 overflow-hidden rounded-xl border">
                  <MemoryToolbar
                    t={t}
                    query={query}
                    filter={filter}
                    factCount={memory.facts.length}
                    summaryCount={summaryCount}
                    embedded
                    onQueryChange={setQuery}
                    onFilterChange={setFilter}
                  />
                  <MemoryFactList
                    t={t}
                    facts={shouldRenderFactsBlock ? filteredFacts : []}
                    isDeleting={deleteMemoryFact?.isPending ?? false}
                    onEdit={
                      permissions.canModify ? openEditFactDialog : undefined
                    }
                    onDelete={
                      permissions.canDelete ? setFactToDelete : undefined
                    }
                    sourceThreadHref={sourceThreadHref}
                    embedded
                    showHeading={false}
                  />
                </section>

                {showContextSidecar ? (
                  <MemoryContextSidecar
                    t={t}
                    recentFocus={recentFocus}
                    groups={filteredSectionGroups}
                    summaryCount={filteredSummaryCount}
                    onViewSummaries={handleViewSummaries}
                  />
                ) : null}
              </div>
            )}
          </>
        )}
      </section>

      {permissions.canClear ? (
        <Dialog open={clearDialogOpen} onOpenChange={setClearDialogOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{clearAllConfirmTitle}</DialogTitle>
              <DialogDescription>
                {clearAllConfirmDescription}
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setClearDialogOpen(false)}
                disabled={clearMemory?.isPending}
              >
                {t.common.cancel}
              </Button>
              <Button
                variant="destructive"
                onClick={() => void handleClearMemory()}
                disabled={clearMemory?.isPending}
              >
                {clearMemory?.isPending ? t.common.loading : clearAllLabel}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}

      {permissions.canModify || permissions.canAdd ? (
        <Dialog
          open={factEditorOpen}
          onOpenChange={(open) => {
            setFactEditorOpen(open);
            if (!open) {
              setFactToEdit(null);
              setFactForm(DEFAULT_FACT_FORM_STATE);
            }
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {factToEdit ? editFactTitle : addFactTitle}
              </DialogTitle>
            </DialogHeader>
            <div className="space-y-4">
              <div className="space-y-2">
                <label
                  className="text-sm font-medium"
                  htmlFor={factContentInputId}
                >
                  {factContentLabel}
                </label>
                <Textarea
                  id={factContentInputId}
                  value={factForm.content}
                  onChange={(event) =>
                    setFactForm((current) => ({
                      ...current,
                      content: event.target.value,
                    }))
                  }
                  placeholder={factContentPlaceholder}
                  rows={4}
                />
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <label
                    className="text-sm font-medium"
                    htmlFor={factCategoryInputId}
                  >
                    {factCategoryLabel}
                  </label>
                  <Input
                    id={factCategoryInputId}
                    value={factForm.category}
                    onChange={(event) =>
                      setFactForm((current) => ({
                        ...current,
                        category: event.target.value,
                      }))
                    }
                    placeholder={factCategoryPlaceholder}
                  />
                </div>

                <div className="space-y-2">
                  <label
                    className="text-sm font-medium"
                    htmlFor={factConfidenceInputId}
                  >
                    {factConfidenceLabel}
                  </label>
                  <Input
                    id={factConfidenceInputId}
                    aria-describedby={factConfidenceHintId}
                    type="number"
                    min="0"
                    max="1"
                    step="0.01"
                    value={factForm.confidence}
                    onChange={(event) =>
                      setFactForm((current) => ({
                        ...current,
                        confidence: event.target.value,
                      }))
                    }
                  />
                  <div
                    className="text-muted-foreground text-xs"
                    id={factConfidenceHintId}
                  >
                    {factConfidenceHint}
                  </div>
                </div>
              </div>
            </div>
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => {
                  setFactEditorOpen(false);
                  setFactToEdit(null);
                  setFactForm(DEFAULT_FACT_FORM_STATE);
                }}
                disabled={isFactFormPending}
              >
                {t.common.cancel}
              </Button>
              <Button
                onClick={() => void handleSaveFact()}
                disabled={isFactFormPending}
              >
                {isFactFormPending ? t.common.loading : factSave}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}

      {permissions.canDelete ? (
        <Dialog
          open={factToDelete !== null}
          onOpenChange={(open) => {
            if (!open) {
              setFactToDelete(null);
            }
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{factDeleteConfirmTitle}</DialogTitle>
              <DialogDescription>
                {factDeleteConfirmDescription}
              </DialogDescription>
            </DialogHeader>
            {factToDelete ? (
              <div className="bg-muted rounded-md border p-3 text-sm">
                <div className="text-muted-foreground mb-1 font-medium">
                  {factPreviewLabel}
                </div>
                <p className="break-words">
                  {truncateFactPreview(factToDelete.content)}
                </p>
              </div>
            ) : null}
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setFactToDelete(null)}
                disabled={deleteMemoryFact?.isPending}
              >
                {t.common.cancel}
              </Button>
              <Button
                variant="destructive"
                onClick={() => void handleDeleteFact()}
                disabled={deleteMemoryFact?.isPending}
              >
                {deleteMemoryFact?.isPending
                  ? t.common.loading
                  : t.common.delete}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}

      {permissions.canImport ? (
        <Dialog
          open={pendingImport !== null}
          onOpenChange={(open) => {
            if (!open) {
              setPendingImport(null);
            }
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{t.settings.memory.importConfirmTitle}</DialogTitle>
              <DialogDescription>
                {t.settings.memory.importConfirmDescription}
              </DialogDescription>
            </DialogHeader>
            {pendingImport ? (
              <div className="bg-muted rounded-md border p-3 text-sm">
                <div>
                  <span className="text-muted-foreground">
                    {t.settings.memory.importFileLabel}:
                  </span>{" "}
                  {pendingImport.fileName}
                </div>
                <div>
                  <span className="text-muted-foreground">
                    {t.settings.memory.markdown.facts}:
                  </span>{" "}
                  {pendingImport.memory.facts.length}
                </div>
                <div>
                  <span className="text-muted-foreground">
                    {t.common.lastUpdated}:
                  </span>{" "}
                  {pendingImport.memory.lastUpdated
                    ? formatTimeAgo(pendingImport.memory.lastUpdated)
                    : "-"}
                </div>
              </div>
            ) : null}
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setPendingImport(null)}
                disabled={importMemoryMutation?.isPending}
              >
                {t.common.cancel}
              </Button>
              <Button
                onClick={() => void handleConfirmImport()}
                disabled={importMemoryMutation?.isPending}
              >
                {importMemoryMutation?.isPending
                  ? t.common.loading
                  : t.common.import}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}
    </>
  );
}
