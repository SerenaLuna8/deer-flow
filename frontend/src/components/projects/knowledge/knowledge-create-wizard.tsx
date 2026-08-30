"use client";

import {
  ArrowLeftIcon,
  FilePlusIcon,
  FolderPlusIcon,
  RefreshCwIcon,
  UploadCloudIcon,
  XIcon,
} from "lucide-react";
import { useEffect, useRef, useState, type DragEvent } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/core/i18n/hooks";
import {
  isChildChunkSizeValid,
  isChunkOverlapValid,
  isChunkSeparatorValid,
  isChunkSizeValid,
  KNOWLEDGE_BASE_NAME_MAX_CHARS,
  KNOWLEDGE_CHILD_CHUNK_SIZE_MAX,
  KNOWLEDGE_CHILD_CHUNK_SIZE_MIN,
  KNOWLEDGE_CHUNK_OVERLAP_MAX,
  KNOWLEDGE_CHUNK_OVERLAP_MIN,
  KNOWLEDGE_CHUNK_SIZE_MAX,
  KNOWLEDGE_CHUNK_SIZE_MIN,
} from "@/core/knowledge/chunk-settings";
import {
  useCreateKnowledgeBase,
  useKnowledgeChunkPreview,
  useKnowledgeDocuments,
  useKnowledgeModelOptions,
  useUploadKnowledgeDocument,
} from "@/core/knowledge/hooks";
import {
  DEFAULT_CHILD_CHUNK_SEPARATOR,
  DEFAULT_CHUNK_SEPARATOR,
  type KnowledgeBaseItem,
  type KnowledgeChunkingMode,
  type PreviewKnowledgeChunksInput,
} from "@/core/knowledge/types";
import type { ProjectClientScope } from "@/core/private-work/types";
import { cn } from "@/lib/utils";

import {
  documentStatusVariant,
  KNOWLEDGE_UPLOAD_ACCEPT,
} from "./knowledge-documents-view";
import { knowledgeErrorMessage } from "./knowledge-error";

const ACCEPTED_EXTENSIONS = KNOWLEDGE_UPLOAD_ACCEPT;

type WizardStep = 1 | 2 | 3;

type UploadFailure = {
  fileName: string;
  message: string;
};

type KnowledgeSubmissionSnapshot = {
  files: File[];
  name: string;
  description: string;
  embeddingModelId: string;
  chunkSize: number;
  chunkOverlap: number;
  chunkSeparator: string;
  chunkingMode: KnowledgeChunkingMode;
  childChunkSize?: number;
  childChunkSeparator?: string;
  removeExtraSpaces: boolean;
  removeUrlsEmails: boolean;
};

