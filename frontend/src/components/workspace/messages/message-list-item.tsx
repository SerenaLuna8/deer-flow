import type { Message } from "@langchain/langgraph-sdk";
import {
  CheckIcon,
  FileIcon,
  Loader2Icon,
  PencilIcon,
  XIcon,
} from "lucide-react";
import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  type ImgHTMLAttributes,
} from "react";

import { Loader } from "@/components/ai-elements/loader";
import {
  Message as AIElementMessage,
  MessageContent as AIElementMessageContent,
  MessageToolbar,
} from "@/components/ai-elements/message";
import { Task, TaskTrigger } from "@/components/ai-elements/task";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { extractCitationSources } from "@/core/citations/sources";
import { useI18n } from "@/core/i18n/hooks";
import {
  extractContentFromMessage,
  getReasoningDurationSeconds,
  extractReasoningContentFromMessage,
  reasoningPresentationKind,
  getMessageCopyData,
  parseUploadedFiles,
  stripUploadedFilesTag,
  type FileInMessage,
} from "@/core/messages/utils";
import { useProjectArtifactReferenceURL } from "@/core/private-work/file-hooks";
import { useRehypeSplitWordsIntoSpans } from "@/core/rehype";
import { useProjectSlashSkills } from "@/core/shared-assets";
import { readReferenceMessageContexts } from "@/core/sidecar";
import {
  parseSlashSkillReference,
  resolveSlashSkillDisplay,
} from "@/core/skills";
import { SafeReasoningContent } from "@/core/streamdown/components";
import { readKnowledgeCitations } from "@/core/threads/message-projection";
import { cn } from "@/lib/utils";

import { CitationSourcesPanel } from "../citations/citation-sources-panel";
import { KnowledgeCitationsPanel } from "../citations/knowledge-citations-panel";
import { CopyButton } from "../copy-button";
import { ReferenceAttachmentSummary } from "../sidecar/reference-attachments";
import { SlashSkillChip } from "../slash-skill-chip";
import { Tooltip } from "../tooltip";

import { MarkdownContent } from "./markdown-content";
import { createMarkdownLinkComponent } from "./markdown-link";
import { ThinkingDisclosure } from "./thinking-disclosure";

export function MessageListItem({
  className,
  message,
  isLoading,
  threadId,
  showCopyButton = true,
  showReasoning = true,
  canEdit = false,
  isEditPending = false,
  editSession,
  onEditAndRegenerate,
  onEditStart,
}: {
  className?: string;
  message: Message;
  isLoading?: boolean;
  threadId: string;
  showCopyButton?: boolean;
  showReasoning?: boolean;
  canEdit?: boolean;
  isEditPending?: boolean;
  editSession?: {
    draft: string;
    onCancel: () => void;
    onDraftChange: (value: string) => void;
  };
  onEditAndRegenerate?: (replacementText: string) => Promise<boolean>;
  onEditStart?: (initialDraft: string) => void;
}) {
  const { t } = useI18n();
  const isHuman = message.type === "human";
  const editableText = useMemo(
    () => (isHuman ? (getMessageCopyData(message) ?? "") : ""),
    [isHuman, message],
  );
  const isEditing = editSession !== undefined;
  const draft = editSession?.draft ?? "";
  const trimmedDraft = draft.trim();
  const editSubmitDisabled =
    !canEdit ||
    isEditPending ||
    trimmedDraft.length === 0 ||
    trimmedDraft === editableText.trim();

  const startEditing = useCallback(() => {
    if (!canEdit || !onEditStart) {
      return;
    }
    onEditStart(editableText);
  }, [canEdit, editableText, onEditStart]);
  const cancelEditing = useCallback(() => {
    editSession?.onCancel();
  }, [editSession]);
  useEffect(() => {
    if (isEditing && !canEdit && !isEditPending) {
      cancelEditing();
    }
  }, [canEdit, cancelEditing, isEditPending, isEditing]);
  const submitEdit = useCallback(async () => {
    if (editSubmitDisabled || !onEditAndRegenerate) {
      return;
    }
    try {
      await onEditAndRegenerate(trimmedDraft);
    } catch {
      // The scoped replay owner reports the concrete error. Keep this draft
      // open so an unexpected callback failure cannot lose the user's edit.
    }
  }, [editSubmitDisabled, onEditAndRegenerate, trimmedDraft]);

  return (
    <AIElementMessage
      className={cn("group/conversation-message relative w-full", className)}
      from={isHuman ? "user" : "assistant"}
    >
      <MessageContent
        className={isHuman ? "w-fit max-w-[88%] sm:max-w-[75%]" : "w-full"}
        message={message}
        isLoading={isLoading}
        threadId={threadId}
        showReasoning={showReasoning}
        editState={
          isHuman && isEditing
            ? {
                draft,
                disabled: !canEdit || isEditPending,
                submitDisabled: editSubmitDisabled,
                onCancel: cancelEditing,
                onDraftChange: editSession.onDraftChange,
                onSubmit: submitEdit,
              }
            : undefined
        }
      />
      {!isLoading && showCopyButton && (
        <MessageToolbar
          className={cn(
            isHuman
              ? "absolute right-0 -bottom-9 left-0 justify-end"
              : "absolute right-0 bottom-0 left-0",
            "z-20 opacity-0 transition-opacity delay-200 duration-300 group-hover/conversation-message:opacity-100",
          )}
        >
          <div className="pointer-events-auto flex gap-1">
            <CopyButton clipboardData={getMessageCopyData(message)} />
            {canEdit &&
              isHuman &&
              onEditAndRegenerate &&
              onEditStart &&
              !isEditing && (
                <Tooltip content={t.common.editAndRerun}>
                  <Button
                    aria-label={t.common.editAndRerun}
                    size="icon-sm"
                    type="button"
                    variant="ghost"
                    disabled={isEditPending}
                    onClick={startEditing}
                  >
                    <PencilIcon className="size-3" />
                  </Button>
                </Tooltip>
              )}
          </div>
        </MessageToolbar>
      )}
    </AIElementMessage>
  );
}

