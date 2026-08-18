import type { Capability } from "@/core/projects/types";

import {
  AGENT_BUILDER_SLUG_MAX_LENGTH,
  AGENT_BUILDER_SLUG_MIN_LENGTH,
  AGENT_BUILDER_SLUG_PATTERN,
  type AgentBuilderBlueprint,
  type AgentBuilderSession,
} from "./types";

export type AgentBuilderDisplayState =
  | "interviewing"
  | "generating"
  | "awaiting_clarification"
  | "proposal_ready"
  | "committing"
  | "completed"
  | "failed"
  | "cancelled";

export function normalizeAgentBuilderSlug(value: string): string {
  return value
    .trim()
    .toLocaleLowerCase()
    .replace(/[^a-z0-9]+/gu, "-")
    .replace(/^-+|-+$/gu, "");
}

export type AgentBuilderSlugErrorCode = "too-short" | "too-long" | "invalid";

export function agentBuilderSlugErrorCode(
  value: string,
): AgentBuilderSlugErrorCode | null {
  if (value.length < AGENT_BUILDER_SLUG_MIN_LENGTH) return "too-short";
  if (value.length > AGENT_BUILDER_SLUG_MAX_LENGTH) return "too-long";
  if (!AGENT_BUILDER_SLUG_PATTERN.test(value)) return "invalid";
  return null;
}

export function agentBuilderSlugError(value: string): string | null {
  switch (agentBuilderSlugErrorCode(value)) {
    case "too-short":
      return "名称至少需要 3 个字符";
    case "too-long":
      return "名称不能超过 63 个字符";
    case "invalid":
      return "仅支持小写字母、数字和单个连字符";
    default:
      return null;
  }
}

export function agentBuilderCanAuthor(
  capabilities: readonly Capability[],
): boolean {
  return capabilities.includes("shared_assets.edit");
}

export function agentBuilderCanRead(
  capabilities: readonly Capability[],
): boolean {
  return capabilities.includes("shared_assets.read");
}

export function agentBuilderCancelActionDisabled(
  session: Pick<AgentBuilderSession, "status"> | null | undefined,
  state: {
    generationPending: boolean;
    commitPending: boolean;
    cancelPending: boolean;
    cancelPreparing: boolean;
  },
): boolean {
  return (
    !session ||
    state.commitPending ||
    state.cancelPending ||
    state.cancelPreparing ||
    session.status === "committing" ||
    session.status === "completed" ||
    session.status === "cancelled"
  );
}

export async function prepareAgentBuilderCancelSession(
  session: AgentBuilderSession,
  shouldRefresh: boolean,
  refetch: () => Promise<AgentBuilderSession | undefined>,
): Promise<AgentBuilderSession> {
  if (!shouldRefresh) return session;
  try {
    const refreshed = await refetch();
    return refreshed && refreshed.revision >= session.revision
      ? refreshed
      : session;
  } catch {
    // A stale CAS is recovered by the cancel mutation's conflict handler.
    return session;
  }
}

export function agentBuilderBlueprintValidationError(
  blueprint: AgentBuilderBlueprint,
): string | null {
  const issue = agentBuilderBlueprintValidationIssue(blueprint);
  switch (issue?.code) {
    case "description-required":
      return "Agent 简介不能为空";
    case "model-required":
      return "Agent 模型不能为空";
    case "tool-group-required":
      return "Agent 至少需要一个有效工具组";
    case "document-required":
      return `${issue.document} 不能为空，请补充后再保存`;
    default:
      return null;
  }
}

export type AgentBuilderBlueprintValidationIssue =
  | { code: "description-required" }
  | { code: "model-required" }
  | { code: "tool-group-required" }
  | { code: "document-required"; document: string };

export function agentBuilderBlueprintValidationIssue(
  blueprint: AgentBuilderBlueprint,
): AgentBuilderBlueprintValidationIssue | null {
  if (blueprint.description.trim() === "") {
    return { code: "description-required" };
  }
  if (blueprint.model_ref.trim() === "") {
    return { code: "model-required" };
  }
  if (
    blueprint.tool_groups.length === 0 ||
    blueprint.tool_groups.some((group) => group.trim() === "")
  ) {
    return { code: "tool-group-required" };
  }
  const documents = [
    ["AGENTS.md", blueprint.agents_instructions],
    ["SOUL.md", blueprint.soul],
    ["IDENTITY.md", blueprint.identity],
    ["USER.md", blueprint.user_context],
  ] as const;
  const emptyDocument = documents.find(([, content]) => content.trim() === "");
  return emptyDocument
    ? { code: "document-required", document: emptyDocument[0] }
    : null;
}

export function agentBuilderDisplayState(
  session: AgentBuilderSession,
): AgentBuilderDisplayState {
  if (
    session.active_clarifications.length > 0 &&
    session.status !== "committing" &&
    session.status !== "completed"
  ) {
    return "awaiting_clarification";
  }
  return session.status;
}

export function agentBuilderComposerDisabled(
  session: AgentBuilderSession,
  mutationPending: boolean,
  localDraftLocked = false,
): boolean {
  const state = agentBuilderDisplayState(session);
  return (
    mutationPending ||
    localDraftLocked ||
    state === "generating" ||
    state === "awaiting_clarification" ||
    state === "committing" ||
    state === "completed" ||
    state === "cancelled"
  );
}

export function agentBuilderCanComplete(session: AgentBuilderSession): boolean {
  return (
    session.status === "proposal_ready" &&
    session.blueprint !== null &&
    agentBuilderBlueprintValidationError(session.blueprint) === null &&
    session.created_agent_id === null
  );
}
