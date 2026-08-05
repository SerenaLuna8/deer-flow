import { accountPersonalizationAccountIdSchema } from "./types";

export function accountPersonalizationRoot(accountId: string) {
  return [
    "account",
    accountPersonalizationAccountIdSchema.parse(accountId),
    "personalization",
  ] as const;
}

export function accountPersonalizationQueryKey(accountId: string) {
  return accountPersonalizationRoot(accountId);
}

export function accountPersonalizationMutationKey(
  accountId: string,
  action: "update-memory" | "reset-memory",
) {
  return [...accountPersonalizationRoot(accountId), "mutation", action] as const;
}

export function isAccountProjectMemoryQueryKey(
  queryKey: readonly unknown[],
  accountId: string,
): boolean {
  const parsedAccountId = accountPersonalizationAccountIdSchema.parse(accountId);
  return (
    queryKey[0] === "account" &&
    queryKey[1] === parsedAccountId &&
    queryKey[2] === "project" &&
    queryKey[4] === "private-work" &&
    queryKey[5] === "memory"
  );
}
