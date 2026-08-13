"use client";

import {
  CheckCircle2Icon,
  Code2Icon,
  EyeIcon,
  FileWarningIcon,
  Loader2Icon,
  SaveIcon,
  ShieldCheckIcon,
  XIcon,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { SkillFileTree } from "@/components/projects/assets/skill-file-tree";
import {
  buildSkillFileTree,
  markdownPreviewContent,
  type SkillFileTreeSelection,
  type WorkingSkillFile,
} from "@/components/projects/assets/skill-file-workbench-state";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type {
  SkillBuilderFile,
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

export function SkillBuilderCandidateWorkbench({
  files,
  selectedPath,
  draftContent,
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
  onSelectPath,
  onDraftContentChange,
  onDisplayModeChange,
  onSave,
  onDiscard,
  onValidate,
  onAcknowledgeWarningsChange,
  onCommit,
  onClose,
}: {
  files: SkillBuilderFile[];
  selectedPath: string | null;
  draftContent: string;
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
  onSelectPath: (path: string) => void;
  onDraftContentChange: (content: string) => void;
  onDisplayModeChange: (mode: "source" | "preview") => void;
  onSave: () => void;
  onDiscard: () => void;
  onValidate: () => void;
  onAcknowledgeWarningsChange: (value: boolean) => void;
  onCommit: () => void;
  onClose?: () => void;
}) {
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(
    () => new Set(ancestorFolders(selectedPath)),
  );
  const selectedFile = files.find((file) => file.path === selectedPath) ?? null;
  const markdown = selectedFile ? isMarkdown(selectedFile) : false;
  const warning = validation?.scan_decision === "warn";
  const workingFiles = useMemo<WorkingSkillFile[]>(
    () =>
      files.map((file) => ({
        path: file.path,
        media_type: file.media_type,
        size_bytes: file.size_bytes,
        sha256: file.sha256,
        state: dirtyPaths.has(file.path) ? "modified" : "unchanged",
      })),
    [dirtyPaths, files],
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

  return (
    <section className="flex h-full min-h-0 flex-col" aria-label="候选文件包">
      <div className="border-border/70 flex min-h-14 shrink-0 items-center justify-between gap-3 border-b px-4">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold">候选文件包</h2>
          <p className="text-muted-foreground text-xs">
            {files.length} 个 UTF-8 文本文件
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {readOnly ? (
            <span className="text-muted-foreground flex items-center gap-1.5 text-xs">
              {pending ? (
                <Loader2Icon aria-hidden className="size-3.5 animate-spin" />
              ) : null}
              {pending ? "正在更新" : "只读"}
            </span>
          ) : null}
          {onClose ? (
            <Button
              type="button"
              size="icon"
              variant="ghost"
              aria-label="关闭候选文件包"
              onClick={onClose}
            >
              <XIcon />
            </Button>
          ) : null}
        </div>
      </div>

      <div className="grid min-h-0 flex-1 md:grid-cols-[15rem_minmax(0,1fr)]">
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
              通过左侧对话描述 Skill 后，候选文件会显示在这里。
            </p>
          )}
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
                    aria-label="文件显示方式"
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
                      源码
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
                      预览
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
                  aria-label={`编辑 ${selectedFile.path}`}
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
              选择一个文件查看内容。
            </div>
          )}
        </div>
      </div>

      {dirty && canAuthor ? (
        <div className="border-border/70 bg-background flex shrink-0 flex-col gap-3 border-t p-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-muted-foreground text-xs">
            {baselineStale
              ? "候选包已在其他位置更新。本地修改仍可复制；请加载最新版本后再编辑。"
              : "文件有未保存修改；保存前不能继续对话或检查。"}
          </p>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={pending}
              onClick={onDiscard}
            >
              {baselineStale ? "加载最新版本" : "放弃修改"}
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
              {pending ? "保存中…" : "保存修改"}
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
              {warning ? "检查通过，但有警告" : "检查通过"}
            </p>
            <p className="text-muted-foreground">{validation.description}</p>
            {validation.scan_rule_ids.length > 0 ? (
              <p className="font-mono break-all">
                {validation.scan_rule_ids.join(" · ")}
              </p>
            ) : null}
            {validation.secret_requirements.length > 0 ? (
              <p className="text-muted-foreground">
                所需凭据：
                {validation.secret_requirements
                  .map((item) => item.name)
                  .join("、")}
              </p>
            ) : null}
          </div>
        ) : (
          <p className="text-muted-foreground text-xs leading-5">
            每次候选文件变化后都需要重新检查路径、frontmatter、安全规则和配额。
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
            <span>确认并接受上述警告</span>
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
              检查 Skill
            </Button>
            <Button
              type="button"
              disabled={
                !canCommit || pending || (warning && !acknowledgeWarnings)
              }
              onClick={onCommit}
            >
              创建 Skill（默认停用）
            </Button>
          </div>
        ) : null}
      </div>
    </section>
  );
}
