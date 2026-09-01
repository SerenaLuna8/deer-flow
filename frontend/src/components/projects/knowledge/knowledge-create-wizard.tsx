"use client";

import {
  ArrowLeftIcon,
  ArrowRightIcon,
  BookOpenIcon,
  CheckIcon,
  LayersIcon,
  WaypointsIcon,
  FolderPlusIcon,
  RefreshCwIcon,
  UploadCloudIcon,
  XIcon,
} from "lucide-react";
import { useEffect, useReducer, useRef, useState, type DragEvent } from "react";

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
import { isKnowledgeConflictError } from "@/core/knowledge/api";
import { createPreviewImageURLs } from "@/core/knowledge/attachment-images";
import {
  isChildChunkSizeValid,
  isChunkOverlapValid,
  isChunkSeparatorValid,
  isChunkSizeValid,
  knowledgeFileExtension,
  normalizeKnowledgeHeaderRules,
  KNOWLEDGE_BASE_NAME_MAX_CHARS,
  KNOWLEDGE_CHUNK_OVERLAP_MIN,
} from "@/core/knowledge/chunk-settings";
import {
  useCreateKnowledgeBase,
  useKnowledgeChunkPreview,
  useKnowledgeDocuments,
  useKnowledgeFileCapabilities,
  useKnowledgeModelOptions,
  useUpdateKnowledgeBase,
  useUploadKnowledgeDocument,
} from "@/core/knowledge/hooks";
import {
  KNOWLEDGE_PREVIEW_IDLE,
  knowledgePreviewReducer,
  matchingPreviewFingerprint,
  previewParamsEqual,
  previewProcessingParameters,
  type KnowledgePreviewParams,
  type KnowledgeSuccessfulPreview,
} from "@/core/knowledge/preview-identity";
import {
  DEFAULT_CHILD_CHUNK_SEPARATOR,
  DEFAULT_CHUNK_SEPARATOR,
  type KnowledgeBaseItem,
  type KnowledgeChunkPreviewResponse,
  type KnowledgeChunkingMode,
  type KnowledgeFileCapabilities,
  type KnowledgeHeaderRule,
  type KnowledgeProcessingParameters,
  type KnowledgeRetrievalMode,
} from "@/core/knowledge/types";
import type { ProjectClientScope } from "@/core/private-work/types";
import { cn } from "@/lib/utils";

import {
  documentStatusClassName,
  documentStatusVariant,
} from "./knowledge-documents-view";
import { knowledgeErrorMessage } from "./knowledge-error";
import { KnowledgeFileTypeIcon } from "./knowledge-file-type-icon";
import { KnowledgeHeaderSettings } from "./knowledge-header-settings";
import type { KnowledgeImageSource } from "./knowledge-image";
import { KnowledgeMarkdown } from "./knowledge-markdown";
import { KnowledgeRetrievalModeField } from "./knowledge-retrieval-mode-field";

type WizardStep = 1 | 2 | 3;

type UploadFailure = {
  fileName: string;
  message: string;
  stalePreview?: boolean;
};

type KnowledgeSubmissionFile = {
  file: File;
  processingProfile: KnowledgeProcessingParameters;
  expectedPreviewFingerprint?: string;
};

type KnowledgeSubmissionSnapshot = {
  files: KnowledgeSubmissionFile[];
  name: string;
  description: string;
  displayName?: string;
  embeddingModelId: string;
  rerankerModelId: string;
  retrievalMode: KnowledgeRetrievalMode;
  chunkSize: number;
  chunkOverlap: number;
  chunkSeparator: string;
  chunkingMode: KnowledgeChunkingMode;
  childChunkSize?: number;
  childChunkSeparator?: string;
  removeExtraSpaces: boolean;
  removeUrlsEmails: boolean;
};

const EMPTY_KNOWLEDGE_IMAGE_SOURCES: ReadonlyMap<string, KnowledgeImageSource> =
  new Map();

