function requirePart(value: string, label: string): string {
  if (value.trim() === "") throw new Error(`${label} is required`);
  return value;
}

export function agentBuilderRootKey(accountId: string, projectId: string) {
  return [
    "account",
    requirePart(accountId, "Account ID"),
    "project",
    requirePart(projectId, "Project ID"),
    "agent-builder",
  ] as const;
}

export function agentBuilderSessionsKey(
  accountId: string,
  projectId: string,
) {
  return [...agentBuilderRootKey(accountId, projectId), "sessions"] as const;
}

export function agentBuilderSessionsInvalidation(
  accountId: string,
  projectId: string,
) {
  return {
    queryKey: agentBuilderSessionsKey(accountId, projectId),
    exact: true,
  } as const;
}

export function agentBuilderSessionKey(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  return [
    ...agentBuilderSessionsKey(accountId, projectId),
    requirePart(sessionId, "Session ID"),
  ] as const;
}

export function agentBuilderMutationKey(
  accountId: string,
  projectId: string,
  action: string,
) {
  return [
    ...agentBuilderRootKey(accountId, projectId),
    "mutation",
    requirePart(action, "Mutation action"),
  ] as const;
}
