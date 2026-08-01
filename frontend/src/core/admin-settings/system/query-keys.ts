import { adminSystemSettingsAccountIdSchema } from "./types";

export function adminSystemSettingsRoot(accountId: string) {
  return [
    "account",
    adminSystemSettingsAccountIdSchema.parse(accountId),
    "admin",
    "settings",
    "system",
  ] as const;
}

export function adminSystemSettingsQueryKey(accountId: string) {
  return adminSystemSettingsRoot(accountId);
}

export function adminSystemSettingsMutationKey(accountId: string) {
  return [...adminSystemSettingsRoot(accountId), "mutation"] as const;
}
