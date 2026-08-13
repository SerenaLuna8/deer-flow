import { isProjectResponseErrorCode } from "@/core/private-work/api-client";

export async function projectNewChatErrorMessage(
  error: unknown,
  refreshDefaultAgent: () => Promise<unknown>,
  fallback: string,
  defaultAgentUnavailable: string,
): Promise<string> {
  if (isProjectResponseErrorCode(error, "DEFAULT_AGENT_UNAVAILABLE")) {
    try {
      await refreshDefaultAgent();
    } catch {
      // The stable admission error remains actionable even if refresh fails.
    }
    return defaultAgentUnavailable;
  }
  return error instanceof Error ? error.message : fallback;
}