function fileBaseName(fileName: string): string {
  const dot = fileName.lastIndexOf(".");
  return dot > 0 ? fileName.slice(0, dot) : fileName;
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

function previewInputsMatch(
  left: PreviewKnowledgeChunksInput | undefined,
  right: PreviewKnowledgeChunksInput | null,
): boolean {
  if (!left || !right) return false;
  return (
    left.file === right.file &&
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

/** Merge newly picked files, replacing entries with the same name. */
function mergeFiles(current: File[], added: File[]): File[] {
  const addedNames = new Set(added.map((file) => file.name));
  return [...current.filter((file) => !addedNames.has(file.name)), ...added];
}

function StepIndicator({
  step,
  labels,
}: {
  step: WizardStep;
  labels: { source: string; configure: string; finish: string };
}) {
  const { t } = useI18n();
  const steps: Array<[WizardStep, string]> = [
    [1, labels.source],
    [2, labels.configure],
    [3, labels.finish],
  ];
  return (
    <ol className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
      {steps.map(([value, label], index) => {
        const active = value === step;
        return (
          <li key={value} className="flex items-center gap-2">
            {index > 0 ? (
              <span aria-hidden className="text-border">
                —
              </span>
            ) : null}
            <span
              aria-current={active ? "step" : undefined}
              className={cn(
                "flex items-center gap-1.5 rounded-full px-2.5 py-1",
                active
                  ? "bg-selection-subtle/70 text-foreground font-medium"
                  : "text-muted-foreground",
              )}
            >
              <span
                className={cn(
                  "flex size-4 items-center justify-center rounded-full text-[10px] tabular-nums",
                  active
                    ? "bg-selection text-selection-foreground"
                    : "bg-muted text-muted-foreground",
                )}
              >
                {value}
              </span>
              {active ? (
                <span className="tracking-wide uppercase">
                  {t.knowledge.wizard.stepBadge(value)}
                </span>
              ) : null}
              {label}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

export function KnowledgeCreateWizard({
  scope,
  onExit,
  onCreateEmpty,
  onFinished,
}: {
  scope: ProjectClientScope;
  onExit: () => void;
  /** Switches to the plain "empty base" dialog outside the wizard. */
  onCreateEmpty: () => void;
  onFinished: (base: KnowledgeBaseItem) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const wizard = labels.wizard;

  const [step, setStep] = useState<WizardStep>(1);
  const [files, setFiles] = useState<File[]>([]);
  const [fileInputKey, setFileInputKey] = useState(0);
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [description, setDescription] = useState("");
  const [embeddingModelId, setEmbeddingModelId] = useState("");
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
  const [createdBase, setCreatedBase] = useState<KnowledgeBaseItem | null>(
    null,
  );
  const [uploadingIndex, setUploadingIndex] = useState<number | null>(null);
  const [uploadFailures, setUploadFailures] = useState<UploadFailure[]>([]);
  const [submissionSnapshot, setSubmissionSnapshot] =
    useState<KnowledgeSubmissionSnapshot | null>(null);
  const [submissionInFlightScopeKey, setSubmissionInFlightScopeKey] = useState<
    string | null
  >(null);
  const mountedRef = useRef(false);
  const submissionInFlightRef = useRef(false);
  const submissionGenerationRef = useRef(0);
  const scopeKey = `${scope.accountId}:${scope.projectId}`;
  const scopeKeyRef = useRef(scopeKey);

  useEffect(() => {
    mountedRef.current = true;
    scopeKeyRef.current = scopeKey;
    return () => {
      mountedRef.current = false;
      submissionInFlightRef.current = false;
      submissionGenerationRef.current += 1;
    };
  }, [scopeKey]);

  const options = useKnowledgeModelOptions(scope, step === 2);
  const createBase = useCreateKnowledgeBase(scope);
  const upload = useUploadKnowledgeDocument(scope);
  const documents = useKnowledgeDocuments(scope, createdBase?.id ?? null);
  const preview = useKnowledgeChunkPreview(scope);
  const isSubmitting =
    submissionInFlightScopeKey === scopeKey || createBase.isPending;

  const parsedChunkSize = Number.parseInt(chunkSize, 10);
  const parsedChunkOverlap = Number.parseInt(chunkOverlap, 10);
  // Bounds mirror the backend exactly: the base is created before the files
  // upload, so a looser client check would strand the user with an empty base.
  const chunkParamsValid =
    isChunkSizeValid(parsedChunkSize) &&
    isChunkOverlapValid(parsedChunkOverlap, parsedChunkSize);
  const separatorValid = isChunkSeparatorValid(chunkSeparator);
  const parsedChildChunkSize = Number.parseInt(childChunkSize, 10);
  const childParamsValid =
    chunkingMode === "general" ||
    (isChildChunkSizeValid(parsedChildChunkSize, parsedChunkSize) &&
      isChunkSeparatorValid(childChunkSeparator));
  const configureValid =
    name.trim().length > 0 &&
    embeddingModelId !== "" &&
    chunkParamsValid &&
    separatorValid &&
    childParamsValid;

  const previewFile = files[0] ?? null;
  // Preview requests are deliberate full-file uploads. Entering step 2 starts
  // one request; later edits only make that exact response stale until the
  // user explicitly refreshes it.
  const currentPreviewInput: PreviewKnowledgeChunksInput | null =
    previewFile && chunkParamsValid && separatorValid && childParamsValid
      ? {
          file: previewFile,
          chunk_size: parsedChunkSize,
          chunk_overlap: parsedChunkOverlap,
          chunk_separator: chunkSeparator,
          remove_extra_spaces: removeExtraSpaces,
          remove_urls_emails: removeUrlsEmails,
          chunking_mode: chunkingMode,
          // Hidden child fields are not part of a general-mode request or its
          // freshness identity.
          ...(chunkingMode === "parent_child"
            ? {
                child_chunk_size: parsedChildChunkSize,
                child_chunk_separator: childChunkSeparator,
              }
            : {}),
        }
      : null;
  const previewMatchesFile = preview.variables?.file === previewFile;
  const previewIsStale =
    previewMatchesFile &&
    !previewInputsMatch(preview.variables, currentPreviewInput);
  const visiblePreviewData = previewMatchesFile ? preview.data : undefined;
  const visiblePreviewError =
    previewMatchesFile && !previewIsStale ? preview.error : null;

  const requestCurrentPreview = () => {
    if (!currentPreviewInput) return;
    preview.mutate(currentPreviewInput);
  };

  const enterConfigureStep = () => {
    setStep(2);
    // The automatic upload belongs only to a first-file change. Returning to
    // the same file preserves success/error/stale state; later parameter edits
    // still require the explicit refresh action.
    if (!previewMatchesFile && currentPreviewInput) {
      preview.mutate(currentPreviewInput);
    }
  };

  const addFiles = (added: File[]) => {
    if (added.length === 0) return;
    const next = mergeFiles(files, added);
    setFiles(next);
    if (!nameTouched) {
      // Derived names obey the same backend limit as typed ones.
      setName(
        fileBaseName(next[0]?.name ?? "").slice(
          0,
          KNOWLEDGE_BASE_NAME_MAX_CHARS,
        ),
      );
    }
    // Selecting the same file twice must still fire the change event.
    setFileInputKey((key) => key + 1);
  };

  const handleDrop = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    addFiles(Array.from(event.dataTransfer.files));
  };

  const startProcessing = async () => {
    if (
      !configureValid ||
      files.length === 0 ||
      submissionInFlightRef.current
    ) {
      return;
    }
    submissionInFlightRef.current = true;
    setSubmissionInFlightScopeKey(scopeKey);
    const submissionGeneration = submissionGenerationRef.current + 1;
    submissionGenerationRef.current = submissionGeneration;
    const submissionScopeKey = scopeKey;
    const isSubmissionCurrent = () =>
      mountedRef.current &&
      scopeKeyRef.current === submissionScopeKey &&
      submissionGenerationRef.current === submissionGeneration;
    const snapshot: KnowledgeSubmissionSnapshot = {
      files: [...files],
      name: name.trim(),
      description: description.trim(),
      embeddingModelId,
      chunkSize: parsedChunkSize,
      chunkOverlap: parsedChunkOverlap,
      chunkSeparator,
      chunkingMode,
      ...(chunkingMode === "parent_child"
        ? {
            childChunkSize: parsedChildChunkSize,
            childChunkSeparator,
          }
        : {}),
      removeExtraSpaces,
      removeUrlsEmails,
    };
    setSubmissionSnapshot(snapshot);
    try {
      let base: KnowledgeBaseItem;
      try {
        base = await createBase.mutateAsync({
          name: snapshot.name,
          embedding_model_id: snapshot.embeddingModelId,
          description: snapshot.description,
        });
      } catch {
        // The mutation error renders in step 2.
        if (isSubmissionCurrent()) setSubmissionSnapshot(null);
        return;
      }
      if (!isSubmissionCurrent()) return;
      setCreatedBase(base);
      setStep(3);

      const failures: UploadFailure[] = [];
      for (const [index, file] of snapshot.files.entries()) {
        if (!isSubmissionCurrent()) return;
        setUploadingIndex(index);
        try {
          await upload.mutateAsync({
            baseId: base.id,
            input: {
              file,
              chunk_size: snapshot.chunkSize,
              chunk_overlap: snapshot.chunkOverlap,
              chunk_separator: snapshot.chunkSeparator,
              remove_extra_spaces: snapshot.removeExtraSpaces,
              remove_urls_emails: snapshot.removeUrlsEmails,
              chunking_mode: snapshot.chunkingMode,
              ...(snapshot.chunkingMode === "parent_child"
                ? {
                    child_chunk_size: snapshot.childChunkSize,
                    child_chunk_separator: snapshot.childChunkSeparator,
                  }
                : {}),
            },
          });
        } catch (error) {
          if (!isSubmissionCurrent()) return;
          failures.push({
            fileName: file.name,
            message: knowledgeErrorMessage(error, labels.errors),
          });
          setUploadFailures([...failures]);
        }
        if (!isSubmissionCurrent()) return;
      }
    } finally {
      if (isSubmissionCurrent()) {
        submissionInFlightRef.current = false;
        setSubmissionInFlightScopeKey(null);
        setUploadingIndex(null);
      }
    }
  };

  const selectedEmbeddingOption = options.data?.embedding_models.find(
    (option) =>
      option.id === (submissionSnapshot?.embeddingModelId ?? embeddingModelId),
  );
  const modelDisplayName = selectedEmbeddingOption
    ? `${selectedEmbeddingOption.provider_name} · ${selectedEmbeddingOption.model_name}`
    : "";

  return (
    <section aria-label={wizard.uploadCreateTitle} className="space-y-6">
      <div className="flex flex-col gap-3 border-b pb-4 lg:flex-row lg:items-center lg:justify-between">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="w-fit"
          // Leaving mid-upload would keep the loop running invisibly with no
          // progress or failure surface; the exit waits like "go to documents".
          disabled={isSubmitting}
          onClick={onExit}
          aria-label={labels.common.back}
        >
          <ArrowLeftIcon aria-hidden className="size-4" />
          {labels.page.title}
        </Button>
        <StepIndicator step={step} labels={wizard.steps} />
      </div>

      {step === 1 ? (
        <div className="mx-auto w-full max-w-2xl space-y-6">
          <div className="space-y-3">
            <h2 className="text-sm font-semibold">
              {wizard.sourceSectionTitle}
            </h2>
            <label
              className="border-border hover:border-selection/60 hover:bg-muted/30 focus-within:ring-ring flex cursor-pointer flex-col items-center gap-2 rounded-xl border border-dashed px-6 py-10 text-center transition-colors focus-within:ring-2"
              onDragOver={(event) => event.preventDefault()}
              onDrop={handleDrop}
            >
              <UploadCloudIcon
                aria-hidden
                className="text-muted-foreground size-6"
              />
              <span className="text-sm font-medium">
                {wizard.dropzoneTitle}
              </span>
              <span className="text-muted-foreground text-xs">
                {labels.documents.uploadDescription}
              </span>
              <input
                key={fileInputKey}
                type="file"
                multiple
                accept={ACCEPTED_EXTENSIONS}
                aria-label={labels.documents.fileLabel}
                className="sr-only"
                onChange={(event) =>
                  addFiles(Array.from(event.target.files ?? []))
                }
              />
            </label>

            {files.length > 0 ? (
              <div className="space-y-2">
                <p
                  role="status"
                  className="text-muted-foreground text-xs tabular-nums"
                >
                  {wizard.filesSelected(files.length)}
                </p>
                <ul className="divide-border/60 divide-y overflow-hidden rounded-xl border">
                  {files.map((file) => (
                    <li
                      key={file.name}
                      className="flex min-w-0 items-center gap-3 px-4 py-2.5 text-sm"
                    >
                      <FilePlusIcon
                        aria-hidden
                        className="text-muted-foreground size-4 shrink-0"
                      />
                      <span className="min-w-0 flex-1 truncate">
                        {file.name}
                      </span>
                      <span className="text-muted-foreground shrink-0 text-xs">
                        {formatSizeBytes(file.size)}
                      </span>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="size-7 shrink-0"
                        aria-label={wizard.removeFile(file.name)}
                        onClick={() =>
                          setFiles((current) =>
                            current.filter(
                              (candidate) => candidate.name !== file.name,
                            ),
                          )
                        }
                      >
                        <XIcon aria-hidden className="size-3.5" />
                      </Button>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>

          <div className="flex justify-end">
            <Button
              type="button"
              disabled={files.length === 0}
              onClick={enterConfigureStep}
            >
              {wizard.next}
            </Button>
          </div>

          <div className="border-t pt-4">
            <button
              type="button"
              className="text-selection hover:text-selection/80 focus-visible:ring-ring flex items-center gap-1.5 rounded-md text-sm focus-visible:ring-2 focus-visible:outline-none"
              onClick={onCreateEmpty}
            >
              <FolderPlusIcon aria-hidden className="size-4" />
              {wizard.emptyCreateTitle}
            </button>
          </div>
        </div>
      ) : null}

      {step === 2 ? (
        <div className="mx-auto grid w-full max-w-6xl gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <form
            className="w-full space-y-6"
            onSubmit={(event) => {
              event.preventDefault();
              void startProcessing();
            }}
          >
            <div className="space-y-3">
              <h2 className="text-sm font-semibold">
                {wizard.chunkSectionTitle}
              </h2>
              <fieldset className="grid gap-2 text-sm">
                <legend className="sr-only">
                  {labels.documents.chunkingModeLabel}
                </legend>
                {(
                  [
                    [
                      "general",
                      labels.documents.chunkingModeGeneral,
                      labels.documents.chunkingModeGeneralHint,
                    ],
                    [
                      "parent_child",
                      labels.documents.chunkingModeParentChild,
                      labels.documents.chunkingModeParentChildHint,
                    ],
                  ] as const
                ).map(([mode, label, hint]) => (
                  <label
                    key={mode}
                    className={cn(
                      "flex cursor-pointer items-start gap-2.5 rounded-xl border px-3.5 py-2.5",
                      chunkingMode === mode
                        ? "border-selection bg-selection-subtle/40"
                        : "border-border hover:bg-muted/40",
                    )}
                  >
                    <input
                      type="radio"
                      name="chunking-mode"
                      value={mode}
                      className="accent-primary mt-0.5 size-4"
                      checked={chunkingMode === mode}
                      disabled={isSubmitting}
                      onChange={() => setChunkingMode(mode)}
                    />
                    <span className="grid gap-0.5">
                      <span className="font-medium">{label}</span>
                      <span className="text-muted-foreground text-xs">
                        {hint}
                      </span>
                    </span>
                  </label>
                ))}
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
                    disabled={isSubmitting}
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
                    disabled={isSubmitting}
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
                  disabled={isSubmitting}
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
                      disabled={isSubmitting}
                      value={childChunkSize}
                      onChange={(event) =>
                        setChildChunkSize(event.target.value)
                      }
                    />
                  </label>
                  <label className="grid gap-1.5 text-sm">
                    <span className="font-medium">
                      {labels.documents.childChunkSeparatorLabel}
                    </span>
                    <Input
                      required
                      maxLength={64}
                      disabled={isSubmitting}
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
                    disabled={isSubmitting}
                    onChange={(event) =>
                      setRemoveExtraSpaces(event.target.checked)
                    }
                  />
                  {labels.documents.removeExtraSpacesLabel}
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    className="accent-primary size-4"
                    checked={removeUrlsEmails}
                    disabled={isSubmitting}
                    onChange={(event) =>
                      setRemoveUrlsEmails(event.target.checked)
                    }
                  />
                  {labels.documents.removeUrlsEmailsLabel}
                </label>
              </fieldset>
              <p className="text-muted-foreground text-xs">
                {labels.documents.chunkImmutableNote}
              </p>
            </div>

            <div className="space-y-3">
              <h2 className="text-sm font-semibold">
                {labels.bases.modelLabel}
              </h2>
              {options.isLoading ? (
                <Skeleton className="h-9 rounded-md" />
              ) : options.error ? (
                <p role="alert" className="text-destructive text-sm">
                  {labels.bases.modelsLoadFailed}
                </p>
              ) : (options.data?.embedding_models.length ?? 0) === 0 ? (
                <p className="text-muted-foreground text-sm">
                  {labels.bases.noModels}
                </p>
              ) : (
                <Select
                  value={embeddingModelId}
                  disabled={isSubmitting}
                  onValueChange={setEmbeddingModelId}
                >
                  <SelectTrigger aria-label={labels.bases.modelLabel}>
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
              <p className="text-muted-foreground text-xs">
                {labels.bases.modelHint}
              </p>
            </div>

            <div className="space-y-3">
              <h2 className="text-sm font-semibold">
                {wizard.infoSectionTitle}
              </h2>
              <label className="grid gap-1.5 text-sm">
                <span className="font-medium">{labels.bases.nameLabel}</span>
                <Input
                  value={name}
                  required
                  maxLength={KNOWLEDGE_BASE_NAME_MAX_CHARS}
                  disabled={isSubmitting}
                  placeholder={labels.bases.namePlaceholder}
                  onChange={(event) => {
                    setNameTouched(true);
                    setName(event.target.value);
                  }}
                />
              </label>
              <label className="grid gap-1.5 text-sm">
                <span className="font-medium">
                  {labels.bases.descriptionLabel}
                </span>
                <Textarea
                  value={description}
                  rows={3}
                  disabled={isSubmitting}
                  placeholder={labels.bases.descriptionPlaceholder}
                  onChange={(event) => setDescription(event.target.value)}
                />
              </label>
            </div>

            {createBase.error ? (
              <p role="alert" className="text-destructive text-sm">
                {knowledgeErrorMessage(createBase.error, labels.errors)}
              </p>
            ) : null}

            <div className="flex items-center justify-between gap-3">
              <Button
                type="button"
                variant="outline"
                disabled={isSubmitting}
                onClick={() => setStep(1)}
              >
                {wizard.previous}
              </Button>
              <Button type="submit" disabled={isSubmitting || !configureValid}>
                {createBase.isPending
                  ? labels.common.creating
                  : wizard.saveAndProcess}
              </Button>
            </div>
          </form>

          <aside
            aria-label={wizard.previewTitle}
            className="min-w-0 space-y-3 lg:border-l lg:pl-8"
            data-testid="chunk-preview-panel"
            aria-busy={preview.isPending}
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="space-y-1">
                <div className="flex flex-wrap items-baseline gap-2">
                  <h2 className="text-sm font-semibold">
                    {wizard.previewTitle}
                  </h2>
                  {visiblePreviewData ? (
                    <span className="text-muted-foreground text-xs tabular-nums">
                      {wizard.previewTotal(visiblePreviewData.total)}
                    </span>
                  ) : null}
                </div>
                {previewFile ? (
                  <p className="text-muted-foreground max-w-sm truncate text-xs">
                    {wizard.previewHint(previewFile.name)}
                  </p>
                ) : null}
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={
                  isSubmitting || !currentPreviewInput || preview.isPending
                }
                onClick={requestCurrentPreview}
              >
                <RefreshCwIcon
                  aria-hidden
                  className={cn(
                    "size-3.5",
                    preview.isPending && "animate-spin",
                  )}
                />
                {preview.isPending
                  ? wizard.previewLoading
                  : wizard.previewRefresh}
              </Button>
            </div>
            {preview.isPending ? (
              <p role="status" className="text-muted-foreground text-xs">
                {wizard.previewLoading}
              </p>
            ) : !currentPreviewInput ? (
              <p role="status" className="text-destructive text-xs">
                {wizard.previewInvalid}
              </p>
            ) : previewIsStale ? (
              <p role="status" className="text-muted-foreground text-xs">
                {wizard.previewStale}
              </p>
            ) : null}
            {visiblePreviewError ? (
              <p role="alert" className="text-destructive text-sm">
                {knowledgeErrorMessage(visiblePreviewError, labels.errors)}
              </p>
            ) : null}
            {visiblePreviewData ? (
              <ul
                className={cn(
                  "grid max-h-[32rem] gap-3 overflow-y-auto pr-1 transition-opacity",
                  previewIsStale && "opacity-60",
                )}
              >
                {visiblePreviewData.items.map((chunk) => (
                  <li
                    key={chunk.position}
                    className="bg-muted/30 space-y-1.5 rounded-xl border px-4 py-3"
                  >
                    <p className="text-muted-foreground flex items-center justify-between gap-2 text-xs tabular-nums">
                      <span className="font-medium">
                        {wizard.previewChunkLabel(chunk.position)}
                      </span>
                      <span>
                        {chunk.child_contents.length > 0
                          ? `${wizard.previewChildCount(chunk.child_contents.length)} · `
                          : null}
                        {wizard.previewCharacters(chunk.word_count)}
                      </span>
                    </p>
                    <p className="text-sm break-words whitespace-pre-wrap">
                      {chunk.content}
                    </p>
                    {chunk.child_contents.length > 0 ? (
                      <ol className="border-border/70 mt-2 grid gap-1.5 border-l-2 pl-3">
                        {chunk.child_contents.map((childContent, index) => (
                          <li
                            key={index}
                            className="bg-background/60 rounded-lg border px-2.5 py-1.5"
                          >
                            <p className="text-muted-foreground text-[10px] font-medium tabular-nums">
                              {wizard.previewChildLabel(index + 1)}
                            </p>
                            <p className="text-xs break-words whitespace-pre-wrap">
                              {childContent}
                            </p>
                          </li>
                        ))}
                      </ol>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}
          </aside>
        </div>
      ) : null}

      {step === 3 && createdBase && submissionSnapshot ? (
        <div className="mx-auto w-full max-w-2xl space-y-6">
          <div className="space-y-1">
            <h2 className="text-lg font-semibold">{wizard.createdTitle}</h2>
            <p className="text-muted-foreground text-sm">
              {wizard.createdHint}
            </p>
          </div>

          <div className="space-y-3">
            <h3 className="text-sm font-semibold">{wizard.processingTitle}</h3>
            {uploadingIndex !== null ? (
              <p role="status" className="text-muted-foreground text-xs">
                {labels.documents.uploadingProgress(
                  uploadingIndex + 1,
                  submissionSnapshot.files.length,
                )}
              </p>
            ) : null}
            <ul
              className="divide-border/60 divide-y overflow-hidden rounded-xl border"
              data-testid="wizard-document-status"
            >
              {(documents.data?.items ?? []).map((document) => (
                <li
                  key={document.id}
                  className="flex min-w-0 items-center gap-3 px-4 py-2.5 text-sm"
                >
                  <span className="min-w-0 flex-1 truncate">
                    {document.name}
                  </span>
                  <Badge variant={documentStatusVariant(document.status)}>
                    {labels.documentStatus[document.status]}
                  </Badge>
                </li>
              ))}
            </ul>
            {uploadFailures.length > 0 ? (
              <div className="space-y-1">
                <p role="alert" className="text-destructive text-xs">
                  {wizard.uploadFailedNote}
                </p>
                <ul className="grid gap-1 text-xs">
                  {uploadFailures.map((failure) => (
                    <li key={failure.fileName} className="text-destructive">
                      {labels.documents.uploadResultFailed(
                        failure.fileName,
                        failure.message,
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>

          <div className="space-y-3">
            <h3 className="text-sm font-semibold">{wizard.summaryTitle}</h3>
            <dl className="divide-border/60 divide-y overflow-hidden rounded-xl border text-sm">
              <div className="flex items-center justify-between gap-3 px-4 py-2.5">
                <dt className="text-muted-foreground">
                  {labels.bases.nameLabel}
                </dt>
                <dd className="min-w-0 truncate font-medium">
                  {createdBase.name}
                </dd>
              </div>
              <div className="flex items-center justify-between gap-3 px-4 py-2.5">
                <dt className="text-muted-foreground">
                  {labels.documents.chunkingModeLabel}
                </dt>
                <dd>
                  {submissionSnapshot.chunkingMode === "parent_child"
                    ? labels.documents.chunkingModeParentChild
                    : labels.documents.chunkingModeGeneral}
                </dd>
              </div>
              <div className="flex items-center justify-between gap-3 px-4 py-2.5">
                <dt className="text-muted-foreground">
                  {labels.documents.chunkSizeLabel}
                </dt>
                <dd className="tabular-nums">{submissionSnapshot.chunkSize}</dd>
              </div>
              <div className="flex items-center justify-between gap-3 px-4 py-2.5">
                <dt className="text-muted-foreground">
                  {labels.documents.chunkOverlapLabel}
                </dt>
                <dd className="tabular-nums">
                  {submissionSnapshot.chunkOverlap}
                </dd>
              </div>
              <div className="flex items-center justify-between gap-3 px-4 py-2.5">
                <dt className="text-muted-foreground">
                  {labels.documents.chunkSeparatorLabel}
                </dt>
                <dd className="font-mono text-xs">
                  {submissionSnapshot.chunkSeparator}
                </dd>
              </div>
              {submissionSnapshot.chunkingMode === "parent_child" ? (
                <>
                  <div className="flex items-center justify-between gap-3 px-4 py-2.5">
                    <dt className="text-muted-foreground">
                      {labels.documents.childChunkSizeLabel}
                    </dt>
                    <dd className="tabular-nums">
                      {submissionSnapshot.childChunkSize}
                    </dd>
                  </div>
                  <div className="flex items-center justify-between gap-3 px-4 py-2.5">
                    <dt className="text-muted-foreground">
                      {labels.documents.childChunkSeparatorLabel}
                    </dt>
                    <dd className="font-mono text-xs">
                      {submissionSnapshot.childChunkSeparator}
                    </dd>
                  </div>
                </>
              ) : null}
              <div className="flex items-center justify-between gap-3 px-4 py-2.5">
                <dt className="text-muted-foreground">
                  {labels.bases.modelLabel}
                </dt>
                <dd className="min-w-0 truncate">{modelDisplayName}</dd>
              </div>
            </dl>
          </div>

          <div className="flex justify-end">
            <Button
              type="button"
              disabled={isSubmitting}
              onClick={() => onFinished(createdBase)}
            >
              {wizard.goToDocuments}
            </Button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