/**
 * Custom image component that handles artifact URLs
 */
function MessageImage({
  src,
  alt,
  threadId,
  maxWidth = "90%",
  ...props
}: React.ImgHTMLAttributes<HTMLImageElement> & {
  threadId: string;
  maxWidth?: string;
}) {
  const projectURL = useProjectArtifactReferenceURL(
    threadId,
    typeof src === "string" ? src : "",
  );
  if (!src) return null;

  const imageProps = {
    ...props,
    className: cn("max-w-full overflow-hidden rounded-lg", props.className),
    style: { ...props.style, maxWidth },
  };

  if (typeof src !== "string") {
    return <img src={src} alt={alt} {...imageProps} />;
  }

  const url = src.startsWith("/mnt/") ? projectURL : src;
  if (!url) return null;

  return (
    <a href={url} target="_blank" rel="noopener noreferrer">
      <img src={url} alt={alt} {...imageProps} />
    </a>
  );
}

function HumanMessageText({ content }: { content: string }) {
  // `parseSlashSkillReference` is a pure regex gate (no data subscription), so
  // the overwhelmingly common plain-text human message never subscribes to the
  // skills query. Only a message that literally looks like a `/skill …`
  // activation mounts `HumanSlashSkillText`, which owns the `useSkills()`
  // lookup. This keeps a skill-enabled toggle from re-rendering every human
  // turn — only the few slash-candidate turns react to catalog changes.
  const reference = useMemo(() => parseSlashSkillReference(content), [content]);

  if (!reference) {
    return <div className="break-words whitespace-pre-wrap">{content}</div>;
  }

  return <HumanSlashSkillText content={content} />;
}

function HumanSlashSkillText({ content }: { content: string }) {
  const { skills } = useProjectSlashSkills();
  const slashSkill = resolveSlashSkillDisplay(content, skills);

  if (!slashSkill) {
    return <div className="break-words whitespace-pre-wrap">{content}</div>;
  }

  return (
    <div className="flex max-w-full min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
      <SlashSkillChip name={slashSkill.name} />
      {slashSkill.remainingText && (
        <span className="min-w-0 flex-1 break-words whitespace-pre-wrap">
          {slashSkill.remainingText}
        </span>
      )}
    </div>
  );
}

