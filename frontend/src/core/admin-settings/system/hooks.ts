"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { modelsQueryKey } from "@/core/models/hooks";

import {
  fetchAdminSystemSettings,
  replaceAdminSystemSettingsSection,
  runAbortableAdminSystemSettingsMutation,
  type ReplaceSystemSettingsSectionInput,
} from "./api";
import {
  adminSystemSettingsMutationKey,
  adminSystemSettingsQueryKey,
  adminSystemSettingsRoot,
} from "./query-keys";
import { adminSystemSettingsAccountIdSchema } from "./types";

export function adminSystemSettingsQueryOptions(accountId: string) {
  const parsed = adminSystemSettingsAccountIdSchema.parse(accountId);
  return {
    queryKey: adminSystemSettingsQueryKey(parsed),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchAdminSystemSettings(parsed, signal),
    retry: false,
    refetchOnWindowFocus: false,
  };
}

export function useAdminSystemSettings(accountId: string) {
  return useQuery(adminSystemSettingsQueryOptions(accountId));
}

export function useReplaceAdminSystemSettingsSection(accountId: string) {
  const parsed = adminSystemSettingsAccountIdSchema.parse(accountId);
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: adminSystemSettingsMutationKey(parsed),
    mutationFn: (request: ReplaceSystemSettingsSectionInput) =>
      runAbortableAdminSystemSettingsMutation(parsed, (signal) => {
        switch (request.section) {
          case "agent_runtime":
            return replaceAdminSystemSettingsSection(
              parsed,
              request.section,
              request.input,
              signal,
            );
          case "auth":
            return replaceAdminSystemSettingsSection(
              parsed,
              request.section,
              request.input,
              signal,
            );
          case "memory_document":
            return replaceAdminSystemSettingsSection(
              parsed,
              request.section,
              request.input,
              signal,
            );
          case "quotas":
            return replaceAdminSystemSettingsSection(
              parsed,
              request.section,
              request.input,
              signal,
            );
        }
      }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: adminSystemSettingsRoot(parsed),
        }),
        queryClient.invalidateQueries({ queryKey: modelsQueryKey }),
      ]);
    },
  });
}
