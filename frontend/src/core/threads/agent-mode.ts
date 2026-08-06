import type {
  ReasoningEffort,
  RunExecutionProfile,
  RunExecutionProfileRequest,
} from "@/core/private-work/execution-profile";

export const AGENT_MODES = ["flash", "thinking", "pro", "ultra"] as const;

export type AgentMode = (typeof AGENT_MODES)[number];

const REASONING_EFFORT_BY_MODE: Record<AgentMode, ReasoningEffort> = {
  flash: "none",
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
  supportsReasoningEffort: boolean = supportsThinking,
): AgentMode {
  const validMode = isAgentMode(mode) ? mode : undefined;
  if (!supportsThinking) {
    return "flash";
  }
  if (
    !supportsReasoningEffort &&
    (validMode === "pro" || validMode === "ultra")
  ) {
    return "thinking";
  }
  return validMode ?? (supportsReasoningEffort ? "pro" : "thinking");
}

export function reasoningEffortForMode(mode: AgentMode): ReasoningEffort {
  return REASONING_EFFORT_BY_MODE[mode];
}

/** Map the Gateway-frozen effective profile back to the read-only UI label. */
export function agentModeForRunExecutionProfile(
  profile: RunExecutionProfile,
): AgentMode {
  if (!profile.thinking_enabled || profile.reasoning_effort === "none") {
    return "flash";
  }
  if (profile.reasoning_effort === "high") {
    return "ultra";
  }
  if (profile.reasoning_effort === "medium") {
    return "pro";
  }
  return "thinking";
}

export function getAgentModeExecutionProfile(
  mode: unknown,
  supportsThinking: boolean,
  supportsReasoningEffort: boolean,
): Pick<RunExecutionProfileRequest, "thinking_enabled" | "reasoning_effort"> {
  const resolvedMode = resolveAgentMode(
    mode,
    supportsThinking,
    supportsReasoningEffort,
  );
  return {
    thinking_enabled: resolvedMode !== "flash",
    reasoning_effort: supportsReasoningEffort
      ? reasoningEffortForMode(resolvedMode)
      : null,
  };
}

export type ExecutionModelCapabilities = {
  name: string;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
};

export type AgentExecutionModelSelection<
  Model extends { name: string; is_default?: boolean } =
    ExecutionModelCapabilities,
> = {
  model: Model | undefined;
  modelName: string | undefined;
  modelSelectionLocked: boolean;
};

export type AgentExecutionAvailability = "loading" | "ready" | "unavailable";

export function resolveAgentExecutionAvailability({
  required,
  agentModelRef,
  agentModelLoading,
  agentModelError,
  models,
  modelsLoading,
  modelsError,
}: {
  required: boolean;
  agentModelRef: string | null | undefined;
  agentModelLoading: boolean;
  agentModelError: unknown;
  models: readonly { name: string }[];
  modelsLoading: boolean;
  modelsError: unknown;
}): AgentExecutionAvailability {
  if (!required) return "ready";
  if (agentModelLoading) return "loading";
  if (
    agentModelError ||
    typeof agentModelRef !== "string" ||
    agentModelRef.length === 0
  ) {
    return "unavailable";
  }
  if (modelsLoading) return "loading";
  if (modelsError || models.length === 0) return "unavailable";
  if (agentModelRef === "default") return "ready";
  return models.some((model) => model.name === agentModelRef)
    ? "ready"
    : "unavailable";
}

export function exactAgentModelName(
  agentModelRef: string | null | undefined,
): string | null {
  return typeof agentModelRef === "string" && agentModelRef !== "default"
    ? agentModelRef
    : null;
}

/**
 * Resolve the model shown by the composer and used for capability projection.
 * An exact Agent owns this choice; a default-bound Agent keeps the user's
 * explicit thread/global preference. The exact reference remains visible even
 * if the active model catalog has not caught up yet.
 */
export function resolveAgentExecutionModelSelection<
  Model extends { name: string; is_default?: boolean },
>(
  models: readonly Model[],
  selectedModelName: string | null | undefined,
  agentModelRef?: string | null,
  modelSelectionExplicit = true,
): AgentExecutionModelSelection<Model> {
  const exactModelName = exactAgentModelName(agentModelRef);
  if (exactModelName) {
    return {
      model: models.find((model) => model.name === exactModelName),
      modelName: exactModelName,
      modelSelectionLocked: true,
    };
  }
  const model =
    (modelSelectionExplicit
      ? models.find((candidate) => candidate.name === selectedModelName)
      : undefined) ??
    models.find((candidate) => candidate.is_default === true) ??
    models[0];
  return {
    model,
    modelName: model?.name,
    modelSelectionLocked: false,
  };
}

/**
 * Keep model selection authoritative only when the user explicitly selected
 * it. Once the execution model is resolved, always send a complete mode
 * profile: an explicit mode uses the user's choice, while a missing explicit
 * choice uses that model's capability-aware default. A persisted explicit mode
 * remains deterministic while the model catalog is still loading; Gateway
 * admission performs the final capability check.
 */
export function buildRunExecutionProfileRequest({
  mode,
  modeSelectionExplicit,
  modelName,
  modelSelectionExplicit,
  agentModelRef,
  model,
}: {
  mode: unknown;
  modeSelectionExplicit: boolean;
  modelName: string | null | undefined;
  modelSelectionExplicit: boolean;
  /**
   * `default` allows a user preference, an exact ref locks model selection,
   * and `null` means the Thread Agent binding is not safely resolved yet.
   * `undefined` retains compatibility for non-project callers without this
   * metadata.
   */
  agentModelRef?: string | null;
  model: ExecutionModelCapabilities | undefined;
}): RunExecutionProfileRequest {
  const modelSelectionLocked = exactAgentModelName(agentModelRef) !== null;
  const modelSelectionResolved = agentModelRef !== null;
  const requestedModelName =
    modelSelectionResolved &&
    !modelSelectionLocked &&
    modelSelectionExplicit &&
    modelName
      ? modelName
      : null;
  if (
    (modeSelectionExplicit && !isAgentMode(mode)) ||
    (!modeSelectionExplicit && !model)
  ) {
    return {
      model_name: requestedModelName,
      thinking_enabled: null,
      reasoning_effort: null,
    };
  }
  const requestedMode = modeSelectionExplicit ? mode : undefined;
  const modeProfile = model
    ? getAgentModeExecutionProfile(
        requestedMode,
        model.supports_thinking,
        model.supports_reasoning_effort,
      )
    : getAgentModeExecutionProfile(requestedMode, true, true);
  return {
    model_name: requestedModelName,
    ...modeProfile,
  };
}