function MessageContent_({
  className,
  message,
  isLoading = false,
  threadId,
  showReasoning,
  editState,
}: {
  className?: string;
  message: Message;
  isLoading?: boolean;
  threadId: string;
  showReasoning: boolean;
  editState?: {
    draft: string;
    disabled: boolean;
    submitDisabled: boolean;
    onCancel: () => void;
    onDraftChange: (value: string) => void;
    onSubmit: () => void | Promise<void>;
  };
}) {
  const { t } = useI18n();
  const rehypePlugins = useRehypeSplitWordsIntoSpans(isLoading);
  const isHuman = message.type === "human";
  const components = useMemo(
    () => ({
      img: (props: ImgHTMLAttributes<HTMLImageElement>) => (
        <MessageImage {...props} threadId={threadId} maxWidth="90%" />
      ),
      a: createMarkdownLinkComponent(threadId),
    }),
    [threadId],
  );

  const rawContent = extractContentFromMessage(message);
  const reasoningContent = extractReasoningContentFromMessage(message);
  const reasoningKind = reasoningPresentationKind(message) ?? "full";
  const reasoningDuration = getReasoningDurationSeconds(message);

  const files = useMemo(() => {
    const files = message.additional_kwargs?.files;
    if (!Array.isArray(files) || files.length === 0) {
      const parsedFiles = parseUploadedFiles(rawContent);
      return parsedFiles.length > 0 ? parsedFiles : null;
    }
    return files as FileInMessage[];
  }, [message.additional_kwargs?.files, rawContent]);
  const referenceAttachments = useMemo(
    () =>
      readReferenceMessageContexts(message.additional_kwargs).map(
        (context, index) => ({
          id: index,
          context,
        }),
      ),
    [message.additional_kwargs],
  );

  const contentToDisplay = useMemo(() => {
    if (isHuman) {
      return rawContent ? stripUploadedFilesTag(rawContent) : "";
    }
    return rawContent ?? "";
  }, [rawContent, isHuman]);
  const citationSources = useMemo(
    () => (isHuman ? [] : extractCitationSources(contentToDisplay)),
    [contentToDisplay, isHuman],
  );
  const knowledgeCitations = useMemo(
    () => readKnowledgeCitations(message),
    [message],
  );

  const filesList =
    files && files.length > 0 ? (
      <RichFilesList files={files} threadId={threadId} />
    ) : null;

  // Uploading state: mock AI message shown while files upload
  if (message.additional_kwargs?.element === "task") {
    return (
      <AIElementMessageContent className={className}>
        <Task defaultOpen={false}>
          <TaskTrigger title="">
            <div className="text-muted-foreground flex w-full cursor-default items-center gap-2 text-sm select-none">
              <Loader className="size-4" />
              <span>{contentToDisplay}</span>
            </div>
          </TaskTrigger>
        </Task>
      </AIElementMessageContent>
    );
  }

  // Reasoning-only AI message (no main response content yet)
  if (!isHuman && showReasoning && reasoningContent && !rawContent) {
    return (
      <AIElementMessageContent className={className}>
        <ThinkingDisclosure
          duration={reasoningDuration}
          isStreaming={isLoading}
          kind={reasoningKind}
        >
          <SafeReasoningContent className="border-border/70 text-foreground/75 mt-2 ml-1.5 border-l py-1 pr-0 pl-5 leading-6">
            {reasoningContent}
          </SafeReasoningContent>
        </ThinkingDisclosure>
      </AIElementMessageContent>
    );
  }

  if (isHuman) {
    // Composer input is plain text, not authored Markdown. Parsing it as
    // Markdown mangles pasted code/logs (indented lines become code blocks,
    // "$...$" spans become math) and lets pathological input crash the page
    // through marked's recursive blockquote lexer, so render it verbatim.
    return (
      <div
        data-message-content-role="user"
        className={cn(
          "ml-auto flex max-w-full min-w-0 flex-col gap-2",
          className,
        )}
      >
        {referenceAttachments.length > 0 && (
          <ReferenceAttachmentSummary
            className="self-end shadow-none"
            references={referenceAttachments}
            testId="message-reference-attachment"
          />
        )}
        {filesList}
        {editState ? (
          <div className="bg-background border-border flex w-full min-w-0 flex-col gap-2 rounded-lg border p-2 shadow-sm">
            <Textarea
              autoFocus
              className="min-h-24 resize-y"
              data-testid="message-edit-textarea"
              disabled={editState.disabled}
              value={editState.draft}
              onChange={(event) =>
                editState.onDraftChange(event.currentTarget.value)
              }
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  editState.onCancel();
                }
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                  event.preventDefault();
                  void editState.onSubmit();
                }
              }}
            />
            <div className="text-muted-foreground text-xs">
              {t.common.editRerunWarning}
            </div>
            <div className="flex justify-end gap-1">
              <Button
                size="sm"
                type="button"
                variant="ghost"
                disabled={editState.disabled}
                onClick={editState.onCancel}
              >
                <XIcon className="size-3" />
                {t.common.cancel}
              </Button>
              <Button
                size="sm"
                type="button"
                disabled={editState.submitDisabled}
                onClick={() => void editState.onSubmit()}
              >
                <CheckIcon className="size-3" />
                {t.common.updateAndRerun}
              </Button>
            </div>
          </div>
        ) : contentToDisplay ? (
          <AIElementMessageContent className="w-full max-w-full">
            <HumanMessageText content={contentToDisplay} />
          </AIElementMessageContent>
        ) : null}
      </div>
    );
  }

  return (
    <AIElementMessageContent
      className={className}
      data-message-content-role="assistant"
    >
      {filesList}
      {showReasoning && reasoningContent && (
        <ThinkingDisclosure
          className="mb-3"
          duration={reasoningDuration}
          isStreaming={isLoading}
          kind={reasoningKind}
        >
          <SafeReasoningContent className="border-border/70 text-foreground/75 mt-2 ml-1.5 border-l py-1 pr-0 pl-5 leading-6">
            {reasoningContent}
          </SafeReasoningContent>
        </ThinkingDisclosure>
      )}
      <MarkdownContent
        content={contentToDisplay}
        isLoading={isLoading}
        rehypePlugins={rehypePlugins}
        className="my-3"
        components={components}
      />
      <CitationSourcesPanel sources={citationSources} />
      <KnowledgeCitationsPanel citations={knowledgeCitations} />
    </AIElementMessageContent>
  );
}

