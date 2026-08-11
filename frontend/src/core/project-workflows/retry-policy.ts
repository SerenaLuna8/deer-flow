import type { WorkflowRunStatusV1 } from "./transport";

/**
 * UI safety projection. The server remains authoritative for every retry, and
 * a side-effect-unknown Run is terminal even if a stale response says retry is
 * otherwise eligible.
 */
export const workflowRunManualRetryAllowed = (
  status: WorkflowRunStatusV1,
  serverRetryEligible: boolean,
): boolean => status !== "side_effect_unknown" && serverRetryEligible;
