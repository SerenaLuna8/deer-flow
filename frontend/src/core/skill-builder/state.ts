import type { Capability } from "@/core/projects/types";

import {
  SKILL_BUILDER_MAX_ATTACHMENT_BYTES,
  SKILL_BUILDER_MAX_ATTACHMENTS,
  SKILL_BUILDER_MAX_ATTACHMENTS_TOTAL_BYTES,
  skillBuilderAttachmentSchema,
  type SkillBuilderAttachment,
  type SkillBuilderFile,
  type SkillBuilderRunStatus,
  type SkillBuilderSession,
} from "./types";

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

export type SkillBuilderSlugErrorCode = "too-short" | "too-long" | "invalid";

export function skillBuilderSlugErrorCode(
  value: string,
): SkillBuilderSlugErrorCode | null {
  if (value.length < 3) return "too-short";
  if (value.length > 63) return "too-long";
  if (!SKILL_SLUG_PATTERN.test(value)) return "invalid";
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
    session.target_skill_deleted ||
    Boolean(session.activeRun) ||
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
    !session.target_skill_deleted &&
    session.files.some((file) => file.path === "SKILL.md") &&
    skillBuilderValidationCurrent(session)
  );
}

export type SkillBuilderRunPresentationStatus =
  | SkillBuilderRunStatus
  | "cancelled";

export type SkillBuilderRunPresentation = {
  runId: string;
  status: SkillBuilderRunPresentationStatus;
};

/**
 * Project the latest known Builder Run without inventing stream data.
 *
 * Gateway exposes only pending/running through `activeRun`; after settlement,
 * the durable Skill Design session is the authority for the terminal outcome.
 * `trackedRunId` ensures a terminal session state is not presented as a Run
 * result unless this UI actually observed or admitted that Run.
 */
export function skillBuilderRunPresentation(
  session: SkillBuilderSession,
  trackedRunId: string | null,
): SkillBuilderRunPresentation | null {
  if (session.activeRun) {
    return {
      runId: session.activeRun.runId,
      status: session.activeRun.status,
    };
  }
  if (!trackedRunId) return null;

  if (session.status === "generating") {
    return { runId: trackedRunId, status: "running" };
  }
  if (session.status === "cancelled") {
    return { runId: trackedRunId, status: "cancelled" };
  }
  if (session.status === "failed") {
    const errorCode = session.error_code?.toUpperCase() ?? "";
    if (errorCode.includes("TIMEOUT")) {
      return { runId: trackedRunId, status: "timeout" };
    }
    if (errorCode.includes("INTERRUPTED")) {
      return { runId: trackedRunId, status: "interrupted" };
    }
    return { runId: trackedRunId, status: "error" };
  }
  if (
    session.status === "awaiting_clarification" ||
    session.status === "draft_ready" ||
    session.status === "validated" ||
    session.status === "committing" ||
    session.status === "completed"
  ) {
    return { runId: trackedRunId, status: "success" };
  }
  return null;
}

export type SkillBuilderMergeAttachmentError =
  | "too_large"
  | "invalid_name"
  | "too_many"
  | "total_too_large";

export type SkillBuilderMergeAttachmentResult =
  | { ok: true; attachments: SkillBuilderAttachment[] }
  | { ok: false; error: SkillBuilderMergeAttachmentError };

/** Merge one uploaded reference file into the composer queue, replacing a same-name entry. */
export function skillBuilderMergeAttachment(
  current: readonly SkillBuilderAttachment[],
  attachment: SkillBuilderAttachment,
): SkillBuilderMergeAttachmentResult {
  const candidate = {
    name: attachment.name.trim(),
    content: attachment.content,
  };
  const parsed = skillBuilderAttachmentSchema.safeParse(candidate);
  if (!parsed.success) {
    if (
      new TextEncoder().encode(candidate.content).byteLength >
      SKILL_BUILDER_MAX_ATTACHMENT_BYTES
    ) {
      return { ok: false, error: "too_large" };
    }
    return { ok: false, error: "invalid_name" };
  }
  const next = current.filter((item) => item.name !== parsed.data.name);
  next.push(parsed.data);
  if (next.length > SKILL_BUILDER_MAX_ATTACHMENTS) {
    return { ok: false, error: "too_many" };
  }
  const total = next.reduce(
    (sum, item) => sum + new TextEncoder().encode(item.content).byteLength,
    0,
  );
  if (total > SKILL_BUILDER_MAX_ATTACHMENTS_TOTAL_BYTES) {
    return { ok: false, error: "total_too_large" };
  }
  return { ok: true, attachments: next };
}

export type SkillBuilderFilesPanelReveal = "open" | "close" | "keep";

export function skillBuilderHasCandidateFiles(
  files: readonly unknown[],
): boolean {
  return files.length > 0;
}

export function skillBuilderFilesPanelReveal(
  previousHadFiles: boolean,
  fileCount: number,
): SkillBuilderFilesPanelReveal {
  if (fileCount <= 0) return "close";
  if (!previousHadFiles) return "open";
  return "keep";
}
