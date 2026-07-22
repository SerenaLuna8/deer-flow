"use client";

import {
  Code2Icon,
  EyeIcon,
  FileCode2Icon,
  FileWarningIcon,
  Loader2Icon,
  PencilIcon,
  PlusIcon,
  RotateCcwIcon,
  SaveIcon,
  Trash2Icon,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
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
import {
  SharedAssetApiError,
  useForkProjectSkillVersion,
  useProjectSkillVersionFile,
  type ProjectAssetItem,
  type SkillFileChange,
} from "@/core/shared-assets";
import { SafeStreamdown } from "@/core/streamdown/components";
import { cn } from "@/lib/utils";

import type { SkillAssetVersion } from "./skill-asset-detail";
import {
  addSkillFile,
  deleteSkillFile,
  editSkillFile,
  listWorkingSkillFiles,
  renameSkillFile,
} from "./skill-file-workbench-state";

type PathDialogMode = "add" | "rename";

const FILE_STATE_LABEL = {
  unchanged: null,
  modified: "已修改",
  added: "新增",
} as const;

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  const kibibytes = value / 1024;
  if (kibibytes < 1024) return `${Number(kibibytes.toFixed(1))} KB`;
  return `${Number((kibibytes / 1024).toFixed(1))} MB`;
}

function mediaTypeForPath(path: string): string {
  const extension = path.split(".").pop()?.toLocaleLowerCase();
  if (extension === "md" || extension === "mdx") return "text/markdown";
  if (extension === "json") return "application/json";
  if (extension === "yaml" || extension === "yml") return "application/yaml";
  if (extension === "py") return "text/x-python";
  if (extension === "js" || extension === "mjs") return "text/javascript";
  if (extension === "ts" || extension === "tsx") return "text/typescript";
  if (extension === "html") return "text/html";
  if (extension === "css") return "text/css";
  if (extension === "sql") return "application/sql";
  if (extension === "sh") return "application/x-sh";
  return "text/plain";
}

function pathErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    if (error.message.includes("already exists")) return "这个文件路径已存在。";
    if (error.message.includes("SKILL.md"))
      return "根目录 SKILL.md 不能重命名或删除。";
  }
  return "文件路径无效。请使用项目内的相对 POSIX 路径。";
}

function isMarkdown(path: string, mediaType: string): boolean {
  return mediaType === "text/markdown" || /\.mdx?$/i.test(path);
}

function basename(path: string): string {
  return path.split("/").at(-1) ?? path;
}

function dirname(path: string): string {
  const parts = path.split("/");
  return parts.length > 1 ? parts.slice(0, -1).join("/") : "根目录";
}

