export function adminModelRegistryRoot(accountId: string) {
  return [
    "account",
    accountId,
    "admin",
    "settings",
    "model-registry",
  ] as const;
}

export function adminModelRegistryQueryKey(
  accountId: string,
  ...segments: readonly unknown[]
) {
  return [...adminModelRegistryRoot(accountId), ...segments] as const;
}
