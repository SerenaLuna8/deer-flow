import {
  skillFilePathSchema,
  type SkillFileChange,
} from "@/core/shared-assets";

export type SkillFileMetadata = {
  path: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
};

export type WorkingSkillFile = SkillFileMetadata & {
  state: "unchanged" | "modified" | "added";
};

export type SkillFileTreeSelection =
  | { kind: "file"; path: string }
  | { kind: "folder"; path: string };

export type SkillFileTreeNode =
  | {
      kind: "file";
      name: string;
      path: string;
      file: WorkingSkillFile;
    }
  | {
      kind: "folder";
      name: string;
      path: string;
      children: SkillFileTreeNode[];
    };

export function markdownPreviewContent(source: string): string {
  const normalized = source.replace(/^\uFEFF/, "");
  if (!normalized.startsWith("---\n") && !normalized.startsWith("---\r\n")) {
    return source;
  }
  const match = /^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/.exec(normalized);
  return match
    ? normalized.slice(match[0].length).replace(/^\r?\n/, "")
    : source;
}

function comparePaths(left: string, right: string): number {
  if (left === "SKILL.md") return -1;
  if (right === "SKILL.md") return 1;
  return left.localeCompare(right, "en");
}

function sortChanges(changes: SkillFileChange[]): SkillFileChange[] {
  return [...changes].sort((left, right) =>
    comparePaths(left.path, right.path),
  );
}

function replaceChange(
  changes: readonly SkillFileChange[],
  next: SkillFileChange,
): SkillFileChange[] {
  return sortChanges([
    ...changes.filter((change) => change.path !== next.path),
    next,
  ]);
}

function removeChange(
  changes: readonly SkillFileChange[],
  path: string,
): SkillFileChange[] {
  return changes.filter((change) => change.path !== path);
}

function requirePath(path: string): string {
  return skillFilePathSchema.parse(path);
}

function parentFolder(path: string): string {
  const parts = path.split("/");
  return parts.length > 1 ? parts.slice(0, -1).join("/") : "";
}

function requireFolderPath(path: string): string {
  if (path === "") return "";
  requirePath(`${path}/.directory`);
  return path;
}

function requirePathSegment(value: string, label: string): string {
  if (
    value === "" ||
    value !== value.trim() ||
    value !== value.normalize("NFC") ||
    value.includes("/") ||
    value.includes("\\") ||
    value.includes("\0") ||
    value === "." ||
    value === ".."
  ) {
    throw new Error(`${label} must be one valid path segment`);
  }
  return value;
}

export function listSkillFolderPaths(
  files: readonly SkillFileMetadata[],
  explicitFolders: readonly string[] = [],
): string[] {
  const folders = new Set<string>();
  const addWithParents = (path: string) => {
    if (!path) return;
    const parts = requireFolderPath(path).split("/");
    for (let index = 1; index <= parts.length; index += 1) {
      folders.add(parts.slice(0, index).join("/"));
    }
  };

  for (const file of files) addWithParents(parentFolder(file.path));
  for (const folder of explicitFolders) addWithParents(folder);

  return [...folders].sort((left, right) => left.localeCompare(right, "en"));
}

function compareTreeNodes(
  left: SkillFileTreeNode,
  right: SkillFileTreeNode,
): number {
  if (left.path === "SKILL.md") return -1;
  if (right.path === "SKILL.md") return 1;
  if (left.kind !== right.kind) return left.kind === "folder" ? -1 : 1;
  return left.name.localeCompare(right.name, "en");
}

export function buildSkillFileTree(
  files: readonly WorkingSkillFile[],
  explicitFolders: readonly string[] = [],
): SkillFileTreeNode[] {
  const roots: SkillFileTreeNode[] = [];
  const folders = new Map<
    string,
    Extract<SkillFileTreeNode, { kind: "folder" }>
  >();

  for (const path of listSkillFolderPaths(files, explicitFolders).sort(
    (left, right) =>
      left.split("/").length - right.split("/").length ||
      left.localeCompare(right, "en"),
  )) {
    const node: Extract<SkillFileTreeNode, { kind: "folder" }> = {
      kind: "folder",
      name: path.split("/").at(-1) ?? path,
      path,
      children: [],
    };
    folders.set(path, node);
    const parent = folders.get(parentFolder(path));
    if (parent) parent.children.push(node);
    else roots.push(node);
  }

  for (const file of files) {
    const node: SkillFileTreeNode = {
      kind: "file",
      name: file.path.split("/").at(-1) ?? file.path,
      path: file.path,
      file,
    };
    const parent = folders.get(parentFolder(file.path));
    if (parent) parent.children.push(node);
    else roots.push(node);
  }

  const sortNodes = (nodes: SkillFileTreeNode[]) => {
    nodes.sort(compareTreeNodes);
    for (const node of nodes) {
      if (node.kind === "folder") sortNodes(node.children);
    }
  };
  sortNodes(roots);
  return roots;
}

export function defaultSkillFileFolder(
  selection: SkillFileTreeSelection | null,
): string {
  if (!selection) return "";
  return selection.kind === "folder"
    ? requireFolderPath(selection.path)
    : parentFolder(requirePath(selection.path));
}

