export const RUN_EXECUTION_PROFILE_CONTEXT_KEY =
  "__deerflow_execution_profile" as const;

export const REASONING_EFFORTS = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
] as const;

export type ReasoningEffort = (typeof REASONING_EFFORTS)[number];

/**
 * Client preferences for a Run. Gateway admission validates these preferences
 * against the selected Agent and model catalog before freezing an effective
 * profile for the Worker.
 */
export interface RunExecutionProfileRequest {
  model_name: string | null;
  thinking_enabled: boolean | null;
  reasoning_effort: ReasoningEffort | null;
}

/** The server-resolved profile returned by Run list/get responses. */
export interface RunExecutionProfile {
  model_name: string;
  thinking_enabled: boolean;
  reasoning_effort: ReasoningEffort | null;
  supports_vision: boolean;
}

export function buildOutputLimitRetryProfile(
  profile: RunExecutionProfileRequest,
): RunExecutionProfileRequest {
  return {
    ...profile,
    thinking_enabled: false,
    reasoning_effort: "none",
  };
}

function isReasoningEffort(value: unknown): value is ReasoningEffort {
  return REASONING_EFFORTS.some((effort) => effort === value);
}

/**
 * Read the public, server-resolved profile from an arbitrary Run payload.
 * Run history is already schema-validated at the API boundary; this small
 * guard keeps downstream presentation code safe when it also receives live
 * SDK values with a narrower compile-time type.
 */
export function readRunExecutionProfile(
  value: unknown,
): RunExecutionProfile | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  const modelName = Reflect.get(value, "model_name");
  const thinkingEnabled = Reflect.get(value, "thinking_enabled");
  const reasoningEffort = Reflect.get(value, "reasoning_effort");
  const supportsVision = Reflect.get(value, "supports_vision");
  if (
    typeof modelName !== "string" ||
    modelName.length === 0 ||
    typeof thinkingEnabled !== "boolean" ||
    (reasoningEffort !== null && !isReasoningEffort(reasoningEffort)) ||
    typeof supportsVision !== "boolean"
  ) {
    return null;
  }
  return {
    model_name: modelName,
    thinking_enabled: thinkingEnabled,
    reasoning_effort: reasoningEffort,
    supports_vision: supportsVision,
  };
}

/** Index effective profiles by Run without trusting private Run metadata. */
export function collectRunExecutionProfiles(
  runs: readonly unknown[],
): ReadonlyMap<string, RunExecutionProfile> {
  const profiles = new Map<string, RunExecutionProfile>();
  for (const run of runs) {
    if (typeof run !== "object" || run === null || Array.isArray(run)) {
      continue;
    }
    const runId = Reflect.get(run, "run_id");
    const profile = readRunExecutionProfile(
      Reflect.get(run, "execution_profile"),
    );
    if (typeof runId === "string" && runId.length > 0 && profile) {
      profiles.set(runId, profile);
    }
  }
  return profiles;
}

const LEGACY_EXECUTION_CONTEXT_KEYS = new Set([
  "model",
  "model_name",
  "mode",
  "model_selection_explicit",
  "mode_selection_explicit",
  "thinking_enabled",
  "reasoning_effort",
  "is_plan_mode",
  "subagent_enabled",
  RUN_EXECUTION_PROFILE_CONTEXT_KEY,
]);

/**
 * Attach a profile for the SDK request adapter while keeping all execution
 * authority out of the generic LangGraph context object.
 */
export function withRunExecutionProfileContext(
  context: Readonly<Record<string, unknown>>,
  profile: RunExecutionProfileRequest,
): Record<string, unknown> {
  const runtimeContext = Object.fromEntries(
    Object.entries(context).filter(
      ([key]) => !LEGACY_EXECUTION_CONTEXT_KEYS.has(key),
    ),
  );
  return {
    ...runtimeContext,
    [RUN_EXECUTION_PROFILE_CONTEXT_KEY]: profile,
  };
}
