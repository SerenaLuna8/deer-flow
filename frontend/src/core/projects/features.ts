export const PROJECT_PRIVATE_WORKSPACE = true as const;
export const PROJECT_AUTOMATION = true as const;
export const PROJECT_FIRST_MODE = true as const;

export function projectAutomationEntryEnabled(
  featureEnabled: boolean,
  staticWebsiteOnly: boolean,
  canReadPrivateWork: boolean,
  readiness: "ready" | "migration_required" | "unavailable" | undefined,
): boolean {
  return (
    featureEnabled &&
    !staticWebsiteOnly &&
    canReadPrivateWork &&
    readiness === "ready"
  );
}

export function workspaceLandingPath(
  staticMode: boolean,
  demoThreadId: string | null,
): string {
  if (staticMode) {
    return demoThreadId
      ? `/workspace/chats/${demoThreadId}`
      : "/workspace/chats/new";
  }
  return PROJECT_FIRST_MODE ? "/workspace" : "/workspace/chats/new";
}
