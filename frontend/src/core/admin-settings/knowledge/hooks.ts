"use client";

import {
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { useCallback } from "react";

import {
  fetchAdminKnowledgeSettings,
  knowledgeSettingsAccountGeneration,
  replaceAdminKnowledgeSettings,
} from "./api";
import { adminKnowledgeSettingsRoot } from "./query-keys";
import type { AdminKnowledgeSettingsUpdate } from "./types";

export function adminKnowledgeSettingsQueryOptions(accountId: string) {
  return {
    queryKey: adminKnowledgeSettingsRoot(accountId),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchAdminKnowledgeSettings(accountId, signal),
    retry: false,
    refetchOnWindowFocus: false,
  };
}

export function useAdminKnowledgeSettings(accountId: string) {
  return useQuery(adminKnowledgeSettingsQueryOptions(accountId));
}

export async function saveAdminKnowledgeSettings(
  queryClient: QueryClient,
  accountId: string,
  input: AdminKnowledgeSettingsUpdate,
  signal?: AbortSignal,
) {
  const generation = knowledgeSettingsAccountGeneration(accountId);
  const result = await replaceAdminKnowledgeSettings(accountId, input, signal);
  const queryKey = adminKnowledgeSettingsRoot(accountId);
  await queryClient.cancelQueries({ queryKey });
  if (
    signal?.aborted ||
    generation !== knowledgeSettingsAccountGeneration(accountId)
  )
    throw new DOMException("Request aborted", "AbortError");
  queryClient.setQueryData(queryKey, result);
  await queryClient.invalidateQueries({ queryKey });
  return result;
}

/** No useMutation: this function never places secret-bearing inputs in a cache. */
export function useSaveAdminKnowledgeSettings(accountId: string) {
  const queryClient = useQueryClient();
  return useCallback(
    (input: AdminKnowledgeSettingsUpdate, signal?: AbortSignal) =>
      saveAdminKnowledgeSettings(queryClient, accountId, input, signal),
    [accountId, queryClient],
  );
}
