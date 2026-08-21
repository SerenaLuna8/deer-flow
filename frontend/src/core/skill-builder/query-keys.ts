function requirePart(value: string, label: string): string {
  if (value.trim() === "") throw new Error(`${label} is required`);
  return value;
}

export function skillBuilderRootKey(accountId: string, projectId: string) {
  return [
    "account",
    requirePart(accountId, "Account ID"),
    "project",
    requirePart(projectId, "Project ID"),
    "skill-builder",
  ] as const;
}

export function skillBuilderSessionsKey(accountId: string, projectId: string) {
  return [...skillBuilderRootKey(accountId, projectId), "sessions"] as const;
}

export function skillBuilderSessionsInvalidation(
  accountId: string,
  projectId: string,
) {
  return {
    queryKey: skillBuilderSessionsKey(accountId, projectId),
    exact: true,
  } as const;
}

export function skillBuilderSessionKey(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  return [
    ...skillBuilderSessionsKey(accountId, projectId),
    requirePart(sessionId, "Session ID"),
  ] as const;
}

export function skillBuilderActivitiesKey(
  accountId: string,
  projectId: string,
  sessionId: string,
) {
  return [
    ...skillBuilderSessionKey(accountId, projectId, sessionId),
    "activities",
  ] as const;
}

export function skillBuilderMutationKey(
  accountId: string,
  projectId: string,
  action: string,
) {
  return [
    ...skillBuilderRootKey(accountId, projectId),
    "mutation",
    requirePart(action, "Mutation action"),
  ] as const;
}
