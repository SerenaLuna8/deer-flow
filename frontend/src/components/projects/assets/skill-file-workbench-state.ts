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
  if (
    listWorkingSkillFiles(sourceFiles, changes).some(
      (file) => file.path === target,
    )
  ) {
    throw new Error("Skill file path already exists");
  }
  return replaceChange(changes, {
    op: "create",
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
  if (
    listWorkingSkillFiles(sourceFiles, changes).some(
      (file) => file.path === target && file.path !== source,
    )
  ) {
    throw new Error("Skill file path already exists");
  }

  const current = changes.find((change) => change.path === source);
  let next = removeChange(changes, source);
  if (current?.op !== "create") {
    next = replaceChange(next, { op: "delete", path: source });
  }
  return replaceChange(next, {
    op: "create",
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
