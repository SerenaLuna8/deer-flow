import type { Capability } from "@/core/projects/types";

import type { SkillBuilderFile, SkillBuilderSession } from "./types";

const SKILL_SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/u;

export function skillBuilderSessionPath(
  projectSlug: string,
  sessionId: string,
): string {
  return `/projects/${encodeURIComponent(projectSlug)}/skills/new/${encodeURIComponent(sessionId)}`;
}

export function normalizeSkillBuilderSlug(value: string): string {
  return value
    .trim()
    .toLocaleLowerCase()
    .replace(/[^a-z0-9]+/gu, "-")
    .replace(/^-+|-+$/gu, "");
}

export function skillBuilderSlugError(value: string): string | null {
  if (value.length < 3) return "名称至少需要 3 个字符";
  if (value.length > 63) return "名称不能超过 63 个字符";
  if (!SKILL_SLUG_PATTERN.test(value)) {
    return "仅支持小写字母、数字和单个连字符";
  }
  return null;
}

export function skillBuilderCanAuthor(
  capabilities: readonly Capability[],
): boolean {
  return capabilities.includes("shared_assets.edit");
}

export function initialSkillBuilderFilePath(
  files: readonly Pick<SkillBuilderFile, "path">[],
): string | null {
  return files.some((file) => file.path === "SKILL.md") ? "SKILL.md" : null;
}

export function reconcileSkillBuilderFileSelection(
  currentPath: string | null,
  previousFiles: readonly Pick<SkillBuilderFile, "path">[],
  nextFiles: readonly Pick<SkillBuilderFile, "path">[],
): string | null {
  if (currentPath && nextFiles.some((file) => file.path === currentPath)) {
    return currentPath;
  }
  if (previousFiles.length === 0) {
    return initialSkillBuilderFilePath(nextFiles);
  }
  if (currentPath) return initialSkillBuilderFilePath(nextFiles);
  return null;
}

export function skillBuilderComposerDisabled(
  session: SkillBuilderSession,
  mutationPending: boolean,
  localDraftDirty = false,
): boolean {
  return (
    mutationPending ||
    localDraftDirty ||
    session.status === "generating" ||
    session.status === "awaiting_clarification" ||
    session.status === "committing" ||
    session.status === "completed" ||
    session.status === "cancelled"
  );
}

export function skillBuilderValidationCurrent(
  session: SkillBuilderSession,
): boolean {
  return Boolean(
    session.draft_checksum &&
    session.validation?.draft_checksum === session.draft_checksum,
  );
}

export function skillBuilderCanCommit(session: SkillBuilderSession): boolean {
  return (
    session.status === "validated" &&
    session.created_skill_id === null &&
    session.files.some((file) => file.path === "SKILL.md") &&
    skillBuilderValidationCurrent(session)
  );
}
