"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useImperativeRequest } from "@/core/api/use-imperative-request";

import {
  createAdminModelProvider,
  createAdminProviderModel,
  deleteAdminModelProvider,
  deleteAdminProviderModel,
  listAdminModelProviders,
  listAdminProviderModels,
  setAdminProviderModelStatus,
  testAdminProviderModel,
  updateAdminModelProvider,
} from "./api";
import {
  adminModelRegistryQueryKey,
  adminModelRegistryRoot,
} from "./query-keys";
import {
  adminModelRegistryAccountIdSchema,
  type AdminProviderModelStatus,
  type CreateAdminModelProviderInput,
  type CreateAdminProviderModelInput,
  type UpdateAdminModelProviderInput,
} from "./types";

export function useAdminModelProviders(accountId: string, enabled = true) {
  const parsed = adminModelRegistryAccountIdSchema.parse(accountId);
  return useQuery({
    queryKey: adminModelRegistryQueryKey(parsed, "providers"),
    queryFn: ({ signal }) => listAdminModelProviders(parsed, signal),
    // Admin queries mount only after the authenticated admin state resolves.
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function useAdminProviderModels(
  accountId: string,
  providerId: string,
  enabled = true,
) {
  const parsed = adminModelRegistryAccountIdSchema.parse(accountId);
  return useQuery({
    queryKey: adminModelRegistryQueryKey(
      parsed,
      "providers",
      providerId,
      "models",
    ),
    queryFn: ({ signal }) => listAdminProviderModels(parsed, providerId, signal),
    enabled,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

/**
 * Invalidate the whole registry root: model mutations also move provider
 * aggregates (`model_count`, `endpoint_frozen`), so partial invalidation
 * would leave stale cards.
 */
function useInvalidateAdminModelRegistry(accountId: string) {
  const queryClient = useQueryClient();
  return () =>
    queryClient.invalidateQueries({
      queryKey: adminModelRegistryRoot(accountId),
    });
}

/** Secret-bearing create: imperative so the API key never enters query state. */
export function useCreateAdminModelProvider(accountId: string) {
  const parsed = adminModelRegistryAccountIdSchema.parse(accountId);
  const invalidate = useInvalidateAdminModelRegistry(parsed);
  return useImperativeRequest(async (input: CreateAdminModelProviderInput) => {
    const result = await createAdminModelProvider(parsed, input);
    await invalidate();
    return result;
  });
}

/** Secret-capable update: imperative because it may carry a replacement key. */
export function useUpdateAdminModelProvider(accountId: string) {
  const parsed = adminModelRegistryAccountIdSchema.parse(accountId);
  const invalidate = useInvalidateAdminModelRegistry(parsed);
  return useImperativeRequest(
    async ({
      providerId,
      input,
    }: {
      providerId: string;
      input: UpdateAdminModelProviderInput;
    }) => {
      const result = await updateAdminModelProvider(parsed, providerId, input);
      await invalidate();
      return result;
    },
  );
}

export function useDeleteAdminModelProvider(accountId: string) {
  const parsed = adminModelRegistryAccountIdSchema.parse(accountId);
  const invalidate = useInvalidateAdminModelRegistry(parsed);
  return useMutation({
    mutationFn: (providerId: string) =>
      deleteAdminModelProvider(parsed, providerId),
    onSuccess: () => invalidate(),
  });
}

export function useCreateAdminProviderModel(accountId: string) {
  const parsed = adminModelRegistryAccountIdSchema.parse(accountId);
  const invalidate = useInvalidateAdminModelRegistry(parsed);
  return useMutation({
    mutationFn: ({
      providerId,
      input,
    }: {
      providerId: string;
      input: CreateAdminProviderModelInput;
    }) => createAdminProviderModel(parsed, providerId, input),
    onSuccess: () => invalidate(),
  });
}

export function useSetAdminProviderModelStatus(accountId: string) {
  const parsed = adminModelRegistryAccountIdSchema.parse(accountId);
  const invalidate = useInvalidateAdminModelRegistry(parsed);
  return useMutation({
    mutationFn: ({
      modelId,
      status,
    }: {
      modelId: string;
      status: AdminProviderModelStatus;
    }) => setAdminProviderModelStatus(parsed, modelId, status),
    onSuccess: () => invalidate(),
  });
}

export function useDeleteAdminProviderModel(accountId: string) {
  const parsed = adminModelRegistryAccountIdSchema.parse(accountId);
  const invalidate = useInvalidateAdminModelRegistry(parsed);
  return useMutation({
    mutationFn: (modelId: string) => deleteAdminProviderModel(parsed, modelId),
    onSuccess: () => invalidate(),
  });
}

export function useTestAdminProviderModel(accountId: string) {
  const parsed = adminModelRegistryAccountIdSchema.parse(accountId);
  return useMutation({
    mutationFn: (modelId: string) => testAdminProviderModel(parsed, modelId),
  });
}
