import { isProjectResponseErrorCode } from "@/core/private-work/api-client";

export const PROJECT_DEFAULT_AGENT_UNAVAILABLE_MESSAGE =
  "项目默认 Agent 当前不可用，请联系项目管理员处理。";

export async function projectNewChatErrorMessage(
  error: unknown,
  refreshDefaultAgent: () => Promise<unknown>,
  fallback: string,
): Promise<string> {
  if (isProjectResponseErrorCode(error, "DEFAULT_AGENT_UNAVAILABLE")) {
    try {
      await refreshDefaultAgent();
    } catch {
      // The stable admission error remains actionable even if refresh fails.
    }
    return PROJECT_DEFAULT_AGENT_UNAVAILABLE_MESSAGE;
  }
  return error instanceof Error ? error.message : fallback;
}
