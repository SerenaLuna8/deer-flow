import { adminModelAccountIdSchema, adminModelIdSchema } from "./types";

export function adminModelSettingsRoot(accountId: string) {
  return [
    "account",
    adminModelAccountIdSchema.parse(accountId),
    "admin",
    "settings",
    "models",
  ] as const;
}

export function adminModelSettingsQueryKey(accountId: string) {
  return adminModelSettingsRoot(accountId);
}

export function adminModelMutationKey(
  accountId: string,
  action:
    | "create"
    | "replace"
    | "clear_api_key"
    | "status"
    | "default"
    | "test_connection",
  modelId?: string,
) {
  return [
    ...adminModelSettingsRoot(accountId),
    "mutation",
    action,
    modelId ? adminModelIdSchema.parse(modelId) : null,
  ] as const;
}
