import { adminKnowledgeAccountIdSchema } from "./types";

export function adminKnowledgeSettingsRoot(accountId: string) {
  return [
    "account",
    adminKnowledgeAccountIdSchema.parse(accountId),
    "admin",
    "settings",
    "knowledge",
  ] as const;
}