export function SkillVersionWorkbench({
  accountId,
  projectId,
  item,
  version,
  canAuthor,
  onDirtyChange,
  onVersionCreated,
}: {
  accountId: string;
  projectId: string;
  item: ProjectAssetItem;
  version: SkillAssetVersion;
  canAuthor: boolean;
  onDirtyChange: (dirty: boolean) => void;
  onVersionCreated: (versionId: string) => void;
}) {
  const initialPath =
    version.file_views.find((file) => file.path === "SKILL.md")?.path ??
    version.file_views[0]?.path ??
    "";
  const [selectedPath, setSelectedPath] = useState(initialPath);
  const [editing, setEditing] = useState(false);
  const [changes, setChanges] = useState<SkillFileChange[]>([]);
  const [loadedSources, setLoadedSources] = useState<Record<string, string>>(
    {},
  );
  const [displayMode, setDisplayMode] = useState<"source" | "preview">(
    "source",
  );
  const [pathDialog, setPathDialog] = useState<PathDialogMode | null>(null);
  const [pathInput, setPathInput] = useState("");
  const [pathError, setPathError] = useState<string | null>(null);
  const [deletePath, setDeletePath] = useState<string | null>(null);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const fork = useForkProjectSkillVersion(accountId, projectId);

  const workingFiles = useMemo(
    () => listWorkingSkillFiles(version.file_views, changes),
    [changes, version.file_views],
  );
  const selectedFile =
    workingFiles.find((file) => file.path === selectedPath) ?? null;
  const selectedChange = changes.find(
    (change) => change.path === selectedPath && change.op !== "delete",
  );
  const selectedIsLocal = selectedChange?.op === "create";
  const queryPath = selectedPath === "" ? "SKILL.md" : selectedPath;
  const source = useProjectSkillVersionFile(
    accountId,
    projectId,
    item.id,
    version.id,
    queryPath,
    selectedPath !== "" && selectedFile !== null && !selectedIsLocal,
  );
  const serverFile = source.data?.data;
  const sourceContent =
    selectedChange && selectedChange.op !== "delete"
      ? selectedChange.content
      : (loadedSources[selectedPath] ?? serverFile?.content ?? null);
  const previewStatus = selectedIsLocal
    ? "ready"
    : (serverFile?.preview_status ?? null);
  const markdown = Boolean(
    selectedFile && isMarkdown(selectedFile.path, selectedFile.media_type),
  );
  const dirty = changes.length > 0;

  useEffect(() => {
    if (serverFile?.preview_status !== "ready" || serverFile.content === null)
      return;
    setLoadedSources((current) =>
      current[serverFile.path] === serverFile.content
        ? current
        : { ...current, [serverFile.path]: serverFile.content ?? "" },
    );
  }, [serverFile]);

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    if (!dirty) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [dirty]);

  useEffect(
    () => () => {
      onDirtyChange(false);
    },
    [onDirtyChange],
  );

  function selectFile(path: string) {
    setSelectedPath(path);
    setDisplayMode("source");
    setLocalError(null);
  }

  function beginPathDialog(mode: PathDialogMode) {
    setPathDialog(mode);
    setPathInput(mode === "rename" ? selectedPath : "references/notes.md");
    setPathError(null);
  }

  function commitPathDialog() {
    try {
      if (pathDialog === "add") {
        const next = addSkillFile(
          changes,
          version.file_views,
          pathInput,
          "",
          mediaTypeForPath(pathInput),
        );
        setChanges(next);
        setSelectedPath(pathInput);
      } else if (
        pathDialog === "rename" &&
        selectedFile &&
        sourceContent !== null
      ) {
        const next = renameSkillFile(
          changes,
          version.file_views,
          selectedFile.path,
          pathInput,
          sourceContent,
          mediaTypeForPath(pathInput),
        );
        setChanges(next);
        setSelectedPath(pathInput);
      }
      setPathDialog(null);
      setPathError(null);
      setDisplayMode("source");
    } catch (error) {
      setPathError(pathErrorMessage(error));
    }
  }

  function confirmDelete() {
    if (!deletePath) return;
    try {
      const next = deleteSkillFile(changes, version.file_views, deletePath);
      setChanges(next);
      const nextFiles = listWorkingSkillFiles(version.file_views, next);
      setSelectedPath(
        nextFiles.find((file) => file.path === "SKILL.md")?.path ??
          nextFiles[0]?.path ??
          "",
      );
      setDeletePath(null);
      setDisplayMode("source");
    } catch (error) {
      setDeletePath(null);
      setLocalError(pathErrorMessage(error));
    }
  }

  function discardChanges() {
    setChanges([]);
    setLoadedSources({});
    setEditing(false);
    setDiscardOpen(false);
    setLocalError(null);
    setSelectedPath(initialPath);
    setDisplayMode("source");
  }

  async function saveAsDraft() {
    setLocalError(null);
    try {
      const result = await fork.mutateAsync({
        assetId: item.id,
        sourceVersionId: version.id,
        input: {
          expected_asset_version: item.version,
          expected_source_payload_checksum: version.payload_checksum,
          changes,
        },
      });
      if (!("skill_id" in result.data)) {
        setLocalError("服务返回了无效的 Skill 版本。请重试。");
        return;
      }
      setSelectedPath(initialPath);
      setChanges([]);
      setLoadedSources({});
      setEditing(false);
      onDirtyChange(false);
      onVersionCreated(result.data.id);
    } catch (error) {
      if (
        error instanceof SharedAssetApiError &&
        error.code === "ASSET_CONFLICT"
      ) {
        setLocalError(
          "资产已在其他窗口发生变化。本地修改仍然保留，请刷新版本后重新提交。",
        );
      } else {
        setLocalError(adminAssetErrorMessage(error));
      }
    }
  }

  return (
    <section className="space-y-4" aria-label="Skill 版本文件">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h3 className="text-sm font-semibold">版本文件</h3>
          <p className="text-muted-foreground mt-1 max-w-2xl text-xs leading-5">
            文件来自版本 {version.version_number}{" "}
            的不可变快照。修改会另存为新的草稿版本，当前版本不会被覆盖。
          </p>
        </div>
        {canAuthor && !editing && (
          <Button type="button" size="sm" onClick={() => setEditing(true)}>
            <PencilIcon aria-hidden className="size-4" />
            编辑为新版本
          </Button>
        )}
      </div>

      {editing && (
        <div className="border-primary/20 bg-primary/5 flex flex-col gap-3 rounded-xl border px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm font-medium">
              正在基于版本 {version.version_number} 编辑副本
            </p>
            <p className="text-muted-foreground mt-1 text-xs">
              {dirty ? `已有 ${changes.length} 项未保存修改` : "尚未修改文件"}
            </p>
          </div>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => beginPathDialog("add")}
          >
            <PlusIcon aria-hidden className="size-4" />
            新建文件
          </Button>
        </div>
      )}

      {workingFiles.length === 0 ? (
        <p className="text-muted-foreground rounded-xl border border-dashed p-5 text-sm">
          这个版本没有可显示的文件。
        </p>
      ) : (
        <div className="border-border/70 overflow-hidden rounded-2xl border md:grid md:grid-cols-[220px_minmax(0,1fr)]">
          <div className="bg-muted/20 border-border/70 border-b p-3 md:border-r md:border-b-0">
            <label className="block md:hidden">
              <span className="text-muted-foreground mb-2 block text-xs font-medium">
                文件{" "}
                {workingFiles.findIndex((file) => file.path === selectedPath) +
                  1}
                /{workingFiles.length}
              </span>
              <select
                aria-label="选择 Skill 文件"
                value={selectedPath}
                onChange={(event) => selectFile(event.target.value)}
                className="border-input bg-background h-10 w-full rounded-lg border px-3 text-sm"
              >
                {workingFiles.map((file) => (
                  <option key={file.path} value={file.path}>
                    {file.path}
                    {file.state === "unchanged"
                      ? ""
                      : ` · ${FILE_STATE_LABEL[file.state]}`}
                  </option>
                ))}
              </select>
            </label>

            <nav aria-label="Skill 文件" className="hidden space-y-1 md:block">
              <p className="text-muted-foreground px-2 pb-2 text-[11px] font-medium tracking-wide uppercase">
                文件 · {workingFiles.length}
              </p>
              {workingFiles.map((file) => {
                const active = file.path === selectedPath;
                return (
                  <button
                    key={file.path}
                    type="button"
                    className={cn(
                      "hover:bg-background focus-visible:ring-ring flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-sm transition-colors focus-visible:ring-2 focus-visible:outline-none",
                      active && "bg-background shadow-sm",
                    )}
                    style={{
                      paddingLeft: `${8 + Math.min(file.path.split("/").length - 1, 4) * 12}px`,
                    }}
                    aria-current={active ? "page" : undefined}
                    onClick={() => selectFile(file.path)}
                  >
                    <FileCode2Icon
                      aria-hidden
                      className="text-muted-foreground size-4 shrink-0"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-medium">
                        {basename(file.path)}
                      </span>
                      {file.path.includes("/") && (
                        <span className="text-muted-foreground block truncate text-[10px]">
                          {dirname(file.path)}
                        </span>
                      )}
                    </span>
                    {file.state !== "unchanged" && (
                      <span className="text-primary text-[10px] font-medium">
                        {FILE_STATE_LABEL[file.state]}
                      </span>
                    )}
                  </button>
                );
              })}
            </nav>
          </div>

          <div className="min-w-0">
            {selectedFile && (
              <>
                <div className="border-border/70 flex flex-col gap-3 border-b px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <p className="truncate font-mono text-sm font-semibold">
                      {selectedFile.path}
                    </p>
                    <p className="text-muted-foreground mt-1 truncate text-[11px]">
                      {selectedFile.media_type} ·{" "}
                      {formatBytes(selectedFile.size_bytes)}
                      {selectedFile.sha256
                        ? ` · SHA-256 ${selectedFile.sha256.slice(0, 12)}…`
                        : " · 尚未保存"}
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {markdown && previewStatus === "ready" && (
                      <div
                        className="bg-muted flex rounded-lg p-1"
                        role="group"
                        aria-label="文件显示方式"
                      >
                        <Button
                          type="button"
                          size="sm"
                          variant={
                            displayMode === "source" ? "secondary" : "ghost"
                          }
                          className="h-7 px-2"
                          aria-pressed={displayMode === "source"}
                          onClick={() => setDisplayMode("source")}
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
                          onClick={() => setDisplayMode("preview")}
                        >
                          <EyeIcon aria-hidden className="size-3.5" />
                          预览
                        </Button>
                      </div>
                    )}
                    {editing && selectedFile.path !== "SKILL.md" && (
                      <>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={sourceContent === null}
                          onClick={() => beginPathDialog("rename")}
                        >
                          重命名
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => setDeletePath(selectedFile.path)}
                        >
                          <Trash2Icon aria-hidden className="size-4" />
                          删除
                        </Button>
                      </>
                    )}
                  </div>
                </div>

                {source.isLoading && !selectedIsLocal ? (
                  <div className="space-y-3 p-4" aria-label="正在加载文件内容">
                    <Skeleton className="h-5 w-48" />
                    <Skeleton className="h-[50vh] min-h-80 w-full rounded-xl md:h-[520px]" />
                  </div>
                ) : source.error && !selectedIsLocal ? (
                  <div className="m-4 rounded-xl border border-dashed p-6 text-center">
                    <FileWarningIcon
                      aria-hidden
                      className="text-muted-foreground mx-auto size-6"
                    />
                    <p role="alert" className="mt-3 text-sm font-medium">
                      文件内容加载失败
                    </p>
                    <p className="text-muted-foreground mt-1 text-xs">
                      {adminAssetErrorMessage(source.error)}
                    </p>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      className="mt-4"
                      disabled={source.isFetching}
                      onClick={() => void source.refetch()}
                    >
                      <RotateCcwIcon aria-hidden className="size-4" />
                      重试
                    </Button>
                  </div>
                ) : previewStatus === "binary" ? (
                  <div className="m-4 flex min-h-72 flex-col items-center justify-center rounded-xl border border-dashed px-6 text-center">
                    <FileWarningIcon
                      aria-hidden
                      className="text-muted-foreground size-7"
                    />
                    <p className="mt-3 text-sm font-medium">
                      二进制文件仅显示元数据
                    </p>
                    <p className="text-muted-foreground mt-1 max-w-sm text-xs leading-5">
                      为避免执行或泄露不可读内容，这个版本不返回二进制正文。编辑副本时仍会完整保留该文件。
                    </p>
                  </div>
                ) : previewStatus === "too_large" ? (
                  <div className="m-4 flex min-h-72 flex-col items-center justify-center rounded-xl border border-dashed px-6 text-center">
                    <FileWarningIcon
                      aria-hidden
                      className="text-muted-foreground size-7"
                    />
                    <p className="mt-3 text-sm font-medium">
                      文件过大，无法在线预览
                    </p>
                    <p className="text-muted-foreground mt-1 text-xs">
                      文件仍会保留在版本快照中。
                    </p>
                  </div>
                ) : sourceContent !== null ? (
                  displayMode === "preview" && markdown ? (
                    <div className="prose prose-neutral dark:prose-invert min-h-[50vh] max-w-none overflow-auto p-5 text-sm md:min-h-[520px]">
                      <SafeStreamdown>{sourceContent}</SafeStreamdown>
                    </div>
                  ) : editing ? (
                    <Textarea
                      aria-label={`编辑 ${selectedFile.path}`}
                      value={sourceContent}
                      spellCheck={false}
                      className="min-h-[50vh] resize-y rounded-none border-0 p-5 font-mono text-sm leading-6 shadow-none focus-visible:ring-0 md:min-h-[520px]"
                      onChange={(event) => {
                        try {
                          setChanges(
                            editSkillFile(
                              changes,
                              version.file_views,
                              selectedFile.path,
                              event.target.value,
                            ),
                          );
                          setLocalError(null);
                        } catch (error) {
                          setLocalError(pathErrorMessage(error));
                        }
                      }}
                    />
                  ) : (
                    <pre className="bg-muted/15 min-h-[50vh] overflow-auto p-5 font-mono text-sm leading-6 whitespace-pre-wrap md:min-h-[520px]">
                      <code>{sourceContent}</code>
                    </pre>
                  )
                ) : (
                  <div className="text-muted-foreground p-5 text-sm">
                    文件正文不可用。
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {(localError ?? fork.error) && (
        <p role="alert" className="text-destructive text-sm">
          {localError ?? adminAssetErrorMessage(fork.error)}
        </p>
      )}

      {editing && (
        <div className="bg-background/95 sticky bottom-0 z-10 -mx-1 flex flex-col gap-3 rounded-xl border p-3 shadow-lg backdrop-blur sm:flex-row sm:items-center sm:justify-between">
          <p className="text-muted-foreground text-xs">
            保存后创建新的 Draft；发布仍需单独确认。
          </p>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              className="min-h-11 flex-1 sm:flex-none"
              onClick={() => (dirty ? setDiscardOpen(true) : discardChanges())}
            >
              放弃修改
            </Button>
            <Button
              type="button"
              className="min-h-11 flex-1 sm:flex-none"
              disabled={!dirty || fork.isPending}
              onClick={() => void saveAsDraft()}
            >
              {fork.isPending ? (
                <Loader2Icon aria-hidden className="size-4 animate-spin" />
              ) : (
                <SaveIcon aria-hidden className="size-4" />
              )}
              {fork.isPending ? "保存中…" : "保存为新版本"}
            </Button>
          </div>
        </div>
      )}

      <Dialog
        open={pathDialog !== null}
        onOpenChange={(open) => !open && setPathDialog(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {pathDialog === "rename" ? "重命名文件" : "新建文件"}
            </DialogTitle>
            <DialogDescription>
              使用相对路径，例如 references/guide.md。保存 Skill
              时会统一校验路径与内容。
            </DialogDescription>
          </DialogHeader>
          <label className="space-y-2">
            <span className="text-sm font-medium">文件路径</span>
            <Input
              autoFocus
              value={pathInput}
              onChange={(event) => {
                setPathInput(event.target.value);
                setPathError(null);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  commitPathDialog();
                }
              }}
            />
          </label>
          {pathError && (
            <p role="alert" className="text-destructive text-sm">
              {pathError}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setPathDialog(null)}
            >
              取消
            </Button>
            <Button type="button" onClick={commitPathDialog}>
              {pathDialog === "rename" ? "确认重命名" : "创建文件"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={deletePath !== null}
        onOpenChange={(open) => !open && setDeletePath(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>从新版本中删除文件？</DialogTitle>
            <DialogDescription>
              {deletePath} 会从新草稿中移除，原版本的文件不会受影响。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setDeletePath(null)}
            >
              取消
            </Button>
            <Button type="button" variant="destructive" onClick={confirmDelete}>
              删除文件
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={discardOpen} onOpenChange={setDiscardOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>放弃未保存的修改？</DialogTitle>
            <DialogDescription>
              当前 {changes.length} 项文件修改将被清除，已保存的版本不会受影响。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setDiscardOpen(false)}
            >
              继续编辑
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={discardChanges}
            >
              放弃修改
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
