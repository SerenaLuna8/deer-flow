export const RUN_WORKLOAD_PROFILE_CONTEXT_KEY =
  "__deerflow_workload_profile" as const;

export const RUN_WORKLOAD_PROFILES = ["interactive", "research"] as const;

export type RunWorkloadProfileName = (typeof RUN_WORKLOAD_PROFILES)[number];

export const DEFAULT_RUN_WORKLOAD_PROFILE: RunWorkloadProfileName =
  "interactive";

export function isRunWorkloadProfile(
  value: unknown,
): value is RunWorkloadProfileName {
  return RUN_WORKLOAD_PROFILES.some((profile) => profile === value);
}

/** Read only the effective profile projected by the server on a Run record. */
export function readRunWorkloadProfile(
  value: unknown,
): RunWorkloadProfileName | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  const profile = Reflect.get(value, "workload_profile");
  return isRunWorkloadProfile(profile) ? profile : null;
}

export function resolveDisplayedRunWorkloadProfile(
  runsNewestFirst: readonly unknown[],
  activeRunId: string | null,
): RunWorkloadProfileName | null {
  const run = activeRunId
    ? runsNewestFirst.find(
        (candidate) =>
          typeof candidate === "object" &&
          candidate !== null &&
          !Array.isArray(candidate) &&
          Reflect.get(candidate, "run_id") === activeRunId,
      )
    : runsNewestFirst[0];
  return readRunWorkloadProfile(run);
}

/**
 * Carry a one-Run workload choice through the SDK's context extension seam.
 * The final request adapter promotes it to the dedicated top-level field.
 */
export function withRunWorkloadProfileContext(
  context: Readonly<Record<string, unknown>>,
  profile: RunWorkloadProfileName,
): Record<string, unknown> {
  if (!isRunWorkloadProfile(profile)) {
    throw new TypeError("Run workload profile is invalid.");
  }
  const runtimeContext = Object.fromEntries(
    Object.entries(context).filter(
      ([key]) =>
        key !== "workload_profile" && key !== RUN_WORKLOAD_PROFILE_CONTEXT_KEY,
    ),
  );
  return {
    ...runtimeContext,
    [RUN_WORKLOAD_PROFILE_CONTEXT_KEY]: profile,
  };
}
