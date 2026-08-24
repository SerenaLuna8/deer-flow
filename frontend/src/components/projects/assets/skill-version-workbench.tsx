"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  Code2Icon,
  EyeIcon,
  FilePlus2Icon,
  FileWarningIcon,
  FolderPlusIcon,
  FilesIcon,
  KeyRoundIcon,
  Loader2Icon,
  RotateCcwIcon,
  SaveIcon,
  Trash2Icon,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

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
import { useI18n } from "@/core/i18n/hooks";
import {
  SharedAssetApiError,
  useForkProjectSkillVersion,
  useProjectSkillVersionFile,
  type ProjectAssetItem,
  type SkillFileChange,
} from "@/core/shared-assets";
import { invalidateProjectSkillConflictQueries } from "@/core/shared-assets/hooks";
import { SafeStreamdown } from "@/core/streamdown/components";

import type { SkillAssetVersion } from "./skill-asset-detail";
import { SkillFileTree } from "./skill-file-tree";
import {
  addSkillFile,
  addSkillFolder,
  buildSkillFileTree,
  defaultSkillFileFolder,
  deleteSkillFile,
  editSkillFile,
  joinSkillFilePath,
  listSkillFolderPaths,
  listWorkingSkillFiles,
  markdownPreviewContent,
  renameSkillFile,
  type SkillFileTreeSelection,
} from "./skill-file-workbench-state";
import { SkillSecretDeclarationsEditor } from "./skill-secret-declarations-editor";
import { skillWorkbenchTabVariant } from "./skill-workbench-tabs";

type PathDialogMode = "add" | "rename";
type WorkbenchSurface = "files" | "secrets";

export function beginSkillSecretEditing(
  setSurface: (surface: WorkbenchSurface) => void,
  onEditingChange: (editing: boolean) => void,
): void {
  setSurface("secrets");
  onEditingChange(true);
}

export function notifySkillCandidateVersionCreated(
  onVersionCreated: (
    versionId: string,
    options?: { focusSecrets?: boolean },
  ) => void,
  version: Pick<SkillAssetVersion, "id" | "secret_requirements">,
): void {
  onVersionCreated(version.id, {
    focusSecrets: version.secret_requirements.length > 0,
  });
}

export function skillVersionWorkbenchTabForKey(
  current: WorkbenchSurface,
  key: string,
): WorkbenchSurface | null {
  if (key === "Home") return "files";
  if (key === "End") return "secrets";
  if (key === "ArrowLeft" || key === "ArrowRight") {
    return current === "files" ? "secrets" : "files";
  }
  return null;
}

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
    if (error.message.includes("Parent folder"))
      return "请选择已经存在的父文件夹。";
    if (error.message.includes("Folder name"))
      return "文件夹名称只能是一段有效名称，不能包含斜杠。";
    if (error.message.includes("Filename"))
      return "文件名不能为空，也不能包含斜杠。";
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

function directoryPath(path: string): string {
  const directory = dirname(path);
  return directory === "根目录" ? "" : directory;
}

function folderPathWithName(parent: string, name: string): string {
  return parent ? `${parent}/${name}` : name;
}

function foldersThrough(path: string): string[] {
  if (!path) return [];
  const parts = path.split("/");
  return parts.map((_, index) => parts.slice(0, index + 1).join("/"));
}

export function initialSkillFilePath(
  files: readonly { path: string }[],
): string {
  return (
    files.find((file) => file.path === "SKILL.md")?.path ?? files[0]?.path ?? ""
  );
}

export function selectedSkillFileAncestorFolders(path: string): string[] {
  return foldersThrough(directoryPath(path));
}

type SkillVersionConflictRecovery = {
  assetId: string;
  assetVersion: number;
  sourceVersionId: string;
};

type PendingSavedSkillVersion = {
  assetId: string;
  assetVersion: number;
};

export function skillVersionConflictHasLatestServerState(
  conflict: SkillVersionConflictRecovery | null,
  item: Pick<ProjectAssetItem, "id" | "revision">,
  sourceVersion: Pick<SkillAssetVersion, "id">,
): boolean {
  return (
    conflict?.assetId === item.id &&
    conflict.sourceVersionId === sourceVersion.id &&
    item.revision > conflict.assetVersion
  );
}

export function skillVersionSaveIsPending(
  pending: PendingSavedSkillVersion | null,
  item: Pick<ProjectAssetItem, "id" | "revision">,
): boolean {
  return pending?.assetId === item.id && item.revision < pending.assetVersion;
}

