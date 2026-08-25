import { z } from "zod";

import { GatewayApiError, throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { privateWorkQueryKey } from "@/core/private-work/query-keys";
import {
  projectClientScopeSchema,
  type ProjectClientScope,
  type ProjectPrivateWorkScope,
} from "@/core/private-work/types";

export const RUN_EXECUTION_PHASES = [
  "queued",
  "waiting_for_worker",
  "starting",
  "executing",
  "retry_wait",
  "waiting_for_lease_expiry",
  "waiting_for_terminalization",
  "waiting_for_recovery",
  "recovering",
  "cancelling",
  "terminal",
] as const;

export const RUN_EXECUTION_STATE_POLL_INTERVAL_MS = 2_000;
export const RUN_EXECUTION_STATE_MAX_RETRIES = 3;
const RUN_EXECUTION_STATE_MAX_RETRY_DELAY_MS = 8_000;

const runExecutionPhaseSchema = z.enum(RUN_EXECUTION_PHASES);
const runStatusSchema = z.enum([
  "pending",
  "running",
  "success",
  "error",
  "timeout",
  "interrupted",
]);
const timestampSchema = z.string().datetime({ offset: true });
const terminalRunStatuses: ReadonlySet<RunExecutionState["run_status"]> =
  new Set(["success", "error", "timeout", "interrupted"]);

export const runExecutionStateSchema = z
  .object({
    phase: runExecutionPhaseSchema,
    observed_at: timestampSchema,
    phase_started_at: timestampSchema.nullable(),
    execution_started_at: timestampSchema.nullable(),
    retry_at: timestampSchema.nullable(),
    run_status: runStatusSchema,
  })
  .strict()
  .superRefine((state, context) => {
    const terminalStatus = terminalRunStatuses.has(state.run_status);
    if (state.phase === "terminal" && !terminalStatus) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "A terminal phase requires a terminal Run status.",
        path: ["run_status"],
      });
    }
    if (state.phase !== "terminal" && terminalStatus) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "A terminal Run status requires the terminal phase.",
        path: ["phase"],
      });
    }
  });

export type RunExecutionPhase = z.infer<typeof runExecutionPhaseSchema>;
export type RunExecutionState = z.infer<typeof runExecutionStateSchema>;

export type RunExecutionPhaseSemantic =
  | "waiting"
  | "starting"
  | "executing"
  | "recovering"
  | "cancelling"
  | "terminal";

const RUN_EXECUTION_PHASE_SEMANTICS = {
  queued: "waiting",
  waiting_for_worker: "waiting",
  starting: "starting",
  executing: "executing",
  retry_wait: "waiting",
  waiting_for_lease_expiry: "waiting",
  waiting_for_terminalization: "waiting",
  waiting_for_recovery: "waiting",
  recovering: "recovering",
  cancelling: "cancelling",
  terminal: "terminal",
} as const satisfies Record<RunExecutionPhase, RunExecutionPhaseSemantic>;

export function runExecutionPhaseSemantic(
  phase: RunExecutionPhase,
): RunExecutionPhaseSemantic {
  return RUN_EXECUTION_PHASE_SEMANTICS[runExecutionPhaseSchema.parse(phase)];
}

export function isTerminalRunExecutionState(
  state: Pick<RunExecutionState, "phase" | "run_status">,
): boolean {
  return (
    state.phase === "terminal" && terminalRunStatuses.has(state.run_status)
  );
}

const executionStateIdSchema = z.string().uuid();

export function runExecutionStateQueryKey(
  scope: ProjectClientScope,
  threadId: string,
  runId: string,
) {
  const selectedThreadId = executionStateIdSchema.parse(threadId);
  const selectedRunId = executionStateIdSchema.parse(runId);
  return privateWorkQueryKey(
    scope,
    "thread",
    selectedThreadId,
    "run",
    selectedRunId,
    "execution-state",
  );
}

export function runExecutionStateObserverQueryKey(
  scope: ProjectClientScope,
  threadId: string,
  runId: string,
  generation: number,
) {
  const selectedGeneration = z.number().int().positive().parse(generation);
  return [
    ...runExecutionStateQueryKey(scope, threadId, runId),
    "generation",
    selectedGeneration,
  ] as const;
}

export function runExecutionStateQueryEnabled(
  activeRunId: string | null,
  generation: number | null,
  visibilityState: DocumentVisibilityState,
): boolean {
  return (
    visibilityState === "visible" &&
    activeRunId !== null &&
    executionStateIdSchema.safeParse(activeRunId).success &&
    generation !== null &&
    Number.isSafeInteger(generation) &&
    generation > 0
  );
}

export function runExecutionStatePollInterval(
  visibilityState: DocumentVisibilityState,
  state?: RunExecutionState,
): number | false {
  if (
    visibilityState !== "visible" ||
    (state !== undefined && isTerminalRunExecutionState(state))
  ) {
    return false;
  }
  return RUN_EXECUTION_STATE_POLL_INTERVAL_MS;
}

export function selectObservedRunExecutionState(
  state: RunExecutionState | undefined,
  failed: boolean,
): RunExecutionState | "unavailable" {
  return failed ? "unavailable" : (state ?? "unavailable");
}

export function shouldRetryRunExecutionState(
  failureCount: number,
  error: unknown,
): boolean {
  if (failureCount >= RUN_EXECUTION_STATE_MAX_RETRIES) return false;
  if (error instanceof GatewayApiError) {
    return error.status === 429 || error.status === 503;
  }
  return error instanceof TypeError;
}

export function runExecutionStateRetryDelay(failureCount: number): number {
  const exponent = Math.max(0, Math.min(failureCount, 3));
  return Math.min(
    1_000 * 2 ** exponent,
    RUN_EXECUTION_STATE_MAX_RETRY_DELAY_MS,
  );
}

export type RunExecutionStateAccess = Pick<
  ProjectPrivateWorkScope,
  "apiBaseURL" | "scope"
>;

function projectPrivateWorkBaseURL(access: RunExecutionStateAccess): string {
  const scope = projectClientScopeSchema.parse(access.scope);
  const baseURL = access.apiBaseURL.replace(/\/+$/u, "");
  const expectedSuffix = `/projects/${scope.projectId}/private-work`;
  if (!baseURL.endsWith(expectedSuffix)) {
    throw new Error(
      "Run execution state requires a project-scoped private-work URL",
    );
  }
  return baseURL;
}

export async function fetchRunExecutionState(
  access: RunExecutionStateAccess,
  threadId: string,
  runId: string,
  signal: AbortSignal,
): Promise<RunExecutionState> {
  const selectedThreadId = executionStateIdSchema.parse(threadId);
  const selectedRunId = executionStateIdSchema.parse(runId);
  const response = await fetchWithAuth(
    `${projectPrivateWorkBaseURL(access)}/threads/${encodeURIComponent(selectedThreadId)}/runs/${encodeURIComponent(selectedRunId)}/execution-state`,
    { method: "GET", signal },
  );
  if (!response.ok) {
    await throwGatewayApiError(response, "Failed to load Run execution state.");
  }
  return runExecutionStateSchema.parse(await response.json());
}
