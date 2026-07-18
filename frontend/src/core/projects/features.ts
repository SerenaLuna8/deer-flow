export const PROJECT_PRIVATE_WORKSPACE = true as const;
export const PROJECT_AUTOMATION = true as const;
export const PROJECT_FIRST_MODE = true as const;

export function projectAutomationEntryEnabled(
  featureEnabled: boolean,
  staticWebsiteOnly: boolean,
  canReadPrivateWork: boolean,
  readiness: "ready" | "unavailable" | undefined,
): boolean {
  return (
    featureEnabled &&
    !staticWebsiteOnly &&
    canReadPrivateWork &&
    readiness === "ready"
  );
}

export function workspaceLandingPath(
  _staticMode: boolean,
  _demoThreadId: string | null,
): string {
  return "/workspace";
}
