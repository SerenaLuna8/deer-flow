"use client";

import {
  ArrowLeftIcon,
  BookOpenIcon,
  FileTextIcon,
  PlusIcon,
  SettingsIcon,
  TagsIcon,
  TargetIcon,
} from "lucide-react";
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
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/core/i18n/hooks";
import { KNOWLEDGE_BASE_NAME_MAX_CHARS } from "@/core/knowledge/chunk-settings";
import {
  useCreateKnowledgeMetadataField,
  useDeleteKnowledgeMetadataField,
  useKnowledgeMetadataFields,
  useKnowledgeModelOptions,
  useRebuildKnowledgeBase,
  useRenameKnowledgeMetadataField,
  useUpdateKnowledgeBase,
} from "@/core/knowledge/hooks";
import {
  KNOWLEDGE_DEFAULT_SORT,
  type KnowledgeNavigationState,
  type KnowledgeView,
} from "@/core/knowledge/navigation";
import type {
  KnowledgeBaseItem,
  KnowledgeDocumentItem,
  KnowledgeMetadataFieldItem,
  KnowledgeMetadataFieldType,
  KnowledgeRetrievalMode,
} from "@/core/knowledge/types";
import type { ProjectClientScope } from "@/core/private-work/types";
import { cn } from "@/lib/utils";

import { KnowledgeBaseSetupDialog } from "./knowledge-base-setup-dialog";
import { KnowledgeDocumentsView } from "./knowledge-documents-view";
import { knowledgeErrorMessage } from "./knowledge-error";
import { KnowledgeRetrievalModeField } from "./knowledge-retrieval-mode-field";
import { KnowledgeSearchPanel } from "./knowledge-search-panel";

type DetailSection = KnowledgeView;

/** Sentinel for the unbound reranker; Radix Select items cannot be "". */
const RERANKER_NONE = "none";

/**
 * In-base layout: a secondary menu (documents / retrieval test / settings)
 * beside the section content, mirroring upstream's in-knowledge-base navigation.
 * The active section comes from the URL (`view=`); switching sections and
 * leaving the base are resource moves, so both push history entries.
 */