/**
 * Get file extension and check helpers
 */
const getFileExt = (filename: string) =>
  filename.split(".").pop()?.toLowerCase() ?? "";

const FILE_TYPE_MAP: Record<string, string> = {
  json: "JSON",
  csv: "CSV",
  txt: "TXT",
  md: "Markdown",
  py: "Python",
  js: "JavaScript",
  ts: "TypeScript",
  tsx: "TSX",
  jsx: "JSX",
  html: "HTML",
  css: "CSS",
  xml: "XML",
  yaml: "YAML",
  yml: "YAML",
  pdf: "PDF",
  png: "PNG",
  jpg: "JPG",
  jpeg: "JPEG",
  gif: "GIF",
  svg: "SVG",
  zip: "ZIP",
  tar: "TAR",
  gz: "GZ",
};

const IMAGE_EXTENSIONS = ["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"];

function getFileTypeLabel(filename: string): string {
  const ext = getFileExt(filename);
  return FILE_TYPE_MAP[ext] ?? (ext.toUpperCase() || "FILE");
}

function isImageFile(filename: string): boolean {
  return IMAGE_EXTENSIONS.includes(getFileExt(filename));
}

/**
 * Format bytes to human-readable size string
 */
function formatBytes(bytes: number): string {
  if (bytes === 0) return "—";
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

/**
 * List of files from additional_kwargs.files (with optional upload status)
 */
function RichFilesList({
  files,
  threadId,
}: {
  files: FileInMessage[];
  threadId: string;
}) {
  if (files.length === 0) return null;
  return (
    <div className="mb-2 flex flex-wrap justify-end gap-2">
      {files.map((file, index) => (
        <RichFileCard
          key={`${file.filename}-${index}`}
          file={file}
          threadId={threadId}
        />
      ))}
    </div>
  );
}

/**
 * Single file card that handles FileInMessage (supports uploading state)
 */
function RichFileCard({
  file,
  threadId,
}: {
  file: FileInMessage;
  threadId: string;
}) {
  const { t } = useI18n();
  const isUploading = file.status === "uploading";
  const isImage = isImageFile(file.filename);
  const fileUrl = useProjectArtifactReferenceURL(threadId, file.path ?? "");

  if (isUploading) {
    return (
      <div className="bg-background border-border/40 flex max-w-50 min-w-30 flex-col gap-1 rounded-lg border p-3 opacity-60 shadow-sm">
        <div className="flex items-start gap-2">
          <Loader2Icon className="text-muted-foreground mt-0.5 size-4 shrink-0 animate-spin" />
          <span
            className="text-foreground truncate text-sm font-medium"
            title={file.filename}
          >
            {file.filename}
          </span>
        </div>
        <div className="flex items-center justify-between gap-2">
          <Badge
            variant="secondary"
            className="rounded px-1.5 py-0.5 text-[10px] font-normal"
          >
            {getFileTypeLabel(file.filename)}
          </Badge>
          <span className="text-muted-foreground text-[10px]">
            {t.uploads.uploading}
          </span>
        </div>
      </div>
    );
  }

  if (!file.path) return null;

  if (!fileUrl) return null;

  if (isImage) {
    return (
      <a
        href={fileUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="group border-border/40 relative block overflow-hidden rounded-lg border"
      >
        <img
          src={fileUrl}
          alt={file.filename}
          className="h-32 w-auto max-w-60 object-cover transition-transform group-hover:scale-105"
        />
      </a>
    );
  }

  return (
    <div className="bg-background border-border/40 flex max-w-50 min-w-30 flex-col gap-1 rounded-lg border p-3 shadow-sm">
      <div className="flex items-start gap-2">
        <FileIcon className="text-muted-foreground mt-0.5 size-4 shrink-0" />
        <span
          className="text-foreground truncate text-sm font-medium"
          title={file.filename}
        >
          {file.filename}
        </span>
      </div>
      <div className="flex items-center justify-between gap-2">
        <Badge
          variant="secondary"
          className="rounded px-1.5 py-0.5 text-[10px] font-normal"
        >
          {getFileTypeLabel(file.filename)}
        </Badge>
        <span className="text-muted-foreground text-[10px]">
          {formatBytes(file.size)}
        </span>
      </div>
    </div>
  );
}

const MessageContent = memo(MessageContent_);
