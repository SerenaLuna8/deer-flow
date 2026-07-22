export const PROJECT_START_CHAT_INTENT = "start_chat" as const;
export const PROJECT_START_CHAT_INTENT_ID_PARAM = "intent_id" as const;

export function projectAgentsStartChatPath(
  projectSlug: string,
  intentId?: string,
): string {
  const params = new URLSearchParams({ intent: PROJECT_START_CHAT_INTENT });
  if (intentId) params.set(PROJECT_START_CHAT_INTENT_ID_PARAM, intentId);
  return `/projects/${encodeURIComponent(projectSlug)}/agents?${params.toString()}`;
}

export function isProjectStartChatIntent(
  value: string | string[] | undefined,
): boolean {
  return value === PROJECT_START_CHAT_INTENT;
}

export function projectStartChatIntentId(
  value: string | string[] | undefined,
): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized.length > 0 && normalized.length <= 128 ? normalized : null;
}