export function KnowledgeBaseDetail({
  scope,
  base,
  canEdit,
  navState,
  onNavigate,
  onUploadDocuments,
  onOpenChunkSettings,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  canEdit: boolean;
  navState: KnowledgeNavigationState;
  onNavigate: (
    next: KnowledgeNavigationState,
    mode: "push" | "replace",
  ) => void;
  onUploadDocuments: () => void;
  onOpenChunkSettings: (document: KnowledgeDocumentItem) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const section = navState.view;
  const setSection = (next: DetailSection) => {
    if (next === section) return;
    // Switching sections drops document-list state; navigation parsing
    // would discard it on the way back in anyway.
    onNavigate(
      {
        ...navState,
        view: next,
        doc: null,
        segment: null,
        status: null,
        sort: KNOWLEDGE_DEFAULT_SORT,
        page: 1,
      },
      "push",
    );
  };

  const menuItems: Array<{
    id: DetailSection;
    label: string;
    icon: typeof FileTextIcon;
  }> = [
    { id: "documents", label: labels.detail.documents, icon: FileTextIcon },
    { id: "search", label: labels.search.title, icon: TargetIcon },
    ...(canEdit
      ? [
          {
            id: "metadata" as const,
            label: labels.detail.metadata,
            icon: TagsIcon,
          },
          {
            id: "settings" as const,
            label: labels.detail.settings,
            icon: SettingsIcon,
          },
        ]
      : []),
  ];

  return (
    <div className="flex flex-col gap-6 lg:flex-row lg:gap-8">
      <aside className="shrink-0 space-y-4 lg:sticky lg:top-6 lg:max-h-[calc(100dvh-3rem)] lg:w-60 lg:self-start lg:overflow-y-auto">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="text-muted-foreground hover:text-foreground h-8 rounded-lg text-[13px] shadow-none"
          aria-label={labels.common.back}
          onClick={() =>
            onNavigate(
              {
                kb: null,
                view: "documents",
                doc: null,
                segment: null,
                status: null,
                sort: KNOWLEDGE_DEFAULT_SORT,
                page: 1,
              },
              "push",
            )
          }
        >
          <ArrowLeftIcon aria-hidden className="size-4" />
          {labels.page.title}
        </Button>

        <div className="border-border/80 bg-muted/30 flex items-center gap-2.5 rounded-xl border p-3">
          <span className="bg-background text-selection ring-border/60 flex size-9 shrink-0 items-center justify-center rounded-lg ring-1">
            <BookOpenIcon aria-hidden className="size-4.5" />
          </span>
          <span className="min-w-0">
            <span
              className="block truncate text-[13px] font-medium"
              title={base.name}
            >
              {base.name}
            </span>
            <span className="text-muted-foreground block text-xs">
              {labels.bases.documentCount(base.document_count)}
            </span>
          </span>
        </div>

        <nav aria-label={labels.detail.navLabel} className="grid gap-1">
          {menuItems.map((item) => {
            const Icon = item.icon;
            const active = section === item.id;
            return (
              <button
                key={item.id}
                type="button"
                aria-current={active ? "page" : undefined}
                className={cn(
                  "focus-visible:ring-ring flex items-center gap-2 rounded-lg px-3 py-2 text-left text-[13px] transition-colors focus-visible:ring-2 focus-visible:outline-none",
                  active
                    ? "bg-selection-subtle/80 text-selection font-medium"
                    : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                )}
                onClick={() => setSection(item.id)}
              >
                <Icon aria-hidden className="size-4 shrink-0" />
                {item.label}
              </button>
            );
          })}
        </nav>
      </aside>

      <div className="min-w-0 flex-1">
        {section === "documents" ? (
          <KnowledgeDocumentsView
            scope={scope}
            base={base}
            canEdit={canEdit}
            navState={navState}
            onNavigate={onNavigate}
            onUploadDocuments={onUploadDocuments}
            onOpenChunkSettings={onOpenChunkSettings}
          />
        ) : null}
        {section === "search" && base.embedding_model_id === null ? (
          <div className="space-y-3 rounded-xl border border-dashed p-8 text-[13px]">
            <h2 className="text-base font-semibold">
              {labels.bases.unconfigured}
            </h2>
            <p className="text-muted-foreground">
              {labels.bases.unconfiguredHint}
            </p>
            {canEdit ? (
              <Button type="button" onClick={onUploadDocuments}>
                {labels.documents.uploadButton}
              </Button>
            ) : null}
          </div>
        ) : section === "search" ? (
          <KnowledgeSearchPanel
            scope={scope}
            base={base}
            onLocateSegment={(documentId, segmentId) =>
              onNavigate(
                {
                  ...navState,
                  view: "documents",
                  doc: documentId,
                  segment: segmentId,
                  status: null,
                  sort: KNOWLEDGE_DEFAULT_SORT,
                  page: 1,
                },
                "push",
              )
            }
          />
        ) : null}
        {section === "metadata" && canEdit ? (
          <KnowledgeMetadataPanel scope={scope} base={base} />
        ) : null}
        {section === "settings" && canEdit ? (
          <KnowledgeBaseSettingsPanel scope={scope} base={base} />
        ) : null}
      </div>
    </div>
  );
}

function KnowledgeBaseSettingsPanel({
  scope,
  base,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const updateBase = useUpdateKnowledgeBase(scope);
  const modelOptions = useKnowledgeModelOptions(scope, true);
  const [setupOpen, setSetupOpen] = useState(false);
  const [name, setName] = useState(base.name);
  const [description, setDescription] = useState(base.description);
  const [status, setStatus] = useState<"active" | "disabled">(
    base.status === "disabled" ? "disabled" : "active",
  );
  const [defaultTopK, setDefaultTopK] = useState(String(base.default_top_k));
  const [retrievalMode, setRetrievalMode] = useState<KnowledgeRetrievalMode>(
    base.retrieval_mode,
  );
  const [defaultThreshold, setDefaultThreshold] = useState(
    String(base.default_score_threshold),
  );
  const [summaryIndexEnabled, setSummaryIndexEnabled] = useState(
    base.summary_index_enabled,
  );
  // Radix Select cannot represent "" as an item value; use a sentinel.
  const [rerankerModelId, setRerankerModelId] = useState(
    base.reranker_model_id ?? RERANKER_NONE,
  );
  // Editing again after a save clears the "saved" note.
  const touch = () => {
    if (updateBase.isSuccess) updateBase.reset();
  };

  const parsedTopK = Number.parseInt(defaultTopK, 10);
  const topKValid =
    Number.isSafeInteger(parsedTopK) && parsedTopK >= 1 && parsedTopK <= 20;
  const parsedThreshold = Number.parseFloat(defaultThreshold);
  const thresholdValid =
    Number.isFinite(parsedThreshold) &&
    parsedThreshold >= 0 &&
    parsedThreshold <= 1;
  const formValid = name.trim().length > 0 && topKValid && thresholdValid;

  // Associate the later retrieval inputs and save action with this form while
  // keeping re-embedding outside it, so native validation and Enter still work.
  const settingsFormId = "knowledge-settings-" + base.id;

  return (
    <section aria-label={labels.detail.settings} className="w-full text-[13px]">
      <header className="mb-8 space-y-1">
        <h2 className="text-base font-semibold tracking-tight">
          {labels.detail.settings}
        </h2>
        <p className="text-muted-foreground text-xs leading-5">
          {labels.detail.settingsDescription}
        </p>
      </header>
      <form
        id={settingsFormId}
        className="grid gap-5"
        onSubmit={(event) => {
          event.preventDefault();
          if (!formValid) return;
          updateBase.mutate({
            baseId: base.id,
            input: {
              name: name.trim(),
              description: description.trim(),
              status,
              default_top_k: parsedTopK,
              retrieval_mode: retrievalMode,
              summary_index_enabled: summaryIndexEnabled,
              default_score_threshold: parsedThreshold,
              // The reranker binding is stated explicitly on every save:
              // either the selected model or an explicit clear.
              ...(rerankerModelId === RERANKER_NONE
                ? { clear_reranker_model: true }
                : { reranker_model_id: rerankerModelId }),
            },
          });
        }}
      >
        <label className="grid gap-2 sm:grid-cols-[160px_minmax(0,1fr)] sm:gap-6">
          <span className="font-medium sm:pt-2.5">
            {labels.bases.nameLabel}
          </span>
          <span className="flex w-full max-w-[820px] min-w-0 items-center gap-2.5">
            <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-blue-50 text-blue-600 dark:bg-blue-500/10 dark:text-blue-400">
              <BookOpenIcon aria-hidden className="size-4.5" />
            </span>
            <Input
              className="bg-background border-input/80 h-9 w-full min-w-0 flex-1 rounded-lg text-[13px] shadow-none focus-visible:border-blue-500/50 focus-visible:ring-blue-500/15 md:text-[13px]"
              value={name}
              required
              maxLength={KNOWLEDGE_BASE_NAME_MAX_CHARS}
              onChange={(event) => {
                touch();
                setName(event.target.value);
              }}
            />
          </span>
        </label>
        <label className="grid gap-2 sm:grid-cols-[160px_minmax(0,1fr)] sm:gap-6">
          <span className="font-medium sm:pt-2.5">
            {labels.bases.descriptionLabel}
          </span>
          <Textarea
            className="bg-background border-input/80 min-h-20 w-full max-w-[820px] min-w-0 rounded-lg text-[13px] leading-5 shadow-none focus-visible:border-blue-500/50 focus-visible:ring-blue-500/15 md:text-[13px]"
            value={description}
            rows={2}
            onChange={(event) => {
              touch();
              setDescription(event.target.value);
            }}
          />
        </label>
        <div className="grid gap-2 sm:grid-cols-[160px_minmax(0,1fr)] sm:gap-6">
          <span className="font-medium sm:pt-2.5">
            {labels.bases.statusLabel}
          </span>
          <div className="w-full max-w-[820px] min-w-0">
            <Select
              value={status}
              onValueChange={(value) => {
                touch();
                setStatus(value === "disabled" ? "disabled" : "active");
              }}
            >
              <SelectTrigger
                className="bg-background border-input/80 w-full min-w-0 rounded-lg text-[13px] shadow-none focus-visible:border-blue-500/50 focus-visible:ring-blue-500/15"
                aria-label={labels.bases.statusLabel}
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="rounded-lg">
                <SelectItem
                  className="text-[13px] focus:bg-blue-50 focus:text-blue-700 dark:focus:bg-blue-950/30 dark:focus:text-blue-300"
                  value="active"
                >
                  {labels.status.active}
                </SelectItem>
                <SelectItem
                  className="text-[13px] focus:bg-blue-50 focus:text-blue-700 dark:focus:bg-blue-950/30 dark:focus:text-blue-300"
                  value="disabled"
                >
                  {labels.status.disabled}
                </SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
      </form>

      {base.embedding_model_id === null ? (
        <section className="grid gap-3 pt-8 sm:grid-cols-[160px_minmax(0,1fr)] sm:gap-6">
          <h3 className="text-[13px] font-medium">
            {labels.bases.rebuildSectionTitle}
          </h3>
          <div className="space-y-3">
            <p className="text-muted-foreground text-xs leading-5">
              {labels.bases.unconfiguredHint}
            </p>
            <Button
              type="button"
              variant="outline"
              onClick={() => setSetupOpen(true)}
            >
              {labels.bases.setupButton}
            </Button>
          </div>
        </section>
      ) : (
        <KnowledgeRebuildSection scope={scope} base={base} />
      )}

      <KnowledgeBaseSetupDialog
        key={base.id}
        scope={scope}
        base={base}
        open={setupOpen}
        onOpenChange={setSetupOpen}
        onConfigured={(configured) => {
          setRetrievalMode(configured.retrieval_mode);
          setRerankerModelId(configured.reranker_model_id ?? RERANKER_NONE);
        }}
      />

      {base.embedding_model_id !== null ? (
        <section
          aria-label={labels.bases.retrievalSectionTitle}
          className="grid gap-3 pt-8 sm:grid-cols-[160px_minmax(0,1fr)] sm:gap-6"
        >
          <h3 className="font-medium sm:pt-2">
            {labels.bases.retrievalSectionTitle}
          </h3>
          <div className="w-full max-w-[820px] min-w-0 space-y-4">
            <KnowledgeRetrievalModeField
              variant="cards"
              showLabel={false}
              showHint={false}
              value={retrievalMode}
              onChange={(value) => {
                touch();
                setRetrievalMode(value);
              }}
              disabled={updateBase.isPending}
              selectedContent={
                <div className="space-y-4">
                  <div className="grid gap-1.5">
                    <span className="font-medium">
                      {labels.bases.rerankerLabel}
                    </span>
                    {modelOptions.isLoading ? (
                      <Skeleton className="h-9 rounded-lg" />
                    ) : modelOptions.error ? (
                      <p role="alert" className="text-destructive text-[13px]">
                        {labels.bases.modelsLoadFailed}
                      </p>
                    ) : (
                      <Select
                        value={rerankerModelId}
                        onValueChange={(value) => {
                          touch();
                          setRerankerModelId(value);
                        }}
                      >
                        <SelectTrigger
                          className="bg-background border-input/80 w-full min-w-0 rounded-lg text-[13px] shadow-none focus-visible:border-blue-500/50 focus-visible:ring-blue-500/15"
                          aria-label={labels.bases.rerankerLabel}
                        >
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent className="rounded-lg">
                          <SelectItem
                            className="text-[13px] focus:bg-blue-50 focus:text-blue-700 dark:focus:bg-blue-950/30 dark:focus:text-blue-300"
                            value={RERANKER_NONE}
                          >
                            {labels.bases.rerankerNone}
                          </SelectItem>
                          {modelOptions.data?.reranker_models.map((option) => (
                            <SelectItem
                              className="text-[13px] focus:bg-blue-50 focus:text-blue-700 dark:focus:bg-blue-950/30 dark:focus:text-blue-300"
                              key={option.id}
                              value={option.id}
                            >
                              {option.provider_name} · {option.model_name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                    <span className="text-muted-foreground text-xs leading-5">
                      {labels.bases.rerankerHint}
                    </span>
                  </div>
                  <div className="grid items-start gap-4 md:grid-cols-2">
                    <label className="grid gap-1.5">
                      <span className="font-medium">
                        {labels.bases.defaultTopKLabel}
                      </span>
                      <Input
                        form={settingsFormId}
                        className="bg-background border-input/80 h-9 w-full min-w-0 rounded-lg text-[13px] shadow-none focus-visible:border-blue-500/50 focus-visible:ring-blue-500/15 md:text-[13px]"
                        type="number"
                        min={1}
                        max={20}
                        required
                        value={defaultTopK}
                        onChange={(event) => {
                          touch();
                          setDefaultTopK(event.target.value);
                        }}
                      />
                      <span className="text-muted-foreground text-xs leading-5">
                        {labels.bases.defaultTopKHint}
                      </span>
                    </label>
                    <label className="grid gap-1.5">
                      <span className="font-medium">
                        {labels.bases.defaultThresholdLabel}
                      </span>
                      <Input
                        form={settingsFormId}
                        className="bg-background border-input/80 h-9 w-full min-w-0 rounded-lg text-[13px] shadow-none focus-visible:border-blue-500/50 focus-visible:ring-blue-500/15 md:text-[13px]"
                        type="number"
                        min={0}
                        max={1}
                        step={0.05}
                        required
                        value={defaultThreshold}
                        onChange={(event) => {
                          touch();
                          setDefaultThreshold(event.target.value);
                        }}
                      />
                      <span className="text-muted-foreground text-xs leading-5">
                        {labels.bases.defaultThresholdHint}
                      </span>
                    </label>
                  </div>
                </div>
              }
            />
            <div className="border-border/70 flex items-start justify-between gap-4 rounded-xl border p-4">
              <div className="space-y-1.5">
                <label
                  htmlFor={`${settingsFormId}-summary`}
                  className="font-medium"
                >
                  {labels.summary.indexLabel}
                </label>
                <p className="text-muted-foreground text-xs leading-5">
                  {labels.summary.indexHint}
                </p>
                <p className="text-muted-foreground text-xs leading-5">
                  {modelOptions.error
                    ? labels.bases.modelsLoadFailed
                    : (modelOptions.data?.summary_model?.display_name ??
                      labels.summary.modelMissing)}
                </p>
              </div>
              <Switch
                id={`${settingsFormId}-summary`}
                form={settingsFormId}
                aria-label={labels.summary.indexLabel}
                checked={summaryIndexEnabled}
                disabled={
                  updateBase.isPending ||
                  modelOptions.isLoading ||
                  (!modelOptions.data?.summary_model && !summaryIndexEnabled)
                }
                onCheckedChange={(checked) => {
                  touch();
                  setSummaryIndexEnabled(checked);
                }}
              />
            </div>
          </div>
        </section>
      ) : null}

      <div className="grid gap-3 pt-8 sm:grid-cols-[160px_minmax(0,1fr)] sm:gap-6">
        <div className="w-full max-w-[820px] space-y-3 sm:col-start-2">
          {updateBase.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(updateBase.error, labels.errors)}
            </p>
          ) : null}
          {updateBase.isSuccess ? (
            <p role="status" className="text-success text-[13px]">
              {labels.detail.settingsSaved}
            </p>
          ) : null}
          {updateBase.data?.summary_backfill ? (
            <p
              role="status"
              data-testid="knowledge-summary-backfill-outcome"
              className="text-muted-foreground text-[13px]"
            >
              {labels.summary.backfillOutcome(
                updateBase.data.summary_backfill.accepted_document_count,
                updateBase.data.summary_backfill.skipped_document_ids.length,
              )}
            </p>
          ) : null}
          <Button
            form={settingsFormId}
            className="h-9 min-w-24 rounded-lg bg-blue-600 text-[13px] text-white shadow-none hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600"
            type="submit"
            disabled={updateBase.isPending || !formValid}
          >
            {updateBase.isPending ? labels.common.saving : labels.common.save}
          </Button>
        </div>
      </div>
    </section>
  );
}

/**
 * Model rebind + full re-embed. Separate from the settings form because it
 * queues work across every document rather than editing base fields.
 */
function KnowledgeRebuildSection({
  scope,
  base,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const modelOptions = useKnowledgeModelOptions(scope, true);
  const rebuild = useRebuildKnowledgeBase(scope);
  const [embeddingModelId, setEmbeddingModelId] = useState(
    base.embedding_model_id ?? "",
  );
  const [confirmOpen, setConfirmOpen] = useState(false);

  const options = modelOptions.data?.embedding_models ?? [];

  return (
    <section
      aria-label={labels.bases.rebuildSectionTitle}
      className="grid gap-3 pt-8 sm:grid-cols-[160px_minmax(0,1fr)] sm:gap-6"
    >
      <div className="space-y-1.5 sm:pt-2.5">
        <h3 className="text-[13px] font-medium">
          {labels.bases.rebuildSectionTitle}
        </h3>
      </div>
      <div className="w-full max-w-[820px] min-w-0 space-y-3">
        <div className="flex min-w-0 flex-col gap-2 xl:flex-row xl:items-center">
          <div className="min-w-0 flex-1">
            <Select
              value={embeddingModelId}
              onValueChange={(value) => {
                rebuild.reset();
                setEmbeddingModelId(value);
              }}
            >
              <SelectTrigger
                className="bg-background border-input/80 w-full min-w-0 rounded-lg text-[13px] shadow-none focus-visible:border-blue-500/50 focus-visible:ring-blue-500/15"
                aria-label={labels.bases.rebuildModelLabel}
              >
                <SelectValue placeholder={labels.bases.modelPlaceholder} />
              </SelectTrigger>
              <SelectContent className="rounded-lg">
                {options.map((option) => (
                  <SelectItem
                    className="text-[13px] focus:bg-blue-50 focus:text-blue-700 dark:focus:bg-blue-950/30 dark:focus:text-blue-300"
                    key={option.id}
                    value={option.id}
                  >
                    {option.provider_name} · {option.model_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button
            className="border-border/70 h-9 shrink-0 rounded-lg text-[13px] shadow-none"
            type="button"
            variant="outline"
            disabled={rebuild.isPending || options.length === 0}
            onClick={() => setConfirmOpen(true)}
          >
            {rebuild.isPending
              ? labels.bases.rebuildPending
              : labels.bases.rebuildButton}
          </Button>
        </div>
        {rebuild.error ? (
          <p role="alert" className="text-destructive text-[13px]">
            {knowledgeErrorMessage(rebuild.error, labels.errors)}
          </p>
        ) : null}
        {rebuild.data ? (
          <p
            role="status"
            className="text-success text-[13px]"
            data-testid="knowledge-rebuild-outcome"
          >
            {labels.bases.rebuildOutcome(
              rebuild.data.accepted_document_count,
              rebuild.data.skipped_document_ids.length,
            )}
          </p>
        ) : null}
      </div>
      <Dialog
        open={confirmOpen}
        onOpenChange={(open) => {
          if (!open) setConfirmOpen(false);
        }}
      >
        <DialogContent className="border-border/80 rounded-xl text-[13px]">
          <DialogHeader>
            <DialogTitle className="text-base font-semibold">
              {labels.bases.rebuildConfirmTitle}
            </DialogTitle>
            <DialogDescription className="text-[13px] leading-5">
              {labels.bases.rebuildConfirmDescription(base.name)}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              className="h-9 rounded-lg text-[13px] shadow-none"
              type="button"
              variant="outline"
              onClick={() => setConfirmOpen(false)}
            >
              {labels.common.cancel}
            </Button>
            <Button
              className="h-9 rounded-lg bg-blue-600 text-[13px] text-white shadow-none hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600"
              type="button"
              disabled={rebuild.isPending}
              onClick={() => {
                setConfirmOpen(false);
                rebuild.mutate({
                  baseId: base.id,
                  embeddingModelId,
                });
              }}
            >
              {labels.bases.rebuildConfirm}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function KnowledgeMetadataPanel({
  scope,
  base,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const fields = useKnowledgeMetadataFields(scope, base.id);
  const deleteField = useDeleteKnowledgeMetadataField(scope);
  const [addOpen, setAddOpen] = useState(false);
  const [renaming, setRenaming] = useState<KnowledgeMetadataFieldItem | null>(
    null,
  );
  const [deleting, setDeleting] = useState<KnowledgeMetadataFieldItem | null>(
    null,
  );

  const typeLabel = (fieldType: KnowledgeMetadataFieldType): string =>
    fieldType === "string"
      ? labels.metadata.typeString
      : fieldType === "number"
        ? labels.metadata.typeNumber
        : labels.metadata.typeTime;

  const closeDeleteDialog = () => {
    setDeleting(null);
    deleteField.reset();
  };

  const items = fields.data ?? [];

  return (
    <section aria-label={labels.metadata.title} className="w-full space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-base font-semibold tracking-tight">
            {labels.metadata.title}
          </h2>
          <p className="text-muted-foreground text-[13px]">
            {labels.metadata.description}
          </p>
        </div>
        <Button
          className="h-9 rounded-lg text-[13px] shadow-none"
          type="button"
          onClick={() => setAddOpen(true)}
        >
          <PlusIcon aria-hidden className="size-4" />
          {labels.metadata.addButton}
        </Button>
      </div>

      {fields.isLoading ? (
        <Skeleton className="h-32 rounded-xl" />
      ) : fields.error ? (
        <p role="alert" className="text-destructive text-[13px]">
          {knowledgeErrorMessage(fields.error, labels.errors)}
        </p>
      ) : items.length === 0 ? (
        <p className="text-muted-foreground rounded-xl border border-dashed px-4 py-10 text-center text-[13px]">
          {labels.metadata.empty}
        </p>
      ) : (
        <div className="border-border/80 bg-card overflow-x-auto rounded-xl border shadow-xs">
          <table className="w-full text-left text-[13px]">
            <thead className="bg-muted/70 text-muted-foreground text-xs [&_th]:font-medium">
              <tr>
                <th className="px-4 py-3">{labels.metadata.columns.name}</th>
                <th className="px-4 py-3">{labels.metadata.columns.type}</th>
                <th className="px-4 py-3 text-right">
                  {labels.metadata.columns.actions}
                </th>
              </tr>
            </thead>
            <tbody data-testid="knowledge-metadata-field-rows">
              {items.map((field) => (
                <tr key={field.id} className="border-t">
                  <td className="px-4 py-3 font-medium">{field.name}</td>
                  <td className="text-muted-foreground px-4 py-3">
                    {typeLabel(field.field_type)}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center justify-end gap-1.5">
                      <Button
                        className="h-8 rounded-lg text-[13px] shadow-none"
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => setRenaming(field)}
                      >
                        {labels.metadata.rename}
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="text-destructive h-8 rounded-lg text-[13px] shadow-none"
                        onClick={() => setDeleting(field)}
                      >
                        {labels.metadata.delete}
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {addOpen ? (
        <AddMetadataFieldDialog
          scope={scope}
          base={base}
          onClose={() => setAddOpen(false)}
        />
      ) : null}

      {renaming ? (
        <RenameMetadataFieldDialog
          scope={scope}
          base={base}
          field={renaming}
          onClose={() => setRenaming(null)}
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
              {labels.metadata.deleteTitle}
            </DialogTitle>
            <DialogDescription className="text-[13px] leading-5">
              {deleting ? labels.metadata.deleteDescription(deleting.name) : ""}
            </DialogDescription>
          </DialogHeader>
          {deleteField.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(deleteField.error, labels.errors)}
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
              disabled={deleteField.isPending}
              onClick={() => {
                if (!deleting) return;
                deleteField.mutate(
                  { fieldId: deleting.id, baseId: base.id },
                  { onSuccess: () => closeDeleteDialog() },
                );
              }}
            >
              {deleteField.isPending
                ? labels.common.deleting
                : labels.common.delete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function AddMetadataFieldDialog({
  scope,
  base,
  onClose,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const createField = useCreateKnowledgeMetadataField(scope);
  const [name, setName] = useState("");
  const [fieldType, setFieldType] =
    useState<KnowledgeMetadataFieldType>("string");

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
            {labels.metadata.addTitle}
          </DialogTitle>
        </DialogHeader>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!name.trim()) return;
            createField.mutate(
              {
                baseId: base.id,
                input: { name: name.trim(), field_type: fieldType },
              },
              { onSuccess: () => onClose() },
            );
          }}
        >
          <label className="grid gap-1.5 text-[13px]">
            <span className="font-medium">{labels.metadata.nameLabel}</span>
            <Input
              className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
              value={name}
              required
              maxLength={64}
              placeholder={labels.metadata.namePlaceholder}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <div className="grid gap-1.5 text-[13px]">
            <span className="font-medium">{labels.metadata.typeLabel}</span>
            <Select
              value={fieldType}
              onValueChange={(value) =>
                setFieldType(value as KnowledgeMetadataFieldType)
              }
            >
              <SelectTrigger
                className="border-input/80 bg-background rounded-lg text-[13px] shadow-none"
                aria-label={labels.metadata.typeLabel}
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="rounded-lg">
                <SelectItem className="text-[13px]" value="string">
                  {labels.metadata.typeString}
                </SelectItem>
                <SelectItem className="text-[13px]" value="number">
                  {labels.metadata.typeNumber}
                </SelectItem>
                <SelectItem className="text-[13px]" value="time">
                  {labels.metadata.typeTime}
                </SelectItem>
              </SelectContent>
            </Select>
          </div>
          {createField.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(createField.error, labels.errors)}
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
              disabled={createField.isPending || !name.trim()}
            >
              {createField.isPending
                ? labels.common.creating
                : labels.common.create}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function RenameMetadataFieldDialog({
  scope,
  base,
  field,
  onClose,
}: {
  scope: ProjectClientScope;
  base: KnowledgeBaseItem;
  field: KnowledgeMetadataFieldItem;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const renameField = useRenameKnowledgeMetadataField(scope);
  const [name, setName] = useState(field.name);

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
            {labels.metadata.renameTitle(field.name)}
          </DialogTitle>
        </DialogHeader>
        <form
          className="grid gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (!name.trim()) return;
            renameField.mutate(
              { fieldId: field.id, baseId: base.id, name: name.trim() },
              { onSuccess: () => onClose() },
            );
          }}
        >
          <label className="grid gap-1.5 text-[13px]">
            <span className="font-medium">{labels.metadata.nameLabel}</span>
            <Input
              className="border-input/80 bg-background h-9 rounded-lg text-[13px] shadow-none md:text-[13px]"
              value={name}
              required
              maxLength={64}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          {renameField.error ? (
            <p role="alert" className="text-destructive text-[13px]">
              {knowledgeErrorMessage(renameField.error, labels.errors)}
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
              disabled={renameField.isPending || !name.trim()}
            >
              {renameField.isPending
                ? labels.common.saving
                : labels.common.save}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
