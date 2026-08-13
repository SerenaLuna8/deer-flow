"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

import { GatewayApiError } from "@/core/api/errors";
import {
  broadcastProjectMemoryCacheHint,
  collectAccountProjectCacheScopes,
} from "@/core/private-work/memory-freshness";

import {
  fetchAccountPersonalization,
  resetAccountMemory,
  runAbortableAccountPersonalizationMutation,
  updateAccountPersonalization,
} from "./api";
import {
  accountPersonalizationMutationKey,
  accountPersonalizationQueryKey,
  isAccountProjectMemoryQueryKey,
} from "./query-keys";
import type {
  AccountPersonalization,
  ResetAccountMemoryInput,
  UpdateAccountPersonalizationInput,
} from "./types";

const UNAVAILABLE_QUERY_KEY = [
  "account",
  "unavailable",
  "personalization",
] as const;

async function invalidateProjectMemory(
  queryClient: QueryClient,
  accountId: string,
): Promise<void> {
  await queryClient.invalidateQueries({
    predicate: (query) =>
      isAccountProjectMemoryQueryKey(query.queryKey, accountId),
  });
}

async function removeProjectMemory(
  queryClient: QueryClient,
  accountId: string,
): Promise<void> {
  const predicate = (query: { queryKey: readonly unknown[] }) =>
    isAccountProjectMemoryQueryKey(query.queryKey, accountId);
  await queryClient.cancelQueries({ predicate });
  queryClient.removeQueries({ predicate });
}

export function useAccountPersonalization(accountId: string | null) {
  return useQuery({
    queryKey: accountId
      ? accountPersonalizationQueryKey(accountId)
      : UNAVAILABLE_QUERY_KEY,
    queryFn: ({ signal }) => {
      if (!accountId) throw new Error("Account personalization is unavailable");
      return fetchAccountPersonalization(accountId, signal);
    },
    enabled: accountId !== null,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function useUpdateAccountPersonalization(accountId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: accountId
      ? accountPersonalizationMutationKey(accountId, "update-memory")
      : [...UNAVAILABLE_QUERY_KEY, "mutation", "update-memory"],
    mutationFn: (input: UpdateAccountPersonalizationInput) => {
      if (!accountId)
        return Promise.reject(new Error("Account is unavailable"));
      return runAbortableAccountPersonalizationMutation(accountId, (signal) =>
        updateAccountPersonalization(accountId, input, signal),
      );
    },
    onSuccess: async (result) => {
      if (!accountId) return;
      const scopes = collectAccountProjectCacheScopes(queryClient, accountId);
      queryClient.setQueryData<AccountPersonalization>(
        accountPersonalizationQueryKey(accountId),
        result,
      );
      await invalidateProjectMemory(queryClient, accountId);
      for (const scope of scopes) {
        broadcastProjectMemoryCacheHint(scope, "document");
      }
    },
    onError: async (error) => {
      if (
        accountId &&
        error instanceof GatewayApiError &&
        error.status === 409
      ) {
        await queryClient.invalidateQueries({
          queryKey: accountPersonalizationQueryKey(accountId),
        });
      }
    },
  });
}

export function useResetAccountMemory(accountId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: accountId
      ? accountPersonalizationMutationKey(accountId, "reset-memory")
      : [...UNAVAILABLE_QUERY_KEY, "mutation", "reset-memory"],
    mutationFn: (input: ResetAccountMemoryInput) => {
      if (!accountId)
        return Promise.reject(new Error("Account is unavailable"));
      return runAbortableAccountPersonalizationMutation(accountId, (signal) =>
        resetAccountMemory(accountId, input, signal),
      );
    },
    onSuccess: async (result) => {
      if (!accountId) return;
      const scopes = collectAccountProjectCacheScopes(queryClient, accountId);
      queryClient.setQueryData<AccountPersonalization>(
        accountPersonalizationQueryKey(accountId),
        (current) =>
          current ? { ...current, version: result.version } : current,
      );
      await removeProjectMemory(queryClient, accountId);
      for (const scope of scopes) {
        broadcastProjectMemoryCacheHint(scope, "reset");
      }
    },
    onError: async (error) => {
      if (
        accountId &&
        error instanceof GatewayApiError &&
        error.status === 409
      ) {
        await queryClient.invalidateQueries({
          queryKey: accountPersonalizationQueryKey(accountId),
        });
      }
    },
  });
}
