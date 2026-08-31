import { QueryClient } from "@tanstack/react-query";

import { abortAccountPersonalizationAccount } from "@/core/account-personalization/api";
import { abortAdminOperationsAccount } from "@/core/admin-operations/api";
import { abortAdminKnowledgeSettingsAccount } from "@/core/admin-settings/knowledge/api";
import { abortAdminModelSettingsAccount } from "@/core/admin-settings/models/api";
import { abortAdminSystemSettingsAccount } from "@/core/admin-settings/system/api";
import { abortPrivacyCenterAccount } from "@/core/privacy-center/hooks";

import type { User } from "./types";

export function createAccountQueryClient(): QueryClient {
  return new QueryClient();
}

export async function transitionAccountQueries(
  queryClient: QueryClient,
  previousUserId: string | null,
  nextUserId: string | null,
  options: {
    force?: boolean;
    previousSystemRole?: User["system_role"] | null;
    nextSystemRole?: User["system_role"] | null;
  } = {},
): Promise<boolean> {
  const roleChanged =
    options.previousSystemRole !== undefined &&
    options.nextSystemRole !== undefined &&
    options.previousSystemRole !== options.nextSystemRole;
  if (!options.force && previousUserId === nextUserId && !roleChanged) {
    return false;
  }
  if (previousUserId) {
    abortAccountPersonalizationAccount(previousUserId);
    abortAdminOperationsAccount(previousUserId);
    abortAdminKnowledgeSettingsAccount(previousUserId);
    abortAdminModelSettingsAccount(previousUserId);
    abortAdminSystemSettingsAccount(previousUserId);
    abortPrivacyCenterAccount(previousUserId);
  }
  const cancellation = queryClient.cancelQueries();
  await cancellation;
  queryClient.clear();
  return true;
}