function PreviewChunkList({
  data,
  scopeKey,
  stale,
  labels,
}: {
  data: KnowledgeChunkPreviewResponse;
  scopeKey: string;
  stale: boolean;
  labels: ReturnType<typeof useI18n>["t"]["knowledge"]["wizard"];
}) {
  const resourceIdentity = `${scopeKey}:${data.preview_fingerprint}:${stale ? "stale" : "current"}`;
  const [imageState, setImageState] = useState<{
    identity: string;
    sources: ReadonlyMap<string, KnowledgeImageSource>;
  } | null>(null);

  useEffect(() => {
    if (stale) return;
    let resources: ReturnType<typeof createPreviewImageURLs> | null = null;
    try {
      resources = createPreviewImageURLs(data.preview_attachments);
      setImageState({
        identity: resourceIdentity,
        sources: new Map(
          [...resources.urls].map(([ref, url]) => [
            ref,
            { kind: "preview", url } as const,
          ]),
        ),
      });
    } catch {
      setImageState({
        identity: resourceIdentity,
        sources: EMPTY_KNOWLEDGE_IMAGE_SOURCES,
      });
    }
    return () => resources?.dispose();
  }, [data, resourceIdentity, stale]);

  const imageSources =
    imageState?.identity === resourceIdentity
      ? imageState.sources
      : EMPTY_KNOWLEDGE_IMAGE_SOURCES;

  return (
    <ul className={cn("grid gap-6 transition-opacity", stale && "opacity-60")}>
      {data.items.map((chunk) => (
        <li key={chunk.position} className="space-y-2">
          <p className="text-muted-foreground flex items-center justify-between gap-2 text-xs tabular-nums">
            <span className="font-medium">
              {labels.previewChunkLabel(chunk.position)}
            </span>
            <span>
              {chunk.child_contents.length > 0
                ? `${labels.previewChildCount(chunk.child_contents.length)} · `
                : null}
              {labels.previewCharacters(chunk.word_count)}
            </span>
          </p>
          {chunk.child_contents.length === 0 ? (
            <KnowledgeMarkdown
              content={chunk.content}
              imageSources={imageSources}
              scopeKey={scopeKey}
              className="text-[13px] leading-6 break-words"
            />
          ) : null}
          {chunk.child_contents.length > 0 ? (
            <ol className="flex flex-wrap items-start gap-x-1 gap-y-1.5">
              {chunk.child_contents.map((childContent, index) => (
                <li
                  key={index}
                  className="bg-muted/60 max-w-full rounded-sm px-1.5 py-0.5 text-[13px] leading-6 break-words"
                >
                  <span className="text-muted-foreground mr-1.5 inline-block text-[10px] font-medium tabular-nums">
                    {labels.previewChildLabel(index + 1)}
                  </span>
                  <KnowledgeMarkdown
                    content={childContent}
                    imageSources={imageSources}
                    scopeKey={scopeKey}
                    className="inline whitespace-normal"
                  />
                </li>
              ))}
            </ol>
          ) : null}
          {chunk.child_contents.length > 0 ? (
            <details className="text-muted-foreground text-xs">
              <summary className="hover:text-foreground cursor-pointer py-1">
                {labels.previewParentText}
              </summary>
              <KnowledgeMarkdown
                content={chunk.content}
                imageSources={imageSources}
                scopeKey={scopeKey}
                className="pt-1 text-[13px] leading-6 break-words"
              />
            </details>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

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

/** Merge newly picked files, replacing entries with the same name. */
function mergeFiles(current: File[], added: File[]): File[] {
  const addedNames = new Set(added.map((file) => file.name));
  return [...current.filter((file) => !addedNames.has(file.name)), ...added];
}

function capabilityForFile(
  file: File,
  capabilities: KnowledgeFileCapabilities | undefined,
) {
  const extension = knowledgeFileExtension(file.name);
  return extension === null
    ? undefined
    : capabilities?.formats.find((item) => item.extension === extension);
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
    <ol className="flex min-w-0 flex-wrap items-center justify-center gap-2 text-[13px]">
      {steps.map(([value, label], index) => {
        const active = value === step;
        return (
          <li key={value} className="flex items-center gap-2">
            {index > 0 ? (
              <span aria-hidden className="bg-border mx-1 h-px w-5" />
            ) : null}
            <span
              aria-current={active ? "step" : undefined}
              className={cn(
                "flex items-center gap-2 whitespace-nowrap",
                active
                  ? "font-medium text-blue-600 dark:text-blue-400"
                  : "text-muted-foreground",
              )}
            >
              <span
                className={cn(
                  "flex h-5 items-center justify-center rounded-full text-[10px] font-medium tabular-nums",
                  active
                    ? "bg-blue-600 px-2 text-white"
                    : "border-border size-5 border",
                )}
              >
                {active ? t.knowledge.wizard.stepBadge(value) : value}
              </span>
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
  existingBase,
  onExit,
  onCreateEmpty,
  onFinished,
}: {
  scope: ProjectClientScope;
  existingBase?: KnowledgeBaseItem;
  onExit: () => void;
  /** Switches to the plain "empty base" dialog outside the wizard. */
  onCreateEmpty?: () => void;
  onFinished: (base: KnowledgeBaseItem) => void;
}) {
  const { t } = useI18n();
  const labels = t.knowledge;
  const wizard = labels.wizard;
  const isExistingUpload = existingBase !== undefined;
  const modelsLocked =
    existingBase !== undefined && existingBase.embedding_model_id !== null;

  const [step, setStep] = useState<WizardStep>(1);
  const [files, setFiles] = useState<File[]>([]);
  const [previewFileName, setPreviewFileName] = useState<string | null>(null);
  const [headerRulesByFile, setHeaderRulesByFile] = useState<
    Map<File, KnowledgeHeaderRule[]>
  >(() => new Map());
  const [successfulPreviews, setSuccessfulPreviews] = useState<
    Map<File, KnowledgeSuccessfulPreview>
  >(() => new Map());
  const [fileInputKey, setFileInputKey] = useState(0);
  const [name, setName] = useState(existingBase?.name ?? "");
  const [nameTouched, setNameTouched] = useState(false);
  const [description, setDescription] = useState(
    existingBase?.description ?? "",
  );
  const [displayName, setDisplayName] = useState("");
  const [embeddingModelId, setEmbeddingModelId] = useState(
    existingBase?.embedding_model_id ?? "",
  );
  const [rerankerModelId, setRerankerModelId] = useState(
    existingBase?.reranker_model_id ?? "",
  );
  const [retrievalMode, setRetrievalMode] = useState<KnowledgeRetrievalMode>(
    existingBase?.retrieval_mode ?? "semantic",
  );
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
  const [uploadingTotal, setUploadingTotal] = useState(0);
  const [uploadedDocumentIds, setUploadedDocumentIds] = useState<string[]>([]);
  const [uploadFailures, setUploadFailures] = useState<UploadFailure[]>([]);
  const [submissionSnapshot, setSubmissionSnapshot] =
    useState<KnowledgeSubmissionSnapshot | null>(null);
  const [submissionInFlightScopeKey, setSubmissionInFlightScopeKey] = useState<
    string | null
  >(null);
  const mountedRef = useRef(false);
  const submissionInFlightRef = useRef(false);
  const submissionGenerationRef = useRef(0);
  const previewAbortControllerRef = useRef<AbortController | null>(null);
  const previewActiveIdentityRef = useRef<{
    file: File;
    scopeKey: string;
    sequence: number;
  } | null>(null);
  const scopeKey = `${scope.accountId}:${scope.projectId}`;
  const scopeKeyRef = useRef(scopeKey);

  useEffect(() => {
    mountedRef.current = true;
    scopeKeyRef.current = scopeKey;
    return () => {
      mountedRef.current = false;
      submissionInFlightRef.current = false;
      submissionGenerationRef.current += 1;
      previewAbortControllerRef.current?.abort();
      previewAbortControllerRef.current = null;
      previewActiveIdentityRef.current = null;
    };
  }, [scopeKey]);

  const fileCapabilities = useKnowledgeFileCapabilities(scope);
  const options = useKnowledgeModelOptions(scope, step === 2);
  const createBase = useCreateKnowledgeBase(scope);
  const updateBase = useUpdateKnowledgeBase(scope);
  const upload = useUploadKnowledgeDocument(scope);
  const documents = useKnowledgeDocuments(scope, createdBase?.id ?? null);
  const preview = useKnowledgeChunkPreview(scope);
  const previewScopeKey = `${scopeKey}:${fileCapabilities.data?.capability_revision ?? "unavailable"}`;
  const [previewState, dispatchPreview] = useReducer(
    knowledgePreviewReducer,
    KNOWLEDGE_PREVIEW_IDLE,
  );
  const previewSequenceRef = useRef(0);
  const autoPreviewedFileRef = useRef<File | null>(null);
  const isSubmitting =
    submissionInFlightScopeKey === scopeKey ||
    createBase.isPending ||
    updateBase.isPending;
  const effectiveEmbeddingModelId = modelsLocked
    ? existingBase.embedding_model_id
    : embeddingModelId;
  const effectiveRerankerModelId = modelsLocked
    ? (existingBase.reranker_model_id ?? "")
    : rerankerModelId;
  const effectiveRetrievalMode = modelsLocked
    ? existingBase.retrieval_mode
    : retrievalMode;
  const initializationError = isExistingUpload
    ? updateBase.error
    : createBase.error;

  const parsedChunkSize = Number.parseInt(chunkSize, 10);
  const parsedChunkOverlap = Number.parseInt(chunkOverlap, 10);
  const chunkLimits = fileCapabilities.data?.chunk_limits;
  const chunkParamsValid =
    chunkLimits !== undefined &&
    isChunkSizeValid(parsedChunkSize, chunkLimits) &&
    isChunkOverlapValid(parsedChunkOverlap, parsedChunkSize, chunkLimits);
  const separatorValid = isChunkSeparatorValid(chunkSeparator);
  const parsedChildChunkSize = Number.parseInt(childChunkSize, 10);
  const childParamsValid =
    chunkLimits !== undefined &&
    (chunkingMode === "general" ||
      (isChildChunkSizeValid(
        parsedChildChunkSize,
        parsedChunkSize,
        chunkLimits,
      ) &&
        isChunkSeparatorValid(childChunkSeparator)));
  const filesAvailable =
    fileCapabilities.data !== undefined &&
    files.every(
      (file) => capabilityForFile(file, fileCapabilities.data)?.available,
    );
  const configureValid =
    (isExistingUpload || name.trim().length > 0) &&
    !!effectiveEmbeddingModelId &&
    chunkParamsValid &&
    separatorValid &&
    childParamsValid &&
    filesAvailable;

  // Any selected file can be previewed; the picker falls back to the first
  // file when its selection was removed.
  const previewFile =
    files.find((file) => file.name === previewFileName) ?? files[0] ?? null;
  const previewParamsForFile = (file: File): KnowledgePreviewParams | null => {
    const capability = capabilityForFile(file, fileCapabilities.data);
    if (
      !chunkParamsValid ||
      !separatorValid ||
      !childParamsValid ||
      fileCapabilities.data === undefined ||
      capability?.available !== true
    ) {
      return null;
    }
    return {
      chunk_size: parsedChunkSize,
      chunk_overlap: parsedChunkOverlap,
      chunk_separator: chunkSeparator,
      remove_extra_spaces: removeExtraSpaces,
      remove_urls_emails: removeUrlsEmails,
      chunking_mode: chunkingMode,
      unit: fileCapabilities.data.chunk_limits.unit,
      tokenizer_profile_id:
        fileCapabilities.data.chunk_limits.tokenizer_profile_id,
      capability_revision: fileCapabilities.data.capability_revision,
      header_rules: normalizeKnowledgeHeaderRules(
        headerRulesByFile.get(file) ?? [],
      ),
      ...(chunkingMode === "parent_child"
        ? {
            child_chunk_size: parsedChildChunkSize,
            child_chunk_separator: childChunkSeparator,
          }
        : {}),
    };
  };
  // Preview requests are deliberate full-file uploads carrying a full
  // identity (File, parameter snapshot, scope, sequence). Showing a file
  // starts one request; later edits only make that exact response stale
  // until the user explicitly refreshes it.
  const currentPreviewParams =
    previewFile === null ? null : previewParamsForFile(previewFile);
  const previewMatchesFile =
    previewFile !== null && previewState.current?.file === previewFile;
  const previewHasStaleConflict =
    previewFile !== null &&
    uploadFailures.some(
      (failure) =>
        failure.fileName === previewFile.name && failure.stalePreview === true,
    );
  const previewIsStale =
    previewHasStaleConflict ||
    (previewMatchesFile &&
      currentPreviewParams !== null &&
      previewState.current !== null &&
      !previewParamsEqual(previewState.current.params, currentPreviewParams));
  const previewLoading =
    previewMatchesFile && !previewIsStale && previewState.status === "loading";
  const visiblePreviewData = previewMatchesFile ? previewState.data : null;
  const visiblePreviewError =
    previewMatchesFile && !previewIsStale && previewState.status === "error"
      ? previewState.error
      : null;

  const requestCurrentPreview = () => {
    if (previewFile === null || currentPreviewParams === null) return;
    previewAbortControllerRef.current?.abort();
    const controller = new AbortController();
    previewAbortControllerRef.current = controller;
    previewSequenceRef.current += 1;
    const identity = {
      file: previewFile,
      params: currentPreviewParams,
      scopeKey: previewScopeKey,
      sequence: previewSequenceRef.current,
    };
    previewActiveIdentityRef.current = identity;
    dispatchPreview({ type: "requested", identity });
    preview
      .mutateAsync({
        input: {
          file: identity.file,
          processing_profile: previewProcessingParameters(identity.params),
        },
        signal: controller.signal,
      })
      .then(
        (data) => {
          const active = previewActiveIdentityRef.current;
          if (
            active?.file !== identity.file ||
            active.scopeKey !== identity.scopeKey ||
            active.sequence !== identity.sequence
          ) {
            return;
          }
          setSuccessfulPreviews((current) => {
            const next = new Map(current);
            next.set(identity.file, {
              file: identity.file,
              params: identity.params,
              fingerprint: data.preview_fingerprint,
            });
            return next;
          });
          setUploadFailures((current) =>
            current.map((failure) =>
              failure.fileName === identity.file.name &&
              failure.stalePreview === true
                ? { ...failure, stalePreview: false }
                : failure,
            ),
          );
          dispatchPreview({
            type: "resolved",
            scopeKey: identity.scopeKey,
            sequence: identity.sequence,
            data,
          });
        },
        (error: unknown) => {
          if (
            controller.signal.aborted ||
            previewActiveIdentityRef.current?.sequence !== identity.sequence
          ) {
            return;
          }
          dispatchPreview({
            type: "failed",
            scopeKey: identity.scopeKey,
            sequence: identity.sequence,
            error,
          });
        },
      );
  };

  // Scope and capability generations both retire in-flight requests and all
  // fingerprints; returning A → B → A cannot recover the old generation.
  useEffect(() => {
    previewAbortControllerRef.current?.abort();
    previewAbortControllerRef.current = null;
    previewActiveIdentityRef.current = null;
    setSuccessfulPreviews(new Map());
    dispatchPreview({ type: "scope_changed", scopeKey: previewScopeKey });
  }, [previewScopeKey]);

  useEffect(() => {
    const requested = previewState.current;
    if (
      requested !== null &&
      (requested.file !== previewFile ||
        currentPreviewParams === null ||
        !previewParamsEqual(requested.params, currentPreviewParams))
    ) {
      previewAbortControllerRef.current?.abort();
      previewAbortControllerRef.current = null;
      previewActiveIdentityRef.current = null;
    }
  }, [currentPreviewParams, previewFile, previewState]);

  // One automatic preview per newly shown File object (first entry, picker
  // switch, or same-name replacement). The ref both keeps the effect
  // re-entrant under strict mode and leaves invalid-parameter states free to
  // fire once the settings become valid again.
  useEffect(() => {
    if (step !== 2 || previewFile === null) return;
    if (autoPreviewedFileRef.current === previewFile) return;
    if (previewState.current?.file === previewFile) {
      autoPreviewedFileRef.current = previewFile;
      return;
    }
    if (currentPreviewParams === null) return;
    autoPreviewedFileRef.current = previewFile;
    requestCurrentPreview();
  });

  const addFiles = (added: File[]) => {
    if (added.length === 0 || fileCapabilities.error) return;
    // A same-name pick replaces the File object; its old preview (or a still
    // running request) must not survive the replacement.
    for (const file of files) {
      if (added.some((candidate) => candidate.name === file.name)) {
        if (previewState.current?.file === file) {
          previewAbortControllerRef.current?.abort();
          previewAbortControllerRef.current = null;
          previewActiveIdentityRef.current = null;
        }
        dispatchPreview({ type: "file_removed", file });
        setHeaderRulesByFile((current) => {
          const next = new Map(current);
          next.delete(file);
          return next;
        });
        setSuccessfulPreviews((current) => {
          const next = new Map(current);
          next.delete(file);
          return next;
        });
      }
    }
    const next = mergeFiles(files, added);
    setFiles(next);
    if (!isExistingUpload && !nameTouched) {
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
    if (fileCapabilities.error) return;
    addFiles(Array.from(event.dataTransfer.files));
  };

  const startProcessing = async (retryFailedOnly = false) => {
    if (submissionInFlightRef.current) return;
    if (!configureValid || files.length === 0) return;

    const submissionFiles: KnowledgeSubmissionFile[] = [];
    for (const file of files) {
      const params = previewParamsForFile(file);
      if (params === null) return;
      const fingerprint = matchingPreviewFingerprint(
        successfulPreviews.get(file),
        file,
        params,
      );
      submissionFiles.push({
        file,
        processingProfile: previewProcessingParameters(params),
        ...(fingerprint === null
          ? {}
          : { expectedPreviewFingerprint: fingerprint }),
      });
    }
    const snapshot: KnowledgeSubmissionSnapshot = {
      files: submissionFiles,
      name: existingBase?.name ?? name.trim(),
      description: existingBase?.description ?? description.trim(),
      ...(isExistingUpload && files.length === 1 && displayName.trim()
        ? { displayName: displayName.trim() }
        : {}),
      embeddingModelId: effectiveEmbeddingModelId ?? "",
      rerankerModelId: effectiveRerankerModelId,
      retrievalMode: effectiveRetrievalMode,
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
    if (retryFailedOnly && createdBase === null) return;
    const pendingFiles = retryFailedOnly
      ? snapshot.files.filter((item) =>
          uploadFailures.some((failure) => failure.fileName === item.file.name),
        )
      : snapshot.files;
    if (pendingFiles.length === 0) return;

    submissionInFlightRef.current = true;
    setSubmissionInFlightScopeKey(scopeKey);
    const submissionGeneration = submissionGenerationRef.current + 1;
    submissionGenerationRef.current = submissionGeneration;
    const submissionScopeKey = scopeKey;
    const isSubmissionCurrent = () =>
      mountedRef.current &&
      scopeKeyRef.current === submissionScopeKey &&
      submissionGenerationRef.current === submissionGeneration;
    setSubmissionSnapshot(snapshot);
    if (!retryFailedOnly && createdBase === null) {
      setUploadedDocumentIds([]);
    }
    try {
      let base = createdBase ?? existingBase ?? null;
      if (createdBase === null && !retryFailedOnly) {
        try {
          if (existingBase) {
            // A configured base owns its models. An empty base gets exactly
            // one atomic initial configuration before any file is uploaded.
            if (existingBase.embedding_model_id === null) {
              base = (
                await updateBase.mutateAsync({
                  baseId: existingBase.id,
                  input: {
                    embedding_model_id: snapshot.embeddingModelId,
                    retrieval_mode: snapshot.retrievalMode,
                    ...(snapshot.rerankerModelId
                      ? { reranker_model_id: snapshot.rerankerModelId }
                      : {}),
                  },
                })
              ).item;
            }
          } else {
            base = await createBase.mutateAsync({
              name: snapshot.name,
              embedding_model_id: snapshot.embeddingModelId,
              ...(snapshot.rerankerModelId
                ? { reranker_model_id: snapshot.rerankerModelId }
                : {}),
              retrieval_mode: snapshot.retrievalMode,
              description: snapshot.description,
            });
          }
        } catch {
          // Keep the selected files and configuration in step 2 on failure.
          if (isSubmissionCurrent()) setSubmissionSnapshot(null);
          return;
        }
      }
      if (base === null || !isSubmissionCurrent()) return;
      setCreatedBase(base);
      setStep(3);
      setUploadFailures([]);
      setUploadingTotal(pendingFiles.length);

      const failures: UploadFailure[] = [];
      for (const [index, item] of pendingFiles.entries()) {
        const file = item.file;
        if (!isSubmissionCurrent()) return;
        setUploadingIndex(index);
        try {
          const document = await upload.mutateAsync({
            baseId: base.id,
            input: {
              file,
              ...(snapshot.displayName ? { name: snapshot.displayName } : {}),
              processing_profile: item.processingProfile,
              ...(item.expectedPreviewFingerprint === undefined
                ? {}
                : {
                    expected_preview_fingerprint:
                      item.expectedPreviewFingerprint,
                  }),
            },
          });
          if (!isSubmissionCurrent()) return;
          setUploadedDocumentIds((current) =>
            current.includes(document.id) ? current : [...current, document.id],
          );
        } catch (error) {
          if (!isSubmissionCurrent()) return;
          const stalePreview = isKnowledgeConflictError(error);
          if (stalePreview) {
            setSuccessfulPreviews((current) => {
              const next = new Map(current);
              next.delete(file);
              return next;
            });
            setPreviewFileName(file.name);
          }
          failures.push({
            fileName: file.name,
            message: knowledgeErrorMessage(error, labels.errors),
            ...(stalePreview ? { stalePreview: true } : {}),
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
      option.id ===
      (submissionSnapshot?.embeddingModelId ?? effectiveEmbeddingModelId),
  );
  const modelDisplayName = selectedEmbeddingOption
    ? `${selectedEmbeddingOption.provider_name} · ${selectedEmbeddingOption.model_name}`
    : wizard.configuredModelUnavailable;
  const selectedRerankerOption = options.data?.reranker_models.find(
    (option) =>
      option.id ===
      (submissionSnapshot?.rerankerModelId ?? effectiveRerankerModelId),
  );
  const rerankerDisplayName = selectedRerankerOption
    ? `${selectedRerankerOption.provider_name} · ${selectedRerankerOption.model_name}`
    : (submissionSnapshot?.rerankerModelId ?? effectiveRerankerModelId)
      ? wizard.configuredModelUnavailable
      : labels.bases.rerankerNone;
  const availableExtensions =
    fileCapabilities.data?.formats
      .filter((format) => format.available)
      .map((format) => format.extension) ?? [];
  const fileAccept = availableExtensions.join(",");
  const unavailableReason = (file: File) => {
    const capability = capabilityForFile(file, fileCapabilities.data);
    const code = capability?.reason_code;
    if (capability === undefined) return wizard.unsupportedFormatReason;
    if (code === "PARSER_DEPENDENCY_UNAVAILABLE") {
      return wizard.dependencyUnavailableReason;
    }
    if (code === "PARSER_SANDBOX_UNAVAILABLE") {
      return wizard.sandboxUnavailableReason;
    }
    if (code === "TOKENIZER_UNAVAILABLE") {
      return wizard.tokenizerUnavailableReason;
    }
    return wizard.unknownUnavailableReason(code ?? "UNAVAILABLE");
  };

  return (
    <section
      data-testid="knowledge-create-wizard"
      aria-label={
        isExistingUpload ? wizard.uploadExistingTitle : wizard.uploadCreateTitle
      }
      className={cn(
        "flex min-h-0 flex-col text-[13px]",
        step === 2 && "lg:h-[calc(100dvh-3rem)]",
      )}
    >
      <div className="border-border/60 grid shrink-0 gap-3 border-b pb-3 md:grid-cols-[1fr_auto_1fr] md:items-center">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8 w-fit rounded-lg px-1 text-[13px]"
          disabled={isSubmitting}
          onClick={onExit}
          aria-label={labels.common.back}
        >
          <ArrowLeftIcon aria-hidden className="size-4" />
          {existingBase?.name ?? labels.page.title}
        </Button>
        <StepIndicator step={step} labels={wizard.steps} />
        <span aria-hidden className="hidden md:block" />
      </div>

      {step === 1 ? (
        <div className="mx-auto min-h-0 w-full max-w-5xl flex-1 space-y-6 overflow-y-auto py-10 [&>div]:mx-auto [&>div]:max-w-[640px]">
          <div className="space-y-3">
            <h2 className="text-base font-semibold tracking-tight">
              {wizard.sourceSectionTitle}
            </h2>
            <label
              className="border-border/80 bg-muted/25 flex cursor-pointer flex-col items-center gap-2 rounded-lg border border-dashed px-6 py-5 text-center transition-colors focus-within:ring-2 focus-within:ring-blue-500/20 hover:border-blue-400 hover:bg-blue-50/40"
              onDragOver={(event) => event.preventDefault()}
              onDrop={handleDrop}
            >
              <span className="flex items-center gap-2">
                <UploadCloudIcon aria-hidden className="size-5 text-blue-600" />
                <span className="text-[13px] font-medium">
                  {wizard.dropzoneTitle}
                </span>
              </span>
              <span className="text-muted-foreground text-xs leading-5">
                {fileCapabilities.data
                  ? wizard.availableFormats(availableExtensions.join(", "))
                  : wizard.capabilitiesLoading}
              </span>
              <input
                key={fileInputKey}
                type="file"
                multiple
                accept={
                  fileCapabilities.data
                    ? fileAccept
                    : ".actweave-capabilities-loading"
                }
                disabled={fileCapabilities.error !== null}
                aria-label={labels.documents.fileLabel}
                className="sr-only"
                onChange={(event) =>
                  addFiles(Array.from(event.target.files ?? []))
                }
              />
            </label>
            {fileCapabilities.error ? (
              <div className="flex items-center gap-3">
                <p role="alert" className="text-destructive text-xs">
                  {wizard.capabilitiesFailed}
                </p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8 rounded-lg text-xs"
                  onClick={() => void fileCapabilities.refetch()}
                >
                  {wizard.retryFormats}
                </Button>
              </div>
            ) : null}

            {files.length > 0 ? (
              <div className="space-y-2">
                <p
                  role="status"
                  className="text-muted-foreground text-xs leading-5 tabular-nums"
                >
                  {wizard.filesSelected(files.length)}
                </p>
                <ul className="divide-border/60 bg-background border-border/70 divide-y overflow-hidden rounded-lg border">
                  {files.map((file) => {
                    const available =
                      capabilityForFile(file, fileCapabilities.data)
                        ?.available === true;
                    return (
                      <li
                        key={file.name}
                        className="flex min-w-0 items-center gap-3 px-4 py-2.5 text-[13px]"
                      >
                        <KnowledgeFileTypeIcon fileName={file.name} />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate">{file.name}</span>
                          {!available ? (
                            <span className="text-destructive block text-xs">
                              {wizard.fileUnavailable(file.name)} ·{" "}
                              {unavailableReason(file)}
                            </span>
                          ) : null}
                        </span>
                        <span className="text-muted-foreground shrink-0 text-xs">
                          {formatSizeBytes(file.size)}
                        </span>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="size-7 shrink-0 rounded-lg"
                          aria-label={wizard.removeFile(file.name)}
                          onClick={() => {
                            // A removed file's preview (or in-flight response)
                            // must never be shown again.
                            dispatchPreview({ type: "file_removed", file });
                            if (previewState.current?.file === file) {
                              previewAbortControllerRef.current?.abort();
                              previewAbortControllerRef.current = null;
                              previewActiveIdentityRef.current = null;
                            }
                            if (file.name === previewFileName) {
                              setPreviewFileName(null);
                            }
                            setFiles((current) =>
                              current.filter(
                                (candidate) => candidate.name !== file.name,
                              ),
                            );
                            setHeaderRulesByFile((current) => {
                              const next = new Map(current);
                              next.delete(file);
                              return next;
                            });
                            setSuccessfulPreviews((current) => {
                              const next = new Map(current);
                              next.delete(file);
                              return next;
                            });
                          }}
                        >
                          <XIcon aria-hidden className="size-3.5" />
                        </Button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ) : null}
          </div>

          <div className="flex justify-end">
            <Button
              className="h-9 rounded-lg bg-blue-600 text-[13px] text-white shadow-none hover:bg-blue-700"
              type="button"
              disabled={files.length === 0 || !filesAvailable}
              onClick={() => setStep(2)}
            >
              {wizard.next}
            </Button>
          </div>

          {!isExistingUpload && onCreateEmpty ? (
            <div className="border-border/70 border-t pt-4">
              <button
                type="button"
                className="flex items-center gap-1.5 rounded-lg text-[13px] text-blue-600 hover:text-blue-700 focus-visible:ring-2 focus-visible:ring-blue-500/20 focus-visible:outline-none"
                onClick={onCreateEmpty}
              >
                <FolderPlusIcon aria-hidden className="size-4" />
                {wizard.emptyCreateTitle}
              </button>
            </div>
          ) : null}
        </div>
      ) : null}

      {step === 2 ? (
        <div className="grid min-h-0 w-full flex-1 gap-5 py-4 lg:grid-cols-2 lg:overflow-hidden">
          <form
            className="flex min-h-0 flex-col gap-4"
            onSubmit={(event) => {
              event.preventDefault();
              void startProcessing(createdBase !== null);
            }}
          >
            <div className="min-h-0 flex-1 space-y-5 pb-1 lg:overflow-y-auto lg:pr-4">
              <div className="space-y-3">
                <div className="space-y-1">
                  <h2 className="text-[13px] font-semibold">
                    {wizard.chunkSectionTitle}
                  </h2>
                  <p className="text-muted-foreground text-xs leading-5">
                    {labels.documents.chunkImmutableNote}
                  </p>
                  <p className="text-muted-foreground text-xs leading-5">
                    {wizard.knowledgeTokenUnit}
                  </p>
                </div>
                <fieldset className="grid gap-2.5">
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
                    <div
                      key={mode}
                      className={cn(
                        "overflow-hidden rounded-xl border transition-colors",
                        chunkingMode === mode
                          ? "border-blue-600"
                          : "border-border/70",
                      )}
                    >
                      <label className="bg-muted/35 flex min-h-16 cursor-pointer items-start gap-3 p-3">
                        <span
                          aria-hidden
                          className="border-border/50 bg-background flex size-8 shrink-0 items-center justify-center rounded-lg border shadow-xs"
                        >
                          {mode === "general" ? (
                            <LayersIcon className="size-4 text-blue-600" />
                          ) : (
                            <WaypointsIcon className="size-4 text-sky-500" />
                          )}
                        </span>
                        <span className="min-w-0 flex-1 space-y-0.5">
                          <span className="block text-[13px] font-semibold">
                            {label}
                          </span>
                          <span className="text-muted-foreground block text-xs leading-5">
                            {hint}
                          </span>
                        </span>
                        <input
                          type="radio"
                          name="chunking-mode"
                          value={mode}
                          className="mt-1 size-4 shrink-0 accent-blue-600"
                          checked={chunkingMode === mode}
                          disabled={isSubmitting}
                          onChange={() => setChunkingMode(mode)}
                        />
                      </label>
                      {chunkingMode === mode ? (
                        <div className="bg-background space-y-4 p-4">
                          {mode === "parent_child" ? (
                            <h3 className="text-xs font-semibold">
                              {wizard.parentContextTitle}
                            </h3>
                          ) : null}
                          <div className="grid items-start gap-3 sm:grid-cols-2 xl:grid-cols-3">
                            <label className="grid gap-1.5 text-[13px]">
                              <span className="font-medium">
                                {labels.documents.chunkSeparatorLabel}
                              </span>
                              <Input
                                className="bg-muted/60 h-9 rounded-lg border-transparent text-[13px] shadow-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/15 md:text-[13px]"
                                required
                                maxLength={64}
                                disabled={isSubmitting}
                                value={chunkSeparator}
                                onChange={(event) =>
                                  setChunkSeparator(event.target.value)
                                }
                              />
                            </label>
                            <label className="grid gap-1.5 text-[13px]">
                              <span className="font-medium">
                                {wizard.chunkSizeTokenLabel}
                              </span>
                              <Input
                                className="bg-muted/60 h-9 rounded-lg border-transparent text-[13px] shadow-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/15 md:text-[13px]"
                                type="number"
                                min={chunkLimits?.parent_min}
                                max={chunkLimits?.parent_max}
                                required
                                disabled={isSubmitting}
                                value={chunkSize}
                                onChange={(event) =>
                                  setChunkSize(event.target.value)
                                }
                              />
                            </label>
                            <label className="grid gap-1.5 text-[13px]">
                              <span className="font-medium">
                                {wizard.chunkOverlapTokenLabel}
                              </span>
                              <Input
                                className="bg-muted/60 h-9 rounded-lg border-transparent text-[13px] shadow-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/15 md:text-[13px]"
                                type="number"
                                min={KNOWLEDGE_CHUNK_OVERLAP_MIN}
                                max={chunkLimits?.overlap_max}
                                required
                                disabled={isSubmitting}
                                value={chunkOverlap}
                                onChange={(event) =>
                                  setChunkOverlap(event.target.value)
                                }
                              />
                            </label>
                          </div>
                          {chunkingMode === "parent_child" ? (
                            <div className="space-y-2">
                              <h3 className="text-xs font-semibold">
                                {wizard.childRetrievalTitle}
                              </h3>
                              <div className="grid grid-cols-2 items-start gap-3">
                                <label className="grid gap-1.5 text-[13px]">
                                  <span className="font-medium">
                                    {wizard.childChunkSizeTokenLabel}
                                  </span>
                                  <Input
                                    className="bg-muted/60 h-9 rounded-lg border-transparent text-[13px] shadow-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/15 md:text-[13px]"
                                    type="number"
                                    min={chunkLimits?.child_min}
                                    max={chunkLimits?.child_max}
                                    required
                                    disabled={isSubmitting}
                                    value={childChunkSize}
                                    onChange={(event) =>
                                      setChildChunkSize(event.target.value)
                                    }
                                  />
                                </label>
                                <label className="grid gap-1.5 text-[13px]">
                                  <span className="font-medium">
                                    {labels.documents.childChunkSeparatorLabel}
                                  </span>
                                  <Input
                                    className="bg-muted/60 h-9 rounded-lg border-transparent text-[13px] shadow-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/15 md:text-[13px]"
                                    required
                                    maxLength={64}
                                    disabled={isSubmitting}
                                    value={childChunkSeparator}
                                    onChange={(event) =>
                                      setChildChunkSeparator(event.target.value)
                                    }
                                  />
                                </label>
                              </div>
                            </div>
                          ) : null}
                          <fieldset className="border-border/60 grid gap-2 border-t pt-3 text-[13px]">
                            <legend className="bg-background pr-2 text-xs font-semibold">
                              {labels.documents.preprocessingLabel}
                            </legend>
                            <label className="flex items-center gap-2">
                              <input
                                type="checkbox"
                                className="size-4 accent-blue-600"
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
                                className="size-4 accent-blue-600"
                                checked={removeUrlsEmails}
                                disabled={isSubmitting}
                                onChange={(event) =>
                                  setRemoveUrlsEmails(event.target.checked)
                                }
                              />
                              {labels.documents.removeUrlsEmailsLabel}
                            </label>
                          </fieldset>
                        </div>
                      ) : null}
                    </div>
                  ))}
                </fieldset>
              </div>
              {modelsLocked ? (
                <section className="border-border/60 space-y-3 border-t pt-5">
                  <p className="text-muted-foreground text-xs leading-5">
                    {wizard.existingConfigurationHint}
                  </p>
                  <dl className="grid gap-3 text-[13px]">
                    <div className="grid min-w-0 gap-1.5">
                      <dt className="font-medium">{labels.bases.modelLabel}</dt>
                      <dd
                        className="bg-muted/60 truncate rounded-lg px-3 py-2"
                        title={modelDisplayName}
                      >
                        {modelDisplayName}
                      </dd>
                    </div>
                    <div className="grid gap-3 sm:grid-cols-2">
                      <div className="grid min-w-0 gap-1.5">
                        <dt className="font-medium">
                          {labels.bases.retrievalModeLabel}
                        </dt>
                        <dd className="bg-muted/60 rounded-lg px-3 py-2">
                          {
                            labels.bases.retrievalModes[
                              submissionSnapshot?.retrievalMode ??
                                effectiveRetrievalMode
                            ]
                          }
                        </dd>
                      </div>
                      <div className="grid min-w-0 gap-1.5">
                        <dt className="font-medium">
                          {labels.bases.rerankerLabel}
                        </dt>
                        <dd
                          className="bg-muted/60 truncate rounded-lg px-3 py-2"
                          title={rerankerDisplayName}
                        >
                          {rerankerDisplayName}
                        </dd>
                      </div>
                    </div>
                  </dl>
                  {options.error ? (
                    <p role="alert" className="text-destructive text-xs">
                      {labels.bases.modelsLoadFailed}
                    </p>
                  ) : null}
                </section>
              ) : (
                <>
                  <div className="border-border/60 space-y-3 border-t pt-5">
                    <div className="space-y-1">
                      <h2 className="text-[13px] font-semibold">
                        {labels.bases.modelLabel}
                      </h2>
                      <p className="text-muted-foreground text-xs leading-5">
                        {labels.bases.modelHint}
                      </p>
                    </div>
                    {options.isLoading ? (
                      <Skeleton className="h-9 rounded-lg" />
                    ) : options.error ? (
                      <p role="alert" className="text-destructive text-[13px]">
                        {labels.bases.modelsLoadFailed}
                      </p>
                    ) : (options.data?.embedding_models.length ?? 0) === 0 ? (
                      <p className="text-muted-foreground text-[13px]">
                        {labels.bases.noModels}
                      </p>
                    ) : (
                      <Select
                        value={embeddingModelId}
                        disabled={isSubmitting}
                        onValueChange={setEmbeddingModelId}
                      >
                        <SelectTrigger
                          className="bg-muted/60 w-full rounded-lg border-transparent text-[13px] shadow-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/15"
                          aria-label={labels.bases.modelLabel}
                        >
                          <SelectValue
                            placeholder={labels.bases.modelPlaceholder}
                          />
                        </SelectTrigger>
                        <SelectContent className="border-border/70 rounded-lg">
                          {options.data?.embedding_models.map((option) => (
                            <SelectItem
                              className="rounded-md text-[13px]"
                              key={option.id}
                              value={option.id}
                            >
                              {option.provider_name} · {option.model_name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  </div>

                  <KnowledgeRetrievalModeField
                    variant="cards"
                    value={retrievalMode}
                    onChange={setRetrievalMode}
                    disabled={isSubmitting}
                    selectedContent={
                      <div className="space-y-2">
                        <h3 className="text-[13px] font-medium">
                          {labels.bases.rerankerLabel}
                        </h3>
                        {options.isLoading ? (
                          <Skeleton className="h-9 rounded-lg" />
                        ) : options.error ? (
                          <p
                            role="alert"
                            className="text-destructive text-[13px]"
                          >
                            {labels.bases.modelsLoadFailed}
                          </p>
                        ) : (
                          <>
                            {options.data?.reranker_models.length === 0 ? (
                              <p className="text-muted-foreground text-xs leading-5">
                                {labels.bases.rerankerUnavailable}
                              </p>
                            ) : null}
                            <Select
                              value={rerankerModelId || "none"}
                              disabled={isSubmitting}
                              onValueChange={(value) =>
                                setRerankerModelId(
                                  value === "none" ? "" : value,
                                )
                              }
                            >
                              <SelectTrigger
                                className="bg-muted/60 w-full min-w-0 rounded-lg border-transparent text-[13px] shadow-none focus-visible:border-blue-500 focus-visible:ring-blue-500/15"
                                aria-label={labels.bases.rerankerLabel}
                              >
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent className="rounded-lg">
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
                          </>
                        )}
                      </div>
                    }
                  />
                </>
              )}
              {!isExistingUpload ? (
                <div className="border-border/60 space-y-3 border-t pt-5">
                  <h2 className="text-[13px] font-semibold">
                    {wizard.infoSectionTitle}
                  </h2>
                  <label className="grid gap-1.5 text-[13px]">
                    <span className="font-medium">
                      {labels.bases.nameLabel}
                    </span>
                    <Input
                      className="bg-muted/60 h-9 rounded-lg border-transparent text-[13px] shadow-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/15 md:text-[13px]"
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
                  <label className="grid gap-1.5 text-[13px]">
                    <span className="font-medium">
                      {labels.bases.descriptionLabel}
                    </span>
                    <Textarea
                      className="bg-muted/60 w-full rounded-lg border-transparent text-[13px] leading-5 shadow-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/15 md:text-[13px]"
                      value={description}
                      rows={2}
                      disabled={isSubmitting}
                      placeholder={labels.bases.descriptionPlaceholder}
                      onChange={(event) => setDescription(event.target.value)}
                    />
                  </label>
                </div>
              ) : files.length === 1 ? (
                <label className="border-border/60 grid gap-1.5 border-t pt-5 text-[13px]">
                  <span className="font-medium">
                    {labels.documents.displayNameLabel}
                  </span>
                  <Input
                    className="bg-muted/60 h-9 rounded-lg border-transparent text-[13px] shadow-none focus-visible:border-blue-500 focus-visible:ring-2 focus-visible:ring-blue-500/15 md:text-[13px]"
                    value={displayName}
                    maxLength={200}
                    disabled={isSubmitting}
                    placeholder={labels.documents.displayNamePlaceholder}
                    onChange={(event) => setDisplayName(event.target.value)}
                  />
                </label>
              ) : null}

              {initializationError ? (
                <p role="alert" className="text-destructive text-[13px]">
                  {knowledgeErrorMessage(initializationError, labels.errors)}
                </p>
              ) : null}
            </div>
            <div className="border-border/70 bg-background flex shrink-0 items-center justify-between gap-3 border-t pt-3">
              <Button
                className="h-9 rounded-lg text-[13px] shadow-none"
                type="button"
                variant="outline"
                disabled={isSubmitting}
                onClick={() => setStep(1)}
              >
                {wizard.previous}
              </Button>
              <Button
                className="h-9 rounded-lg bg-blue-600 text-[13px] text-white shadow-none hover:bg-blue-700"
                type="submit"
                disabled={isSubmitting || !configureValid}
              >
                {createBase.isPending
                  ? labels.common.creating
                  : updateBase.isPending
                    ? labels.common.saving
                    : isExistingUpload
                      ? wizard.uploadAction
                      : wizard.saveAndProcess}
              </Button>
            </div>
          </form>
          <aside
            aria-label={wizard.previewTitle}
            className="border-border/70 bg-background flex min-h-80 min-w-0 flex-col overflow-hidden rounded-xl border lg:min-h-0"
            data-testid="chunk-preview-panel"
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
                  disabled={
                    isSubmitting ||
                    previewFile === null ||
                    currentPreviewParams === null ||
                    previewLoading
                  }
                  onClick={requestCurrentPreview}
                >
                  <RefreshCwIcon
                    aria-hidden
                    className={cn("size-3.5", previewLoading && "animate-spin")}
                  />
                  {previewLoading
                    ? wizard.previewLoading
                    : wizard.previewRefresh}
                </Button>{" "}
              </div>
              <div className="flex min-w-0 items-center gap-2">
                {previewFile ? (
                  <KnowledgeFileTypeIcon fileName={previewFile.name} />
                ) : null}
                {files.length > 1 ? (
                  <Select
                    value={previewFile?.name ?? ""}
                    disabled={isSubmitting}
                    onValueChange={setPreviewFileName}
                  >
                    <SelectTrigger
                      size="sm"
                      aria-label={wizard.previewPickFile}
                      className="min-w-0 flex-1 rounded-lg border-0 bg-transparent px-1 text-[13px] font-medium shadow-none focus-visible:ring-blue-500/20"
                    >
                      <SelectValue placeholder={wizard.previewPickFile}>
                        {previewFile
                          ? wizard.previewHint(previewFile.name)
                          : undefined}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent className="border-border/70 rounded-lg">
                      {files.map((file) => (
                        <SelectItem
                          className="rounded-md text-[13px]"
                          key={file.name}
                          value={file.name}
                        >
                          {file.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : previewFile ? (
                  <p
                    className="min-w-0 flex-1 truncate text-[13px] font-medium"
                    title={previewFile.name}
                  >
                    {wizard.previewHint(previewFile.name)}
                  </p>
                ) : null}
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
            {visiblePreviewData && previewFile ? (
              <KnowledgeHeaderSettings
                sources={visiblePreviewData.table_sources}
                rules={headerRulesByFile.get(previewFile) ?? []}
                disabled={isSubmitting || previewLoading}
                onChange={(rule) => {
                  setHeaderRulesByFile((current) => {
                    const next = new Map(current);
                    const rules = [
                      ...(next.get(previewFile) ?? []).filter(
                        (item) => item.sheet !== rule.sheet,
                      ),
                      rule,
                    ];
                    next.set(previewFile, normalizeKnowledgeHeaderRules(rules));
                    return next;
                  });
                }}
              />
            ) : null}
            <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
              {previewLoading ? (
                <p
                  role="status"
                  className="text-muted-foreground text-xs leading-5"
                >
                  {wizard.previewLoading}
                </p>
              ) : currentPreviewParams === null ? (
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
              {visiblePreviewData ? (
                <PreviewChunkList
                  data={visiblePreviewData}
                  scopeKey={previewScopeKey}
                  stale={previewIsStale}
                  labels={wizard}
                />
              ) : null}
            </div>
          </aside>
        </div>
      ) : null}

      {step === 3 && createdBase && submissionSnapshot ? (
        <div className="mx-auto grid min-h-0 w-full max-w-6xl flex-1 gap-8 overflow-y-auto py-7 lg:grid-cols-[minmax(0,1fr)_18rem]">
          <div className="space-y-5">
            <div className="space-y-1">
              <h2 className="flex items-center gap-2 text-base font-semibold tracking-tight">
                {isExistingUpload ? (
                  <UploadCloudIcon
                    aria-hidden
                    className="size-5 text-blue-600"
                  />
                ) : (
                  <CheckIcon aria-hidden className="size-5 text-emerald-600" />
                )}
                {isExistingUpload
                  ? wizard.uploadProcessingTitle
                  : wizard.createdTitle}
              </h2>
              <p className="text-muted-foreground text-[13px]">
                {wizard.createdHint}
              </p>
            </div>

            <div className="flex items-center gap-3">
              <span className="flex size-11 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-blue-600 dark:bg-blue-950/30">
                <BookOpenIcon aria-hidden className="size-5" />
              </span>
              <div className="min-w-0 flex-1 space-y-1">
                <p className="text-xs font-medium">{labels.bases.nameLabel}</p>
                <p
                  className="bg-muted/60 truncate rounded-lg px-3 py-2 text-[13px]"
                  title={createdBase.name}
                >
                  {createdBase.name}
                </p>
              </div>
            </div>
            <div className="border-border/60 space-y-3 border-t pt-4">
              <h3 className="text-[13px] font-semibold">
                {wizard.processingTitle}
              </h3>
              {uploadingIndex !== null ? (
                <p
                  role="status"
                  className="text-muted-foreground text-xs leading-5"
                >
                  {labels.documents.uploadingProgress(
                    uploadingIndex + 1,
                    uploadingTotal,
                  )}
                </p>
              ) : null}
              <ul
                className="divide-border/60 bg-background border-border/70 divide-y overflow-hidden rounded-lg border"
                data-testid="wizard-document-status"
              >
                {(documents.data?.items ?? [])
                  .filter((document) =>
                    uploadedDocumentIds.includes(document.id),
                  )
                  .map((document) => (
                    <li
                      key={document.id}
                      className="flex min-w-0 items-center gap-3 px-4 py-2.5 text-[13px]"
                    >
                      <KnowledgeFileTypeIcon
                        fileName={document.original_name}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate">{document.name}</span>
                        <span className="text-muted-foreground block text-xs">
                          {wizard.actualProfile(
                            document.parsing_profile?.chunk.size ??
                              document.chunk_size,
                            document.chunk_size_unit === "token"
                              ? wizard.knowledgeTokenShort
                              : wizard.characterUnit,
                          )}
                        </span>
                      </span>
                      <Badge
                        variant={documentStatusVariant(document.status)}
                        className={documentStatusClassName(document.status)}
                      >
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
                  <Button
                    type="button"
                    variant="outline"
                    className="mt-2 h-8 rounded-lg text-[13px] shadow-none"
                    disabled={isSubmitting}
                    onClick={() => {
                      if (
                        uploadFailures.some(
                          (failure) => failure.stalePreview === true,
                        )
                      ) {
                        setStep(2);
                      } else {
                        void startProcessing(true);
                      }
                    }}
                  >
                    {uploadFailures.some(
                      (failure) => failure.stalePreview === true,
                    )
                      ? wizard.refreshStalePreview
                      : wizard.retryFailedUploads}
                  </Button>
                </div>
              ) : null}
            </div>

            <div className="space-y-3">
              <h3 className="text-[13px] font-semibold">
                {wizard.summaryTitle}
              </h3>
              <dl className="grid gap-x-4 gap-y-2 text-[13px]">
                <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] items-start gap-4 py-0.5 sm:grid-cols-[180px_minmax(0,1fr)]">
                  <dt className="text-muted-foreground">
                    {labels.bases.nameLabel}
                  </dt>
                  <dd className="min-w-0 truncate font-medium">
                    {createdBase.name}
                  </dd>
                </div>
                <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] items-start gap-4 py-0.5 sm:grid-cols-[180px_minmax(0,1fr)]">
                  <dt className="text-muted-foreground">
                    {labels.documents.chunkingModeLabel}
                  </dt>
                  <dd>
                    {submissionSnapshot.chunkingMode === "parent_child"
                      ? labels.documents.chunkingModeParentChild
                      : labels.documents.chunkingModeGeneral}
                  </dd>
                </div>
                <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] items-start gap-4 py-0.5 sm:grid-cols-[180px_minmax(0,1fr)]">
                  <dt className="text-muted-foreground">
                    {wizard.chunkSizeTokenLabel}
                  </dt>
                  <dd className="tabular-nums">
                    {submissionSnapshot.chunkSize}
                  </dd>
                </div>
                <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] items-start gap-4 py-0.5 sm:grid-cols-[180px_minmax(0,1fr)]">
                  <dt className="text-muted-foreground">
                    {wizard.chunkOverlapTokenLabel}
                  </dt>
                  <dd className="tabular-nums">
                    {submissionSnapshot.chunkOverlap}
                  </dd>
                </div>
                <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] items-start gap-4 py-0.5 sm:grid-cols-[180px_minmax(0,1fr)]">
                  <dt className="text-muted-foreground">
                    {labels.documents.chunkSeparatorLabel}
                  </dt>
                  <dd className="font-mono text-xs">
                    {submissionSnapshot.chunkSeparator}
                  </dd>
                </div>
                {submissionSnapshot.chunkingMode === "parent_child" ? (
                  <>
                    <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] items-start gap-4 py-0.5 sm:grid-cols-[180px_minmax(0,1fr)]">
                      <dt className="text-muted-foreground">
                        {wizard.childChunkSizeTokenLabel}
                      </dt>
                      <dd className="tabular-nums">
                        {submissionSnapshot.childChunkSize}
                      </dd>
                    </div>
                    <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] items-start gap-4 py-0.5 sm:grid-cols-[180px_minmax(0,1fr)]">
                      <dt className="text-muted-foreground">
                        {labels.documents.childChunkSeparatorLabel}
                      </dt>
                      <dd className="font-mono text-xs">
                        {submissionSnapshot.childChunkSeparator}
                      </dd>
                    </div>
                  </>
                ) : null}
                <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] items-start gap-4 py-0.5 sm:grid-cols-[180px_minmax(0,1fr)]">
                  <dt className="text-muted-foreground">
                    {labels.bases.modelLabel}
                  </dt>
                  <dd className="min-w-0 truncate">{modelDisplayName}</dd>
                </div>
                <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] items-start gap-4 py-0.5 sm:grid-cols-[180px_minmax(0,1fr)]">
                  <dt className="text-muted-foreground">
                    {labels.bases.retrievalModeLabel}
                  </dt>
                  <dd>
                    {
                      labels.bases.retrievalModes[
                        submissionSnapshot.retrievalMode
                      ]
                    }
                  </dd>
                </div>
                <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] items-start gap-4 py-0.5 sm:grid-cols-[180px_minmax(0,1fr)]">
                  <dt className="text-muted-foreground">
                    {labels.bases.rerankerLabel}
                  </dt>
                  <dd className="min-w-0 truncate" title={rerankerDisplayName}>
                    {rerankerDisplayName}
                  </dd>
                </div>
              </dl>
            </div>

            <div className="flex justify-start pt-1">
              <Button
                className="h-9 rounded-lg bg-blue-600 text-[13px] text-white shadow-none hover:bg-blue-700"
                type="button"
                disabled={isSubmitting}
                onClick={() => onFinished(createdBase)}
              >
                {wizard.goToDocuments}
                <ArrowRightIcon aria-hidden className="size-4" />
              </Button>
            </div>
          </div>
          <aside className="bg-muted/35 h-fit space-y-3 rounded-xl p-5 lg:mt-12">
            <BookOpenIcon aria-hidden className="size-5 text-blue-600" />
            <h3 className="text-[13px] font-semibold">
              {wizard.nextStepsTitle}
            </h3>
            <p className="text-muted-foreground text-xs leading-5">
              {wizard.nextStepsHint}
            </p>
          </aside>
        </div>
      ) : null}
    </section>
  );
}
