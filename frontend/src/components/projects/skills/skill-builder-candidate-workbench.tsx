"use client";

import {
  CheckCircle2Icon,
  Code2Icon,
  EyeIcon,
  FileWarningIcon,
  FilesIcon,
  KeyRoundIcon,
  Loader2Icon,
  SaveIcon,
  ShieldCheckIcon,
  XIcon,
} from "lucide-react";
import { useEffect, useId, useMemo, useState, type KeyboardEvent } from "react";

import { SkillFileTree } from "@/components/projects/assets/skill-file-tree";
import {
  buildSkillFileTree,
  markdownPreviewContent,
  type SkillFileTreeSelection,
  type WorkingSkillFile,
} from "@/components/projects/assets/skill-file-workbench-state";
import { SkillSecretDeclarationsEditor } from "@/components/projects/assets/skill-secret-declarations-editor";
import { skillWorkbenchTabVariant } from "@/components/projects/assets/skill-workbench-tabs";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/core/i18n/hooks";
import type {
  SkillBuilderBaseFile,
  SkillBuilderFile,
  SkillBuilderSessionKind,
  SkillBuilderValidation,
} from "@/core/skill-builder";
import { SafeStreamdown } from "@/core/streamdown/components";

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  const kibibytes = value / 1024;
  if (kibibytes < 1024) return `${Number(kibibytes.toFixed(1))} KB`;
  return `${Number((kibibytes / 1024).toFixed(1))} MB`;
}

function isMarkdown(file: SkillBuilderFile): boolean {
  return file.media_type === "text/markdown" || /\.mdx?$/iu.test(file.path);
}

function ancestorFolders(path: string | null) {
  if (!path) return [];
  const parts = path.split("/").slice(0, -1);
  return parts.map((_, index) => parts.slice(0, index + 1).join("/"));
}

export type SkillBuilderRevisionDiff = {
  added: number;
  modified: number;
  deleted: number;
  deletedPaths: string[];
  stateByPath: ReadonlyMap<string, "unchanged" | "modified" | "added">;
};

type SkillBuilderWorkbenchSurface = "files" | "secrets";

export function skillBuilderWorkbenchTabForKey(
  current: SkillBuilderWorkbenchSurface,
  key: string,
): SkillBuilderWorkbenchSurface | null {
  if (key === "Home") return "files";
  if (key === "End") return "secrets";
  if (key === "ArrowLeft" || key === "ArrowRight") {
    return current === "files" ? "secrets" : "files";
  }
  return null;
}

export function skillBuilderRevisionDiff(
  files: readonly Pick<
    SkillBuilderFile,
    "path" | "sha256" | "media_type" | "size_bytes"
  >[],
  baseFiles: readonly SkillBuilderBaseFile[],
): SkillBuilderRevisionDiff {
  const base = new Map(baseFiles.map((file) => [file.path, file]));
  const stateByPath = new Map<string, "unchanged" | "modified" | "added">();
  let added = 0;
  let modified = 0;
  for (const file of files) {
    const baseline = base.get(file.path);
    if (!baseline) {
      stateByPath.set(file.path, "added");
      added += 1;
    } else if (
      baseline.sha256 !== file.sha256 ||
      baseline.media_type !== file.media_type ||
      baseline.size_bytes !== file.size_bytes
    ) {
      stateByPath.set(file.path, "modified");
      modified += 1;
    } else {
      stateByPath.set(file.path, "unchanged");
    }
  }
  const candidatePaths = new Set(files.map((file) => file.path));
  const deletedPaths = baseFiles
    .map((file) => file.path)
    .filter((path) => !candidatePaths.has(path));
  return {
    added,
    modified,
    deleted: deletedPaths.length,
    deletedPaths,
    stateByPath,
  };
}