export function joinSkillFilePath(folder: string, filename: string): string {
  const parent = requireFolderPath(folder);
  const name = requirePathSegment(filename, "Filename");
  return requirePath(parent ? `${parent}/${name}` : name);
}

export function addSkillFolder(
  explicitFolders: readonly string[],
  files: readonly SkillFileMetadata[],
  parentPath: string,
  name: string,
): string[] {
  const parent = requireFolderPath(parentPath);
  const available = listSkillFolderPaths(files, explicitFolders);
  if (parent && !available.includes(parent)) {
    throw new Error("Parent folder does not exist");
  }
  const folderName = requirePathSegment(name, "Folder name");
  const target = requireFolderPath(
    parent ? `${parent}/${folderName}` : folderName,
  );
  if (
    available.includes(target) ||
    files.some((file) => file.path === target)
  ) {
    throw new Error("Skill folder path already exists");
  }
  return [...explicitFolders, target].sort((left, right) =>
    left.localeCompare(right, "en"),
  );
}

export function listWorkingSkillFiles(
  sourceFiles: readonly SkillFileMetadata[],
  changes: readonly SkillFileChange[],
): WorkingSkillFile[] {
  const files = new Map<string, WorkingSkillFile>();
  for (const file of sourceFiles) {
    files.set(file.path, { ...file, state: "unchanged" });
  }
  for (const change of changes) {
    if (change.op === "delete") {
      files.delete(change.path);
      continue;
    }
    const current = files.get(change.path);
    files.set(change.path, {
      path: change.path,
      media_type: change.media_type,
      size_bytes: new TextEncoder().encode(change.content).byteLength,
      sha256: current?.sha256 ?? "",
      state: change.op === "create" ? "added" : "modified",
    });
  }
  return [...files.values()].sort((left, right) =>
    comparePaths(left.path, right.path),
  );
}

export function editSkillFile(
  changes: readonly SkillFileChange[],
  sourceFiles: readonly SkillFileMetadata[],
  path: string,
  content: string,
): SkillFileChange[] {
  const target = requirePath(path);
  const current = changes.find((change) => change.path === target);
  if (current?.op === "delete")
    throw new Error("Deleted files cannot be edited");
  if (current?.op === "create") {
    return replaceChange(changes, { ...current, content });
  }
  const source = sourceFiles.find((file) => file.path === target);
  if (!source) throw new Error("Skill file does not exist");
  return replaceChange(changes, {
    op: "replace",
    path: target,
    content,
    media_type: source.media_type,
  });
}

export function addSkillFile(
  changes: readonly SkillFileChange[],
  sourceFiles: readonly SkillFileMetadata[],
  path: string,
  content: string,
  mediaType: string,
): SkillFileChange[] {
  const target = requirePath(path);
  const workingFiles = listWorkingSkillFiles(sourceFiles, changes);
  if (
    workingFiles.some((file) => file.path === target) ||
    listSkillFolderPaths(workingFiles).includes(target)
  ) {
    throw new Error("Skill file path already exists");
  }
  const op = sourceFiles.some((file) => file.path === target)
    ? "replace"
    : "create";
  return replaceChange(changes, {
    op,
    path: target,
    content,
    media_type: mediaType,
  });
}

export function renameSkillFile(
  changes: readonly SkillFileChange[],
  sourceFiles: readonly SkillFileMetadata[],
  sourcePath: string,
  targetPath: string,
  content: string,
  mediaType: string,
): SkillFileChange[] {
  const source = requirePath(sourcePath);
  const target = requirePath(targetPath);
  if (source === "SKILL.md") throw new Error("SKILL.md cannot be renamed");
  if (source === target) return [...changes];
  const workingFiles = listWorkingSkillFiles(sourceFiles, changes);
  if (
    workingFiles.some((file) => file.path === target && file.path !== source) ||
    listSkillFolderPaths(workingFiles).includes(target)
  ) {
    throw new Error("Skill file path already exists");
  }

  const current = changes.find((change) => change.path === source);
  let next = removeChange(changes, source);
  if (current?.op !== "create") {
    next = replaceChange(next, { op: "delete", path: source });
  }
  const targetOp = sourceFiles.some((file) => file.path === target)
    ? "replace"
    : "create";
  return replaceChange(next, {
    op: targetOp,
    path: target,
    content,
    media_type: mediaType,
  });
}

export function deleteSkillFile(
  changes: readonly SkillFileChange[],
  sourceFiles: readonly SkillFileMetadata[],
  path: string,
): SkillFileChange[] {
  const target = requirePath(path);
  if (target === "SKILL.md") throw new Error("SKILL.md cannot be deleted");
  const current = changes.find((change) => change.path === target);
  if (current?.op === "create") return removeChange(changes, target);
  if (!sourceFiles.some((file) => file.path === target)) {
    throw new Error("Skill file does not exist");
  }
  return replaceChange(removeChange(changes, target), {
    op: "delete",
    path: target,
  });
}