export function skillVersionDraftMatchesSubmittedChanges(
  submitted: readonly SkillFileChange[],
  current: readonly SkillFileChange[],
): boolean {
  if (submitted.length !== current.length) return false;
  return submitted.every((submittedChange, index) => {
    const currentChange = current[index];
    if (submittedChange.op !== currentChange?.op) return false;
    if (submittedChange.path !== currentChange.path) return false;
    if (submittedChange.op === "delete") return true;
    return (
      currentChange.op !== "delete" &&
      submittedChange.content === currentChange.content &&
      submittedChange.media_type === currentChange.media_type
    );
  });
}

export function skillVersionSnapshotCopy(
  scope: ProjectAssetItem["scope"],
  relation: SkillAssetVersion["relation"],
  versionNumber: number,
): string {
  if (scope === "system") {
    return `文件来自 System Skill 的 Current Version v${versionNumber}，由软件包统一管理，当前为只读。`;
  }
  if (relation === "historical") {
    return `文件来自历史版本 ${versionNumber} 的不可变快照。历史版本仅供查看和导出，不能修改或作为新版本的编辑基线。`;
  }
  return `文件来自版本 ${versionNumber} 的不可变快照。修改会另存为新的候选版本，当前版本不会被覆盖。`;
}

export function SkillVersionWorkbench({
  accountId,
  projectId,
  item,
  version,
  canAuthor,
  editing,
  onEditingChange,
  onDirtyChange,
  onActivationValidityChange,
  onVersionCreated,
  secretConfigurationDirty = false,
  focusSecrets = false,
  onSecretsFocused,
  secretConfiguration,
}: {
  accountId: string;
  projectId: string;
  item: ProjectAssetItem;
  version: SkillAssetVersion;
  canAuthor: boolean;
  editing: boolean;
  onEditingChange: (editing: boolean) => void;
  onDirtyChange: (dirty: boolean) => void;
  onActivationValidityChange: (valid: boolean) => void;
  onVersionCreated: (
    versionId: string,
    options?: { focusSecrets?: boolean },
  ) => void;
  secretConfigurationDirty?: boolean;
  focusSecrets?: boolean;
  onSecretsFocused?: () => void;
  secretConfiguration?: ReactNode;
}) {
  const { t } = useI18n();
  const secretCopy = t.skills.secrets;
  const initialPath = initialSkillFilePath(version.file_views);
  const [selection, setSelection] = useState<SkillFileTreeSelection | null>(
    initialPath ? { kind: "file", path: initialPath } : null,
  );
  const [changes, setChanges] = useState<SkillFileChange[]>([]);
  const [explicitFolders, setExplicitFolders] = useState<string[]>([]);
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(
    () => new Set(selectedSkillFileAncestorFolders(initialPath)),
  );
  const [loadedSources, setLoadedSources] = useState<Record<string, string>>(
    {},
  );
  const [displayMode, setDisplayMode] = useState<"source" | "preview">(
    "source",
  );
  const [surface, setSurface] = useState<WorkbenchSurface>("files");
  const tabIdPrefix = useId();
  const filesTabId = `${tabIdPrefix}-files-tab`;
  const secretsTabId = `${tabIdPrefix}-secrets-tab`;
  const filesPanelId = `${tabIdPrefix}-files-panel`;
  const secretsPanelId = `${tabIdPrefix}-secrets-panel`;
  const secretsPanelRef = useRef<HTMLDivElement | null>(null);
  const [secretDeclarationValid, setSecretDeclarationValid] = useState(false);
  const [pathDialog, setPathDialog] = useState<PathDialogMode | null>(null);
  const [pathInput, setPathInput] = useState("");
  const [fileNameInput, setFileNameInput] = useState("");
  const [targetFolder, setTargetFolder] = useState("");
  const [inlineFolderOpen, setInlineFolderOpen] = useState(false);
  const [inlineFolderName, setInlineFolderName] = useState("");
  const [pathError, setPathError] = useState<string | null>(null);
  const [folderDialogOpen, setFolderDialogOpen] = useState(false);
  const [folderParent, setFolderParent] = useState("");
  const [folderNameInput, setFolderNameInput] = useState("");
  const [folderError, setFolderError] = useState<string | null>(null);
  const [deletePath, setDeletePath] = useState<string | null>(null);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const changesRef = useRef(changes);
  const expectedAssetVersionRef = useRef(item.revision);
  const appliedServerStateRef = useRef(
    `${item.id}:${item.revision}:${version.id}`,
  );
  const conflictRecoveryRef = useRef<SkillVersionConflictRecovery | null>(null);
  const pendingSavedVersionRef = useRef<PendingSavedSkillVersion | null>(null);
  const queryClient = useQueryClient();
  const fork = useForkProjectSkillVersion(accountId, projectId);

  function handleSurfaceKeyDown(
    current: WorkbenchSurface,
    event: KeyboardEvent<HTMLButtonElement>,
  ) {
    const next = skillVersionWorkbenchTabForKey(current, event.key);
    if (!next) return;
    event.preventDefault();
    setSurface(next);
    event.currentTarget.ownerDocument
      .getElementById(next === "files" ? filesTabId : secretsTabId)
      ?.focus();
  }

  function replaceChanges(next: SkillFileChange[]) {
    changesRef.current = next;
    setChanges(next);
  }

  const workingFiles = useMemo(
    () => listWorkingSkillFiles(version.file_views, changes),
    [changes, version.file_views],
  );
  const folderPaths = useMemo(
    () => listSkillFolderPaths(workingFiles, explicitFolders),
    [explicitFolders, workingFiles],
  );
  const fileTree = useMemo(
    () => buildSkillFileTree(workingFiles, explicitFolders),
    [explicitFolders, workingFiles],
  );
  const selectedPath = selection?.kind === "file" ? selection.path : "";
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
  const skillMdChange = changes.find(
    (change) => change.path === "SKILL.md" && change.op !== "delete",
  );
  const skillMdSource = useProjectSkillVersionFile(
    accountId,
    projectId,
    item.id,
    version.id,
    "SKILL.md",
    selectedPath !== "SKILL.md" &&
      ((canAuthor && editing) || surface === "secrets"),
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
  const isEditing = canAuthor && editing;
  const skillMdServerFile =
    selectedPath === "SKILL.md" ? serverFile : skillMdSource.data?.data;
  const skillMdContent =
    skillMdChange && skillMdChange.op !== "delete"
      ? skillMdChange.content
      : (loadedSources["SKILL.md"] ?? skillMdServerFile?.content ?? null);
  const serverState = `${item.id}:${item.revision}:${version.id}`;
  const emptyExplicitFolders = explicitFolders.filter(
    (folder) =>
      !workingFiles.some((file) => file.path.startsWith(`${folder}/`)),
  );

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
    if (
      skillMdServerFile?.preview_status !== "ready" ||
      skillMdServerFile.content === null
    ) {
      return;
    }
    setLoadedSources((current) =>
      current["SKILL.md"] === skillMdServerFile.content
        ? current
        : { ...current, "SKILL.md": skillMdServerFile.content ?? "" },
    );
  }, [skillMdServerFile]);

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    if (!focusSecrets) return;
    if (surface !== "secrets") setSurface("secrets");
  }, [focusSecrets, surface]);

  useEffect(() => {
    if (!focusSecrets || surface !== "secrets") return;
    const frame = requestAnimationFrame(() => {
      secretsPanelRef.current?.focus({ preventScroll: true });
      secretsPanelRef.current?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
      onSecretsFocused?.();
    });
    return () => cancelAnimationFrame(frame);
  }, [focusSecrets, onSecretsFocused, surface]);

  useEffect(() => {
    const conflictRecovery = conflictRecoveryRef.current;
    if (
      skillVersionConflictHasLatestServerState(conflictRecovery, item, version)
    ) {
      expectedAssetVersionRef.current = item.revision;
      appliedServerStateRef.current = serverState;
      conflictRecoveryRef.current = null;
      setLocalError(
        "已加载最新资产修订并保留本地修改。再次保存会基于当前版本创建新的候选版本，请确认后重试。",
      );
      return;
    }
    if (
      conflictRecovery &&
      (conflictRecovery.assetId !== item.id ||
        conflictRecovery.sourceVersionId !== version.id)
    ) {
      conflictRecoveryRef.current = null;
    }

    const pendingSavedVersion = pendingSavedVersionRef.current;
    if (pendingSavedVersion?.assetId === item.id) {
      if (skillVersionSaveIsPending(pendingSavedVersion, item)) return;
      pendingSavedVersionRef.current = null;
    } else if (pendingSavedVersion) {
      pendingSavedVersionRef.current = null;
    }
    if (serverState === appliedServerStateRef.current || dirty) return;

    expectedAssetVersionRef.current = item.revision;
    appliedServerStateRef.current = serverState;
  }, [dirty, item, item.id, item.revision, serverState, version, version.id]);

  useEffect(() => {
    if (canAuthor || !editing) return;
    setLocalError(
      dirty
        ? "Skill 状态或编辑权限已发生变化，本地修改仍保留在当前页面。恢复权限后可继续保存，离开前也可以先复制内容。"
        : "Skill 状态或编辑权限已发生变化，当前页面已切换为只读。",
    );
  }, [canAuthor, dirty, editing]);

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
    setSurface("files");
    setSelection({ kind: "file", path });
    expandFolder(directoryPath(path));
    setDisplayMode("source");
    setLocalError(null);
  }

  const handleSecretValidityChange = useCallback(
    (valid: boolean) => {
      setSecretDeclarationValid(valid);
      onActivationValidityChange(valid);
    },
    [onActivationValidityChange],
  );

  useEffect(() => {
    handleSecretValidityChange(false);
  }, [handleSecretValidityChange, version.id]);

  function applySkillMdContent(content: string) {
    try {
      setSecretDeclarationValid(false);
      replaceChanges(
        editSkillFile(
          changesRef.current,
          version.file_views,
          "SKILL.md",
          content,
        ),
      );
      setLocalError(null);
    } catch (error) {
      setLocalError(pathErrorMessage(error));
    }
  }

  function openSkillMdSource() {
    setSurface("files");
    setSelection({ kind: "file", path: "SKILL.md" });
    setDisplayMode("source");
  }

  function selectFolder(path: string) {
    setSelection({ kind: "folder", path });
    setDisplayMode("source");
    setLocalError(null);
  }

  function toggleFolder(path: string) {
    setExpandedFolders((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  function expandFolder(path: string) {
    setExpandedFolders(
      (current) => new Set([...current, ...foldersThrough(path)]),
    );
  }

  function beginPathDialog(mode: PathDialogMode) {
    if (fork.isPending) return;
    setPathDialog(mode);
    if (mode === "rename") {
      setPathInput(selectedPath);
    } else {
      setFileNameInput("");
      setTargetFolder(defaultSkillFileFolder(selection));
      setInlineFolderOpen(false);
      setInlineFolderName("");
    }
    setPathError(null);
  }

  function commitPathDialog() {
    if (fork.isPending) return;
    try {
      if (pathDialog === "add") {
        const nextPath = joinSkillFilePath(targetFolder, fileNameInput);
        if (folderPaths.includes(nextPath)) {
          throw new Error("Skill file path already exists");
        }
        const next = addSkillFile(
          changes,
          version.file_views,
          nextPath,
          "",
          mediaTypeForPath(nextPath),
        );
        replaceChanges(next);
        setSelection({ kind: "file", path: nextPath });
        expandFolder(targetFolder);
      } else if (
        pathDialog === "rename" &&
        selectedFile &&
        sourceContent !== null
      ) {
        const previousFolder = directoryPath(selectedFile.path);
        if (folderPaths.includes(pathInput)) {
          throw new Error("Skill file path already exists");
        }
        const next = renameSkillFile(
          changes,
          version.file_views,
          selectedFile.path,
          pathInput,
          sourceContent,
          mediaTypeForPath(pathInput),
        );
        replaceChanges(next);
        if (previousFolder && previousFolder !== directoryPath(pathInput)) {
          setExplicitFolders((current) =>
            current.includes(previousFolder)
              ? current
              : [...current, previousFolder],
          );
        }
        setSelection({ kind: "file", path: pathInput });
        expandFolder(directoryPath(pathInput));
      }
      setPathDialog(null);
      setPathError(null);
      setDisplayMode("source");
    } catch (error) {
      setPathError(pathErrorMessage(error));
    }
  }

  function createFolder({
    parent,
    name,
    select,
  }: {
    parent: string;
    name: string;
    select: boolean;
  }) {
    const normalizedName = name.trim();
    const next = addSkillFolder(
      explicitFolders,
      workingFiles,
      parent,
      normalizedName,
    );
    const path = folderPathWithName(parent, normalizedName);
    setExplicitFolders(next);
    expandFolder(path);
    if (select) selectFolder(path);
    return path;
  }

  function commitFolderDialog() {
    if (fork.isPending) return;
    try {
      createFolder({
        parent: folderParent,
        name: folderNameInput,
        select: true,
      });
      setFolderDialogOpen(false);
      setFolderError(null);
      setFolderNameInput("");
    } catch (error) {
      setFolderError(pathErrorMessage(error));
    }
  }

  function commitInlineFolder() {
    if (fork.isPending) return;
    try {
      const path = createFolder({
        parent: targetFolder,
        name: inlineFolderName,
        select: false,
      });
      setTargetFolder(path);
      setInlineFolderOpen(false);
      setInlineFolderName("");
      setPathError(null);
    } catch (error) {
      setPathError(pathErrorMessage(error));
    }
  }

  function beginFolderDialog() {
    if (fork.isPending) return;
    setFolderParent(defaultSkillFileFolder(selection));
    setFolderNameInput("");
    setFolderError(null);
    setFolderDialogOpen(true);
  }

  function confirmDelete() {
    if (!deletePath || fork.isPending) return;
    try {
      const previousFolder = directoryPath(deletePath);
      const next = deleteSkillFile(changes, version.file_views, deletePath);
      replaceChanges(next);
      if (previousFolder) {
        setExplicitFolders((current) =>
          current.includes(previousFolder)
            ? current
            : [...current, previousFolder],
        );
      }
      const nextFiles = listWorkingSkillFiles(version.file_views, next);
      const nextPath =
        nextFiles.find((file) => file.path === "SKILL.md")?.path ??
        nextFiles[0]?.path ??
        "";
      setSelection(nextPath ? { kind: "file", path: nextPath } : null);
      setDeletePath(null);
      setDisplayMode("source");
    } catch (error) {
      setDeletePath(null);
      setLocalError(pathErrorMessage(error));
    }
  }

  function discardChanges() {
    if (fork.isPending) return;
    conflictRecoveryRef.current = null;
    replaceChanges([]);
    setExplicitFolders([]);
    setLoadedSources({});
    onEditingChange(false);
    setDiscardOpen(false);
    setLocalError(null);
    setSelection(initialPath ? { kind: "file", path: initialPath } : null);
    setDisplayMode("source");
  }

  async function saveCandidateVersion() {
    if (fork.isPending) return;
    setLocalError(null);
    const expectedAssetVersion = expectedAssetVersionRef.current;
    const submittedChanges = changes.map((change) => ({ ...change }));
    try {
      const result = await fork.mutateAsync({
        assetId: item.id,
        sourceVersionId: version.id,
        input: {
          expected_revision: expectedAssetVersion,
          expected_source_payload_checksum: version.payload_checksum,
          changes,
        },
      });
      if (!("skill_id" in result.data)) {
        setLocalError("服务返回了无效的 Skill 版本。请重试。");
        return;
      }
      conflictRecoveryRef.current = null;
      const savedAssetVersion = expectedAssetVersion + 1;
      expectedAssetVersionRef.current = savedAssetVersion;
      pendingSavedVersionRef.current = {
        assetId: item.id,
        assetVersion: savedAssetVersion,
      };
      if (
        skillVersionDraftMatchesSubmittedChanges(
          submittedChanges,
          changesRef.current,
        )
      ) {
        setSelection(initialPath ? { kind: "file", path: initialPath } : null);
        replaceChanges([]);
        setExplicitFolders([]);
        setLoadedSources({});
        onEditingChange(false);
        onDirtyChange(false);
        notifySkillCandidateVersionCreated(onVersionCreated, result.data);
      } else {
        setLocalError(
          "提交时的修改已保存为新版本；保存期间产生的后续修改仍保留在当前编辑副本中，请再次保存或手动放弃。",
        );
        onDirtyChange(true);
      }
    } catch (error) {
      if (
        error instanceof SharedAssetApiError &&
        error.code === "ASSET_CONFLICT"
      ) {
        conflictRecoveryRef.current = {
          assetId: item.id,
          assetVersion: expectedAssetVersion,
          sourceVersionId: version.id,
        };
        setLocalError(
          "资产已在其他窗口发生变化。本地修改仍然保留，正在加载最新资产修订…",
        );
        void invalidateProjectSkillConflictQueries(
          queryClient,
          accountId,
          projectId,
          item.id,
        ).catch(() => undefined);
      } else {
        setLocalError(adminAssetErrorMessage(error));
      }
    }
  }

  const secretConfigurationContent = isEditing ? (
    <section className="border-border/70 space-y-2 rounded-xl border p-4">
      <div className="flex items-center gap-2">
        <KeyRoundIcon aria-hidden className="size-4" />
        <h3 className="text-sm font-semibold">2. 版本秘密配置</h3>
      </div>
    </section>
  ) : (
    secretConfiguration
  );

  return (
    <section className="space-y-4" aria-label="Skill 版本内容">
      <div>
        <h3 className="text-sm font-semibold">版本内容</h3>
        <p className="text-muted-foreground mt-1 max-w-2xl text-xs leading-5">
          {skillVersionSnapshotCopy(
            item.scope,
            version.relation,
            version.version_number,
          )}
        </p>
      </div>

      <div
        className="bg-muted flex w-fit rounded-lg p-1"
        role="tablist"
        aria-label={secretCopy.workbenchAria}
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
          {secretCopy.filesTab}
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
          {secretCopy.secretsTab}
        </Button>
      </div>

      {isEditing && (
        <section
          aria-label="版本编辑状态"
          className="border-primary/20 bg-primary/5 flex flex-col gap-3 rounded-xl border px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
        >
          <div className="min-w-0">
            <p className="text-sm font-medium">
              正在基于版本 {version.version_number} 编辑副本
            </p>
            <p className="text-muted-foreground mt-1 text-xs">
              {dirty ? `已有 ${changes.length} 项未保存修改` : "尚未修改文件"}
            </p>
            {emptyExplicitFolders.length > 0 && (
              <p className="text-muted-foreground mt-1 text-xs">
                {emptyExplicitFolders.length}{" "}
                个空文件夹仅在当前编辑中显示；请在其中创建文件后再保存版本。
              </p>
            )}
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={fork.isPending}
              onClick={() => (dirty ? setDiscardOpen(true) : discardChanges())}
            >
              放弃修改
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={
                !dirty ||
                fork.isPending ||
                skillMdContent === null ||
                !secretDeclarationValid
              }
              title={
                dirty && !secretDeclarationValid
                  ? secretCopy.saveBlocked
                  : undefined
              }
              onClick={() => void saveCandidateVersion()}
            >
              {fork.isPending ? (
                <Loader2Icon aria-hidden className="size-4 animate-spin" />
              ) : (
                <SaveIcon aria-hidden className="size-4" />
              )}
              {fork.isPending ? "保存中…" : "保存为新版本"}
            </Button>
          </div>
        </section>
      )}

      {(localError ?? fork.error) && (
        <p role="alert" className="text-destructive text-sm">
          {localError ?? adminAssetErrorMessage(fork.error)}
        </p>
      )}

      <div
        id={filesPanelId}
        hidden={surface !== "files"}
        role="tabpanel"
        aria-labelledby={filesTabId}
      >
        {workingFiles.length === 0 && folderPaths.length === 0 ? (
          <p className="text-muted-foreground rounded-xl border border-dashed p-5 text-sm">
            这个版本没有可显示的文件。
          </p>
        ) : (
          <div className="border-border/70 overflow-hidden rounded-2xl border md:grid md:grid-cols-[260px_minmax(0,1fr)]">
            <div className="bg-muted/20 border-border/70 border-b p-3 md:border-r md:border-b-0">
              <div className="mb-3 flex items-center justify-between gap-2 px-1">
                <p className="text-sm font-semibold">
                  文件{" "}
                  <span className="text-muted-foreground font-normal">
                    · {workingFiles.length}
                  </span>
                </p>
              </div>
              {isEditing && (
                <div className="mb-3 grid grid-cols-2 gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="bg-background min-w-0 px-2"
                    disabled={fork.isPending}
                    onClick={() => beginPathDialog("add")}
                  >
                    <FilePlus2Icon aria-hidden className="size-4" />
                    新建文件
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="bg-background min-w-0 px-2"
                    disabled={fork.isPending}
                    onClick={beginFolderDialog}
                  >
                    <FolderPlusIcon aria-hidden className="size-4" />
                    新建文件夹
                  </Button>
                </div>
              )}
              <SkillFileTree
                nodes={fileTree}
                selection={selection}
                expandedFolders={expandedFolders}
                onSelectFile={selectFile}
                onSelectFolder={selectFolder}
                onToggleFolder={toggleFolder}
              />
              {isEditing && emptyExplicitFolders.length > 0 && (
                <p
                  role="status"
                  className="text-muted-foreground mt-3 border-t px-1 pt-3 text-[11px] leading-4"
                >
                  空文件夹不会写入版本快照。
                </p>
              )}
            </div>

            <div className="min-w-0">
              {selectedFile && (
                <>
                  <div className="border-border/70 flex flex-col gap-3 border-b px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0">
                      <p className="truncate font-mono text-sm font-semibold">
                        {basename(selectedFile.path)}
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
                      {isEditing && selectedFile.path !== "SKILL.md" && (
                        <>
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            disabled={sourceContent === null || fork.isPending}
                            onClick={() => beginPathDialog("rename")}
                          >
                            重命名
                          </Button>
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            disabled={fork.isPending}
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
                    <div
                      className="space-y-3 p-4"
                      aria-label="正在加载文件内容"
                    >
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
                        <SafeStreamdown>
                          {markdownPreviewContent(sourceContent)}
                        </SafeStreamdown>
                      </div>
                    ) : isEditing ? (
                      <Textarea
                        aria-label={`编辑 ${selectedFile.path}`}
                        value={sourceContent}
                        disabled={fork.isPending}
                        spellCheck={false}
                        className="min-h-[50vh] resize-y rounded-none border-0 p-5 font-mono text-sm leading-6 shadow-none focus-visible:ring-0 md:min-h-[520px]"
                        onChange={(event) => {
                          if (fork.isPending) return;
                          try {
                            if (selectedFile.path === "SKILL.md") {
                              setSecretDeclarationValid(false);
                            }
                            replaceChanges(
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
              {!selectedFile && (
                <div className="text-muted-foreground flex min-h-[50vh] items-center justify-center p-6 text-center text-sm md:min-h-[520px]">
                  选择一个文件查看内容。
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      <div
        ref={secretsPanelRef}
        id={secretsPanelId}
        hidden={surface !== "secrets"}
        role="tabpanel"
        aria-labelledby={secretsTabId}
        tabIndex={-1}
        className="scroll-mt-5 space-y-5 focus:outline-none"
      >
        {skillMdContent !== null ? (
          <SkillSecretDeclarationsEditor
            projectId={projectId}
            content={skillMdContent}
            editable={isEditing}
            canBeginEdit={canAuthor && !isEditing && !secretConfigurationDirty}
            showEmptyDescription={false}
            readOnlyReason={
              !canAuthor
                ? "当前版本只读，你没有创建新版本的权限。"
                : secretConfigurationDirty
                  ? "请先保存或撤销当前秘密配置修改，再创建新版本。"
                  : undefined
            }
            disabled={fork.isPending}
            beforeAdvancedSettings={secretConfigurationContent}
            onContentChange={applySkillMdContent}
            onBeginEdit={() =>
              beginSkillSecretEditing(setSurface, onEditingChange)
            }
            onOpenSource={openSkillMdSource}
            onValidityChange={handleSecretValidityChange}
          />
        ) : skillMdSource.error ||
          (selectedPath === "SKILL.md" && source.error) ? (
          <div className="border-destructive/30 space-y-3 rounded-lg border p-4">
            <p role="alert" className="text-destructive text-sm">
              {secretCopy.loadSourceFailed}
            </p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() =>
                void (selectedPath === "SKILL.md"
                  ? source.refetch()
                  : skillMdSource.refetch())
              }
            >
              {secretCopy.retry}
            </Button>
          </div>
        ) : (
          <div
            role="status"
            className="text-muted-foreground flex items-center gap-2 rounded-lg border border-dashed p-4 text-sm"
          >
            <Loader2Icon aria-hidden className="size-4 animate-spin" />
            {secretCopy.loadSource}
          </div>
        )}
      </div>

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
              {pathDialog === "rename"
                ? "使用项目内的相对 POSIX 路径。"
                : "填写文件名并选择所在文件夹；创建后会立即打开这个文件。"}
            </DialogDescription>
          </DialogHeader>
          {pathDialog === "rename" ? (
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
          ) : (
            <div className="space-y-4">
              <label className="space-y-2">
                <span className="text-sm font-medium">文件名</span>
                <Input
                  autoFocus
                  value={fileNameInput}
                  placeholder="guide.md"
                  onChange={(event) => {
                    setFileNameInput(event.target.value);
                    setPathError(null);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !inlineFolderOpen) {
                      event.preventDefault();
                      commitPathDialog();
                    }
                  }}
                />
              </label>
              <label className="space-y-2">
                <span className="text-sm font-medium">所在文件夹</span>
                <select
                  aria-label="所在文件夹"
                  value={targetFolder}
                  className="border-input bg-background h-10 w-full rounded-md border px-3 text-sm"
                  onChange={(event) => {
                    setTargetFolder(event.target.value);
                    setPathError(null);
                  }}
                >
                  <option value="">根目录</option>
                  {folderPaths.map((folder) => (
                    <option key={folder} value={folder}>
                      {"　".repeat(Math.max(0, folder.split("/").length - 1))}
                      {folder}
                    </option>
                  ))}
                </select>
              </label>
              <div className="bg-muted/35 rounded-lg px-3 py-2 text-xs">
                <span className="text-muted-foreground">完整路径：</span>
                <span className="font-mono break-all">
                  {fileNameInput
                    ? folderPathWithName(targetFolder, fileNameInput)
                    : "等待输入文件名"}
                </span>
              </div>
              {!inlineFolderOpen ? (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setInlineFolderOpen(true);
                    setInlineFolderName("");
                    setPathError(null);
                  }}
                >
                  <FolderPlusIcon aria-hidden className="size-4" />
                  在所选目录中新建文件夹
                </Button>
              ) : (
                <div className="border-border/70 space-y-3 rounded-lg border p-3">
                  <div>
                    <p className="text-sm font-medium">新建子文件夹</p>
                    <p className="text-muted-foreground mt-1 text-xs">
                      父级：{targetFolder || "根目录"}
                    </p>
                  </div>
                  <Input
                    aria-label="新文件夹名称"
                    value={inlineFolderName}
                    placeholder="references"
                    onChange={(event) => {
                      setInlineFolderName(event.target.value);
                      setPathError(null);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        commitInlineFolder();
                      }
                    }}
                  />
                  <div className="flex justify-end gap-2">
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() => setInlineFolderOpen(false)}
                    >
                      取消
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      onClick={commitInlineFolder}
                    >
                      创建并选中
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
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
            <Button
              type="button"
              disabled={fork.isPending}
              onClick={commitPathDialog}
            >
              {pathDialog === "rename" ? "确认重命名" : "创建文件"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={folderDialogOpen}
        onOpenChange={(open) => {
          setFolderDialogOpen(open);
          if (!open) setFolderError(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>新建文件夹</DialogTitle>
            <DialogDescription>
              选择父级并输入文件夹名称。新目录会立即显示在文件树中。
            </DialogDescription>
          </DialogHeader>
          <label className="space-y-2">
            <span className="text-sm font-medium">文件夹名称</span>
            <Input
              autoFocus
              value={folderNameInput}
              placeholder="references"
              onChange={(event) => {
                setFolderNameInput(event.target.value);
                setFolderError(null);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  commitFolderDialog();
                }
              }}
            />
          </label>
          <label className="space-y-2">
            <span className="text-sm font-medium">父级文件夹</span>
            <select
              aria-label="父级文件夹"
              value={folderParent}
              className="border-input bg-background h-10 w-full rounded-md border px-3 text-sm"
              onChange={(event) => {
                setFolderParent(event.target.value);
                setFolderError(null);
              }}
            >
              <option value="">根目录</option>
              {folderPaths.map((folder) => (
                <option key={folder} value={folder}>
                  {"　".repeat(Math.max(0, folder.split("/").length - 1))}
                  {folder}
                </option>
              ))}
            </select>
          </label>
          <p className="text-muted-foreground text-xs leading-5">
            版本快照只保存文件。空文件夹会在当前编辑中显示，但必须包含文件后才能随新版本保留。
          </p>
          {folderError && (
            <p role="alert" className="text-destructive text-sm">
              {folderError}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setFolderDialogOpen(false)}
            >
              取消
            </Button>
            <Button
              type="button"
              disabled={fork.isPending}
              onClick={commitFolderDialog}
            >
              创建文件夹
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
              {deletePath} 会从新候选版本中移除，原版本的文件不会受影响。
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
            <Button
              type="button"
              variant="destructive"
              disabled={fork.isPending}
              onClick={confirmDelete}
            >
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
              disabled={fork.isPending}
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
