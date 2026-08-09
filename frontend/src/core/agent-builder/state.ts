import type { Capability } from "@/core/projects/types";

import type { AgentBuilderBlueprint, AgentBuilderSession } from "./types";

const AGENT_SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/u;

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

export function agentBuilderSlugError(value: string): string | null {
  if (value.length < 3) return "名称至少需要 3 个字符";
  if (value.length > 63) return "名称不能超过 63 个字符";
  if (!AGENT_SLUG_PATTERN.test(value)) {
    return "仅支持小写字母、数字和单个连字符";
  }
  return null;
}

export function agentBuilderCanAuthor(
  capabilities: readonly Capability[],
): boolean {
  return capabilities.includes("shared_assets.edit");
}

export function agentBuilderBlueprintValidationError(
  blueprint: AgentBuilderBlueprint,
): string | null {
  if (blueprint.description.trim() === "") return "Agent 简介不能为空";
  if (blueprint.model_ref.trim() === "") return "Agent 模型不能为空";
  if (
    blueprint.tool_groups.length === 0 ||
    blueprint.tool_groups.some((group) => group.trim() === "")
  ) {
    return "Agent 至少需要一个有效工具组";
  }
  const documents = [
    ["AGENTS.md", blueprint.agents_instructions],
    ["SOUL.md", blueprint.soul],
    ["IDENTITY.md", blueprint.identity],
    ["USER.md", blueprint.user_context],
  ] as const;
  const emptyDocument = documents.find(([, content]) => content.trim() === "");
  return emptyDocument ? `${emptyDocument[0]} 不能为空，请补充后再保存` : null;
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
