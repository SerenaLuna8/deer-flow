export function adminKnowledgeModelsRoot(accountId: string) {
  return [
    "account",
    accountId,
    "admin",
    "settings",
    "knowledge-models",
  ] as const;
}

export function adminKnowledgeModelsQueryKey(
  accountId: string,
  ...segments: readonly unknown[]
) {
  return [...adminKnowledgeModelsRoot(accountId), ...segments] as const;
}
