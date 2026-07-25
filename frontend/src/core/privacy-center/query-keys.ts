export function privacyCenterRoot(accountId: string) {
  return ["account", accountId, "privacy-center"] as const;
}

export function privacyCasesQueryKey(accountId: string) {
  return [...privacyCenterRoot(accountId), "cases"] as const;
}

export function privacyEarlyDeleteMutationKey(
  accountId: string,
  projectId: string,
) {
  return [
    ...privacyCenterRoot(accountId),
    "cases",
    projectId,
    "early-delete",
  ] as const;
}