export function SkillBuilderCandidateWorkbench({
  projectId,
  files,
  selectedPath,
  draftContent,
  skillMdContent,
  displayMode,
  canAuthor,
  readOnly,
  dirty,
  dirtyPaths = new Set<string>(),
  baselineStale,
  pending,
  validation,
  canValidate,
  canCommit,
  acknowledgeWarnings,
  errorMessage,
  sessionKind = "create",
  baseFiles = [],
  baseVersionNumber = null,
  onSelectPath,
  onDraftContentChange,
  onSkillMdContentChange,
  onSecretValidityChange,
  onDisplayModeChange,
  onSave,
  onDiscard,
  onValidate,
  onAcknowledgeWarningsChange,
  onCommit,
  onClose,
}: {
  projectId: string;
  files: SkillBuilderFile[];
  selectedPath: string | null;
  draftContent: string;
  skillMdContent: string | null;
  displayMode: "source" | "preview";
  canAuthor: boolean;
  readOnly: boolean;
  dirty: boolean;
  dirtyPaths?: ReadonlySet<string>;
  baselineStale: boolean;
  pending: boolean;
  validation: SkillBuilderValidation | null;
  canValidate: boolean;
  canCommit: boolean;
  acknowledgeWarnings: boolean;
  errorMessage: string | null;
  sessionKind?: SkillBuilderSessionKind;
  baseFiles?: SkillBuilderBaseFile[];
  baseVersionNumber?: number | null;
  onSelectPath: (path: string) => void;
  onDraftContentChange: (content: string) => void;
  onSkillMdContentChange: (content: string) => boolean;
  onSecretValidityChange: (valid: boolean) => void;
  onDisplayModeChange: (mode: "source" | "preview") => void;
  onSave: () => void;
  onDiscard: () => void;
  onValidate: () => void;
  onAcknowledgeWarningsChange: (value: boolean) => void;
  onCommit: () => void;
  onClose?: () => void;
}) {
  const { t } = useI18n();
  const copy = t.skills.builder.workbench;
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(
    () => new Set(ancestorFolders(selectedPath)),
  );
  const [surface, setSurface] = useState<SkillBuilderWorkbenchSurface>("files");
  const tabIdPrefix = useId();
  const filesTabId = `${tabIdPrefix}-files-tab`;
  const secretsTabId = `${tabIdPrefix}-secrets-tab`;
  const filesPanelId = `${tabIdPrefix}-files-panel`;
  const secretsPanelId = `${tabIdPrefix}-secrets-panel`;
  const [secretEditorRevision, setSecretEditorRevision] = useState(0);
  const selectedFile = files.find((file) => file.path === selectedPath) ?? null;
  const markdown = selectedFile ? isMarkdown(selectedFile) : false;
  const warning = validation?.scan_decision === "warn";
  const revising = sessionKind === "revise";
  const revisionDiff = useMemo(
    () => (revising ? skillBuilderRevisionDiff(files, baseFiles) : null),
    [baseFiles, files, revising],
  );
  const workingFiles = useMemo<WorkingSkillFile[]>(
    () =>
      files.map((file) => ({
        path: file.path,
        media_type: file.media_type,
        size_bytes: file.size_bytes,
        sha256: file.sha256,
        state: dirtyPaths.has(file.path)
          ? "modified"
          : (revisionDiff?.stateByPath.get(file.path) ?? "unchanged"),
      })),
    [dirtyPaths, files, revisionDiff],
  );
  const tree = useMemo(() => buildSkillFileTree(workingFiles), [workingFiles]);
  const selection: SkillFileTreeSelection | null = selectedPath
    ? { kind: "file", path: selectedPath }
    : null;

  useEffect(() => {
    if (!selectedPath) return;
    setExpandedFolders(
      (current) => new Set([...current, ...ancestorFolders(selectedPath)]),
    );
  }, [selectedPath]);

  function toggleFolder(path: string) {
    setExpandedFolders((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  function handleSurfaceKeyDown(
    current: SkillBuilderWorkbenchSurface,
    event: KeyboardEvent<HTMLButtonElement>,
  ) {
    const next = skillBuilderWorkbenchTabForKey(current, event.key);
    if (!next) return;
    event.preventDefault();
    setSurface(next);
    event.currentTarget.ownerDocument
      .getElementById(next === "files" ? filesTabId : secretsTabId)
      ?.focus();
  }

  return (
    <section
      className="flex h-full min-h-0 flex-col"
      aria-label={copy.packageAria}
    >
      <div className="border-border/70 flex min-h-14 shrink-0 items-center justify-between gap-3 border-b px-4">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold">
            {revising ? copy.titleRevise : copy.title}
          </h2>
          <p className="text-muted-foreground truncate text-xs">
            {copy.fileCount(files.length)}
            {revisionDiff
              ? copy.diffSummary(
                  baseVersionNumber ? ` v${baseVersionNumber}` : "",
                  revisionDiff.added,
                  revisionDiff.modified,
                  revisionDiff.deleted,
                )
              : ""}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {readOnly ? (
            <span className="text-muted-foreground flex items-center gap-1.5 text-xs">
              {pending ? (
                <Loader2Icon aria-hidden className="size-3.5 animate-spin" />
              ) : null}
              {pending ? copy.updating : copy.readOnly}
            </span>
          ) : null}
          {onClose ? (
            <Button
              type="button"
              size="icon"
              variant="ghost"
              aria-label={copy.closeAria}
              onClick={onClose}
            >
              <XIcon />
            </Button>
          ) : null}
        </div>
      </div>

      <div
        className="border-border/70 bg-muted/10 flex shrink-0 gap-1 border-b p-2"
        role="tablist"
        aria-label={copy.packageAria}
      >
        <Button
          id={filesTabId}
          type="button"
          size="sm"
          variant={skillWorkbenchTabVariant(surface === "files")}
          role="tab"
          aria-selected={surface === "files"}
          aria-controls={filesPanelId}
          tabIndex={surface === "files" ? 0 : -1}
          onClick={() => setSurface("files")}
          onKeyDown={(event) => handleSurfaceKeyDown("files", event)}
        >
          <FilesIcon aria-hidden className="size-4" />
          {copy.filesSurface}
        </Button>
        <Button
          id={secretsTabId}
          type="button"
          size="sm"
          variant={skillWorkbenchTabVariant(surface === "secrets")}
          role="tab"
          aria-selected={surface === "secrets"}
          aria-controls={secretsPanelId}
          tabIndex={surface === "secrets" ? 0 : -1}
          onClick={() => setSurface("secrets")}
          onKeyDown={(event) => handleSurfaceKeyDown("secrets", event)}
        >
          <KeyRoundIcon aria-hidden className="size-4" />
          {copy.secretsSurface}
        </Button>
      </div>

      <div
        id={filesPanelId}
        hidden={surface !== "files"}
        className="grid min-h-0 flex-1 md:grid-cols-[15rem_minmax(0,1fr)]"
        role="tabpanel"
        aria-labelledby={filesTabId}
      >
        <aside className="bg-muted/20 border-border/70 min-h-40 overflow-y-auto border-b p-2 md:border-r md:border-b-0">
          {tree.length > 0 ? (
            <SkillFileTree
              nodes={tree}
              selection={selection}
              expandedFolders={expandedFolders}
              onSelectFile={onSelectPath}
              onSelectFolder={() => undefined}
              onToggleFolder={toggleFolder}
            />
          ) : (
            <p className="text-muted-foreground px-3 py-8 text-center text-xs leading-5">
              {copy.empty}
            </p>
          )}
          {revisionDiff && revisionDiff.deletedPaths.length > 0 ? (
            <div className="text-muted-foreground mt-2 space-y-1 border-t border-dashed px-3 pt-2 text-[11px] leading-5">
              <p className="font-medium">{copy.deletedFromBase}</p>
              {revisionDiff.deletedPaths.map((path) => (
                <p key={path} className="truncate font-mono line-through">
                  {path}
                </p>
              ))}
            </div>
          ) : null}
        </aside>

        <div className="min-h-0 overflow-y-auto">
          {selectedFile ? (
            <>
              <div className="border-border/70 flex min-h-14 flex-wrap items-center justify-between gap-2 border-b px-4 py-2">
                <div className="min-w-0">
                  <p className="truncate font-mono text-sm font-semibold">
                    {selectedFile.path}
                  </p>
                  <p className="text-muted-foreground mt-0.5 truncate text-[11px]">
                    {selectedFile.media_type} ·{" "}
                    {formatBytes(selectedFile.size_bytes)}
                  </p>
                </div>
                {markdown ? (
                  <div
                    className="bg-muted flex rounded-lg p-1"
                    role="group"
                    aria-label={copy.displayModeAria}
                  >
                    <Button
                      type="button"
                      size="sm"
                      variant={displayMode === "source" ? "secondary" : "ghost"}
                      className="h-7 px-2"
                      aria-pressed={displayMode === "source"}
                      onClick={() => onDisplayModeChange("source")}
                    >
                      <Code2Icon aria-hidden className="size-3.5" />
                      {copy.source}
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant={
                        displayMode === "preview" ? "secondary" : "ghost"
                      }
                      className="h-7 px-2"
                      aria-pressed={displayMode === "preview"}
                      onClick={() => onDisplayModeChange("preview")}
                    >
                      <EyeIcon aria-hidden className="size-3.5" />
                      {copy.preview}
                    </Button>
                  </div>
                ) : null}
              </div>

              {displayMode === "preview" && markdown ? (
                <div className="prose prose-neutral dark:prose-invert min-h-80 max-w-none overflow-auto p-5 text-sm">
                  <SafeStreamdown>
                    {markdownPreviewContent(draftContent)}
                  </SafeStreamdown>
                </div>
              ) : canAuthor && !readOnly ? (
                <Textarea
                  aria-label={copy.editFile(selectedFile.path)}
                  value={draftContent}
                  spellCheck={false}
                  className="min-h-80 resize-none rounded-none border-0 p-5 font-mono text-sm leading-6 shadow-none focus-visible:ring-0"
                  onChange={(event) => onDraftContentChange(event.target.value)}
                />
              ) : (
                <pre className="bg-muted/15 min-h-80 overflow-auto p-5 font-mono text-sm leading-6 whitespace-pre-wrap">
                  <code>{draftContent}</code>
                </pre>
              )}
            </>
          ) : (
            <div className="text-muted-foreground flex min-h-80 items-center justify-center p-6 text-center text-sm">
              {copy.selectFile}
            </div>
          )}
        </div>
      </div>

      <div
        id={secretsPanelId}
        hidden={surface !== "secrets"}
        className="min-h-0 flex-1 overflow-y-auto p-4"
        role="tabpanel"
        aria-labelledby={secretsTabId}
      >
        {skillMdContent === null ? (
          <p role="alert" className="text-destructive text-sm">
            {copy.secretsUnavailable}
          </p>
        ) : (
          <SkillSecretDeclarationsEditor
            key={secretEditorRevision}
            projectId={projectId}
            content={skillMdContent}
            editable={canAuthor && !readOnly}
            disabled={pending}
            onContentChange={(content) => {
              if (!onSkillMdContentChange(content)) {
                setSecretEditorRevision((current) => current + 1);
              }
            }}
            onOpenSource={() => {
              setSurface("files");
              onSelectPath("SKILL.md");
              onDisplayModeChange("source");
            }}
            onValidityChange={onSecretValidityChange}
          />
        )}
      </div>

      {dirty && canAuthor ? (
        <div className="border-border/70 bg-background flex shrink-0 flex-col gap-3 border-t p-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-muted-foreground text-xs">
            {baselineStale ? copy.baselineStale : copy.unsavedHint}
          </p>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={pending}
              onClick={onDiscard}
            >
              {baselineStale ? copy.loadLatest : copy.discard}
            </Button>
            <Button
              type="button"
              disabled={pending || baselineStale}
              onClick={onSave}
            >
              {pending ? (
                <Loader2Icon aria-hidden className="size-4 animate-spin" />
              ) : (
                <SaveIcon aria-hidden className="size-4" />
              )}
              {pending ? copy.saving : copy.save}
            </Button>
          </div>
        </div>
      ) : null}

      <div className="border-border/70 bg-muted/15 shrink-0 space-y-3 border-t p-4">
        {validation ? (
          <div className="space-y-2 text-xs">
            <p className="flex items-center gap-2 font-medium">
              {warning ? (
                <FileWarningIcon
                  aria-hidden
                  className="size-4 text-amber-600"
                />
              ) : (
                <CheckCircle2Icon
                  aria-hidden
                  className="size-4 text-emerald-600"
                />
              )}
              {warning ? copy.checkPassedWithWarnings : copy.checkPassed}
            </p>
            <p className="text-muted-foreground">{validation.description}</p>
            {validation.scan_rule_ids.length > 0 ? (
              <p className="font-mono break-all">
                {validation.scan_rule_ids.join(" · ")}
              </p>
            ) : null}
            {validation.secret_requirements.length > 0 ? (
              <p className="text-muted-foreground">
                {copy.requiredCredentials}
                {validation.secret_requirements
                  .map((item) => item.name)
                  .join(", ")}
              </p>
            ) : null}
          </div>
        ) : (
          <p className="text-muted-foreground text-xs leading-5">
            {copy.recheckHint}
          </p>
        )}

        {warning ? (
          <label className="flex cursor-pointer items-start gap-2 text-xs">
            <input
              type="checkbox"
              className="mt-0.5 size-4"
              checked={acknowledgeWarnings}
              disabled={!canAuthor || pending}
              onChange={(event) =>
                onAcknowledgeWarningsChange(event.target.checked)
              }
            />
            <span>{copy.acknowledgeWarnings}</span>
          </label>
        ) : null}

        {errorMessage ? (
          <p role="alert" className="text-destructive text-xs">
            {errorMessage}
          </p>
        ) : null}

        {canAuthor ? (
          <div className="flex flex-col gap-2 sm:flex-row sm:justify-end">
            <Button
              type="button"
              variant="outline"
              disabled={!canValidate || pending}
              onClick={onValidate}
            >
              {pending ? (
                <Loader2Icon aria-hidden className="size-4 animate-spin" />
              ) : (
                <ShieldCheckIcon aria-hidden className="size-4" />
              )}
              {copy.checkSkill}
            </Button>
            <Button
              type="button"
              disabled={
                !canCommit || pending || (warning && !acknowledgeWarnings)
              }
              onClick={onCommit}
            >
              {revising ? copy.commitRevise : copy.commitCreate}
            </Button>
          </div>
        ) : null}
      </div>
    </section>
  );
}
