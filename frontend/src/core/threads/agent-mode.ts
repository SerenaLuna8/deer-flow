export const AGENT_MODES = ["flash", "thinking", "pro", "ultra"] as const;

export type AgentMode = (typeof AGENT_MODES)[number];
export type ReasoningEffort = "minimal" | "low" | "medium" | "high";

const REASONING_EFFORT_BY_MODE: Record<AgentMode, ReasoningEffort> = {
  flash: "minimal",
  thinking: "low",
  pro: "medium",
  ultra: "high",
};

export function isAgentMode(mode: unknown): mode is AgentMode {
  return AGENT_MODES.some((candidate) => candidate === mode);
}

export function resolveAgentMode(
  mode: unknown,
  supportsThinking: boolean,
): AgentMode {
  const validMode = isAgentMode(mode) ? mode : undefined;
  if (!supportsThinking && validMode !== "flash") {
    return "flash";
  }
  return validMode ?? (supportsThinking ? "pro" : "flash");
}

export function reasoningEffortForMode(mode: AgentMode): ReasoningEffort {
  return REASONING_EFFORT_BY_MODE[mode];
}

export function getAgentModeRuntimeContext(mode: unknown) {
  const resolvedMode = resolveAgentMode(mode, true);
  return {
    thinking_enabled: resolvedMode !== "flash",
    is_plan_mode: resolvedMode === "pro" || resolvedMode === "ultra",
    subagent_enabled: resolvedMode === "ultra",
    reasoning_effort: reasoningEffortForMode(resolvedMode),
  };
}
